#
# SPDX-License-Identifier: Apache-2.0

"""
Project Compute views (Project > Telemetry > Compute).

The index page is a light shell; every section is an HTMX partial that
loads and refreshes itself, and renders a bounded amount of data (four
cards, three quota rows, at most IDLE_LIMIT idle holders and TABLE_LIMIT
rows per table, one chart) whatever the size of the project.

Two sources, each read once per refresh and shared by the partials and by
every member of the project through grian_ui.cache, always with
project_cache_key() (the project ID is in the key):

* the Compute API, with the user's token: the project's servers, flavors
  and absolute limits (what holds quota, as Project > Overview counts it);
* Prometheus through Aetos, every query filtered with project_matcher():
  current values (every refresh), 7-day statistics, the forecast and the
  charts (cached longer, they change slowly).

When one source fails, the sections show what the other provides.
"""

import logging
import time
import urllib.parse
import uuid

from django.urls import NoReverseMatch
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import generic

from grian_ui import cache
from grian_ui import config
from grian_ui import project
from grian_ui import project_compute as pc
from grian_ui import project_compute_inventory
from grian_ui.datasource import prometheus_project_compute as queries
from grian_ui.datasource.api import get_datasource


LOG = logging.getLogger(__name__)

#: Refresh interval of the partials, in seconds (used by the templates).
REFRESH_SECONDS = 60
#: Seconds the Compute API data and the current metrics are shared.
#: Slightly shorter than the refresh so every refresh gets new data,
#: computed once.
NOVA_CACHE_TTL = REFRESH_SECONDS - 5
SNAPSHOT_CACHE_TTL = REFRESH_SECONDS - 5
#: Seconds the 7-day statistics, the sparklines and the detail charts are
#: shared: a few more minutes barely change a 7-day window.
RIGHTSIZING_CACHE_TTL = 600
SPARKS_CACHE_TTL = 600
DETAIL_CACHE_TTL = 300
#: Seconds the forecast is shared (a trend over up to 30 days).
FORECAST_CACHE_TTL = 1800
#: Seconds the quota trend is shared, per range.
TREND_CACHE_TTL = {"6h": 60, "24h": 300, "7d": 900, "30d": 1800}
#: A copy older than its lifetime plus this margin is flagged "possibly out
#: of date" (still being refreshed, not a failure).
OLD_COPY_MARGIN = 30
#: Bump when the format of a cached value changes (see
#: grian_ui.cache.project_cache_key).
CACHE_VERSION = 1
CACHE_NAMESPACE = "project_compute"

_LOGGED_CONFIG_ERRORS = set()

#: Notes for optional data that is not available: (failed, still loading).
OPTIONAL_NOTES = {
    "nova": (
        _(
            "The Compute API could not be reached: instance states, names "
            "and flavors are missing."
        ),
        _("Loading the instance list from the Compute API…"),
    ),
    "metrics": (
        _(
            "Telemetry data could not be loaded: only the Compute API state "
            "is checked."
        ),
        _("Loading the telemetry data…"),
    ),
    "rightsizing": (
        _("The 7-day statistics could not be loaded."),
        _("Loading the 7-day statistics…"),
    ),
    "forecast": (
        _("The forecast could not be loaded."),
        _("Loading the forecast…"),
    ),
    "sparks": (
        _("The CPU sparklines could not be loaded."),
        _("Loading the CPU sparklines…"),
    ),
}

#: Advice per issue kind (see project_compute.advice_kind).
ADVICE = {
    pc.ISSUE_ERROR: _(
        "The instance failed. Delete it to free its quota, or rebuild it "
        "with a different flavor."
    ),
    pc.ISSUE_CPU_WAIT: _(
        "The instance waits for physical CPU on its host. This comes from "
        "the host, not your workload. If it lasts, tell your operator and "
        "give them the instance ID."
    ),
    pc.ISSUE_SWAP: _(
        "The guest is short of memory. Resize to a flavor with more RAM."
    ),
    pc.ISSUE_DISK: _(
        "Disk requests are slow. Check the I/O inside the guest; if it is "
        "low, tell your operator."
    ),
}

#: Icons and label classes of the assessments.
ASSESSMENT_STYLE = {
    pc.UNDERSIZED: ("pc-tag-crit", "fa-arrow-up"),
    pc.OVERSIZED: ("pc-tag-blue", "fa-arrow-down"),
    pc.RIGHT_SIZED: ("pc-tag-ok", "fa-check"),
}


def _cache_key(request, name):
    return cache.project_cache_key(
        request, CACHE_NAMESPACE, name, version=CACHE_VERSION
    )


def _check_datasource_settings():
    """
    Raise ConfigurationError for settings read when the client is built.

    The client is built inside shared computations, and only the request
    that runs a computation sees its cause: checking first gives every
    section (and the index) the same message.
    """
    if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
        config.get_openstack_keystone_url()


def instance_metrics_base():
    """The Instance metrics page, or None when that panel is absent."""
    try:
        return reverse("horizon:project:instance_metrics:index")
    except NoReverseMatch:
        return None


def metrics_link(base, instance_id):
    """Link to one instance on the Instance metrics page, or None."""
    if not base or not instance_id:
        return None
    return f"{base}?{urllib.parse.urlencode({'instance': instance_id})}"


def _instance_page(name, instance_id):
    try:
        return reverse(f"horizon:project:instances:{name}", args=[instance_id])
    except NoReverseMatch:
        return None


def instance_url(instance_id):
    """Project > Compute > Instances detail page, or None."""
    return _instance_page("detail", instance_id)


def resize_url(instance_id):
    """The Resize form of Project > Compute > Instances, or None."""
    return _instance_page("resize", instance_id)


class _ProjectComputeMixin:
    """Shared helpers: request parameters, cached data, errors."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Unique per response: element ids inside partials must not repeat
        # across swaps (htmx settles new elements with the old attributes,
        # which resets the size of a canvas after its chart is drawn).
        context["uid"] = uuid.uuid4().hex[:12]
        return context

    # Parameters -----------------------------------------------------------

    def get_range_key(self):
        key = self.request.GET.get("range")
        return key if key in pc.TREND_RANGES else pc.DEFAULT_TREND_RANGE

    def get_resource(self):
        resource = self.request.GET.get("resource")
        if resource in pc.TREND_RESOURCES:
            return resource
        return pc.DEFAULT_TREND_RESOURCE

    def get_group(self):
        group = self.request.GET.get("group")
        return group if group in pc.ASSESSMENTS else None

    # Data -----------------------------------------------------------------

    def matcher(self):
        """The current project's PromQL matcher (ValueError without one)."""
        return project.project_matcher(self.request)

    def get_or_compute(self, name, ttl, compute):
        """
        cache.get_or_compute with a project-scoped key.

        Values are stamped with ``stored_at`` when their computation ends:
        one older than its lifetime plus OLD_COPY_MARGIN is marked
        ``is_old_copy`` (possibly out of date: probably still being
        refreshed, not a failure).
        """
        _check_datasource_settings()
        key = _cache_key(self.request, name)

        def stamped():
            return dict(compute(), stored_at=time.time())

        value = cache.get_or_compute(key, ttl, stamped)
        stored_at = value.get("stored_at")
        if (
            not value.get("is_stale_copy")
            and stored_at is not None
            and time.time() - stored_at > ttl + OLD_COPY_MARGIN
        ):
            value = dict(value, is_old_copy=True)
        return value

    def get_client(self):
        if getattr(self, "_client", None) is None:
            self._client = get_datasource(self.request)
        return self._client

    def get_nova(self):
        """The project's servers and limits (Compute API)."""

        def compute():
            inventory = project_compute_inventory.get_inventory(self.request)
            return dict(inventory.read(), fetched_at=time.time())

        return self.get_or_compute("nova", NOVA_CACHE_TTL, compute)

    def get_snapshot(self):
        """Current values of the running instances (metrics)."""
        matcher = self.matcher()

        def compute():
            snapshot = self.get_client().get_project_compute_snapshot(matcher)
            return dict(snapshot, fetched_at=time.time())

        return self.get_or_compute("snapshot", SNAPSHOT_CACHE_TTL, compute)

    def get_rightsizing(self):
        """7-day statistics of the instances (metrics)."""
        matcher = self.matcher()

        def compute():
            stats = self.get_client().get_project_compute_rightsizing(matcher)
            return dict(stats, fetched_at=time.time())

        return self.get_or_compute(
            "rightsizing", RIGHTSIZING_CACHE_TTL, compute
        )

    def get_forecast(self):
        """Growth over the forecast window (at most the retention)."""
        matcher = self.matcher()
        days = pc.forecast_lookback_days(config.get_metrics_retention_days())

        def compute():
            value = self.get_client().get_project_compute_forecast(
                matcher, days
            )
            return dict(value, fetched_at=time.time())

        return self.get_or_compute(
            f"forecast:{days}", FORECAST_CACHE_TTL, compute
        )

    def get_trend(self, resource, range_key):
        """What running instances held of ``resource`` over a range."""
        matcher = self.matcher()

        def compute():
            now = time.time()
            trend = self.get_client().get_project_compute_trend(
                matcher, resource, range_key, at=now
            )
            return dict(trend, fetched_at=now)

        return self.get_or_compute(
            f"trend:{range_key}:{resource}",
            TREND_CACHE_TTL[range_key],
            compute,
        )

    def get_sparks(self, instance_ids):
        """7-day CPU of the listed instances."""
        matcher = self.matcher()
        ids = sorted(instance_ids)

        def compute():
            now = time.time()
            series = self.get_client().get_project_compute_cpu_series(
                matcher, ids, at=now
            )
            return dict(series, fetched_at=now)

        return self.get_or_compute(
            f"sparks:{','.join(ids)}", SPARKS_CACHE_TTL, compute
        )

    def get_detail(self, instance_id):
        """7-day CPU and memory of one instance."""
        matcher = self.matcher()

        def compute():
            now = time.time()
            trend = self.get_client().get_project_compute_instance_trend(
                matcher, instance_id, at=now
            )
            return dict(trend, fetched_at=now)

        return self.get_or_compute(
            f"detail:{instance_id}", DETAIL_CACHE_TTL, compute
        )

    def get_optional(self, context, part, fetch):
        """
        Optional data: on DataUnavailable note it and return None.

        The section still renders; ``partial_notes`` says what is missing,
        ``<part>_unavailable`` is set and ``loading`` makes the section
        retry shortly.
        """
        try:
            value = fetch()
        except cache.DataUnavailable as exc:
            loading = isinstance(exc, cache.StillComputing)
            context[f"{part}_unavailable"] = True
            context["loading"] = context.get("loading") or loading
            context.setdefault("partial_notes", []).append(
                OPTIONAL_NOTES[part][loading]
            )
            return None
        _note_age(context, value)
        return value

    def handle_error(self, context, exc):
        if isinstance(exc, cache.ComputationFailed) and isinstance(
            exc.__cause__, config.ConfigurationError
        ):
            # raised inside a shared computation (e.g. building the client)
            exc = exc.__cause__
        if isinstance(exc, config.ConfigurationError):
            # the index shows it; log each message once, not on every
            # refresh of every partial
            if str(exc) not in _LOGGED_CONFIG_ERRORS:
                _LOGGED_CONFIG_ERRORS.add(str(exc))
                LOG.error("Invalid project compute setting: %s", exc)
            _forget_partial_state(context)
            context["error_message"] = _(
                "Not available: invalid configuration (see the top of the "
                "page)."
            )
            return context
        if isinstance(exc, cache.StillComputing):
            # another request is still computing the shared data: show a
            # loading state, the template retries shortly
            LOG.info("Project compute data not ready yet: %s", exc)
            context["loading"] = True
            context["error_message"] = _("Still loading…")
            return context
        # an error is not "loading" (no quick retry)
        _forget_partial_state(context)
        if isinstance(exc, cache.DataUnavailable):
            # grian_ui.cache already logged the failure with its traceback
            LOG.warning("Project compute data unavailable: %s", exc)
        else:
            # details (backend messages) go to the log, not to the page
            LOG.exception("Error loading project compute data: %s", exc)
        context["error_message"] = self.error_text
        return context

    #: Shown when the section's main source fails.
    error_text = _(
        "Error loading telemetry data. See the Horizon logs for details."
    )


def _note_age(context, value):
    """Flag a failed refresh (stale) or a copy being refreshed (old)."""
    context["data_stale"] = context.get("data_stale") or value.get(
        "is_stale_copy", False
    )
    context["data_old"] = context.get("data_old") or value.get(
        "is_old_copy", False
    )


def _forget_partial_state(context):
    """Drop the optional-data state of a section replaced by an error."""
    for key in list(context):
        if key.endswith("_unavailable") or key in ("loading", "partial_notes"):
            del context[key]


def _timestamp(value):
    """Epoch seconds as an integer string for the browser clocks, or None."""
    fetched_at = (value or {}).get("fetched_at")
    return str(int(fetched_at)) if fetched_at else None


_NOVA_ERROR = _(
    "Error loading the instances and quota from the Compute API. See the "
    "Horizon logs for details."
)


class ProjectComputeIndexView(_ProjectComputeMixin, generic.TemplateView):
    template_name = "project/telemetry_compute/index.html"
    page_title = _("Compute")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["project_name"] = getattr(
            self.request.user, "project_name", ""
        )
        context["refresh_seconds"] = REFRESH_SECONDS
        context["default_range"] = pc.DEFAULT_TREND_RANGE
        context["trend_resources"] = [
            {"key": key, "label": pc.QUOTA_LABELS[key]}
            for key in pc.TREND_RESOURCES
        ]
        context["default_resource"] = pc.DEFAULT_TREND_RESOURCE
        context["current_window"] = pc.CURRENT_WINDOW
        context["current_window_text"] = pc.CURRENT_WINDOW_TEXT
        context["table_limit"] = pc.TABLE_LIMIT
        context["idle_limit"] = pc.IDLE_LIMIT
        context["min_history_days"] = pc.MIN_HISTORY_DAYS
        context["forecast_critical_days"] = pc.FORECAST_CRITICAL_DAYS
        context["forecast_warning_days"] = pc.FORECAST_WARNING_DAYS
        context["instance_metrics_enabled"] = bool(instance_metrics_base())
        # every invalid setting at once, not one per page load
        errors = []
        try:
            context["thresholds"] = config.get_project_compute_thresholds()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            context["rs_thresholds"] = (
                config.get_project_rightsizing_thresholds()
            )
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        retention = config.DEFAULT_METRICS_RETENTION_DAYS
        try:
            retention = config.get_metrics_retention_days()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        context["retention_days"] = retention
        context["forecast_days"] = pc.forecast_lookback_days(retention)
        context["ranges"] = pc.available_ranges(retention)
        try:
            _check_datasource_settings()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        context["error_messages"] = errors
        return context


class SummaryView(_ProjectComputeMixin, generic.TemplateView):
    """The four cards, and the telemetry and history notices."""

    template_name = "project/telemetry_compute/_summary.html"
    error_text = _NOVA_ERROR

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_project_compute_thresholds()
            rs_thresholds = config.get_project_rightsizing_thresholds()
            retention = config.get_metrics_retention_days()
            nova = self.get_nova()
            context["nova_stale"] = nova.get("is_stale_copy", False)
            context["nova_old"] = nova.get("is_old_copy", False)
            servers, limits = nova["servers"], nova["limits"]
            rows = pc.quota_rows(servers, limits, thresholds)
            highest = pc.highest_quota(rows)
            counts = pc.server_counts(servers)
            _idle, _count, idle = pc.idle_holders(
                servers, pc.ram_limit(limits), limit=0
            )
            idle_percent = pc.percent(idle["ram"], pc.ram_limit(limits))
            if not idle["ram"]:
                idle_level = pc.LEVEL_OK
            elif (idle_percent or 0) >= thresholds["idle_ram_critical"]:
                idle_level = pc.LEVEL_CRITICAL
            else:
                idle_level = pc.LEVEL_WARNING
            context.update(
                highest=highest,
                counts=counts,
                idle=idle,
                idle_ram_text=pc.format_mib(idle["ram"]),
                idle_percent=idle_percent,
                idle_level=idle_level,
                fetched_ts=_timestamp(nova),
                retention_days=retention,
                min_history_days=pc.MIN_HISTORY_DAYS,
            )
            if highest and highest["key"] == pc.QUOTA_RAM:
                context["highest_detail"] = _(
                    "RAM (%(used)s of %(limit)s)"
                ) % {
                    "used": highest["used_text"],
                    "limit": highest["limit_text"],
                }
            elif highest:
                context["highest_detail"] = highest["label"]

            snapshot = self.get_optional(context, "metrics", self.get_snapshot)
            stats = self.get_optional(
                context, "rightsizing", self.get_rightsizing
            )
            if stats is not None:
                running = pc.running_instances(
                    servers, (snapshot or {}).get("instances", {})
                )
                assessed = pc.rightsizing_rows(
                    running,
                    stats["instances"],
                    (snapshot or {}).get("instances", {}),
                    rs_thresholds,
                )
                context["resize"] = pc.rightsizing_summary(assessed)
            forecast = self.get_optional(
                context, "forecast", self.get_forecast
            )
            if (
                forecast is not None
                and counts["running"]
                and retention >= pc.MIN_HISTORY_DAYS
            ):
                hours = forecast.get("hours")
                if hours is None or hours < pc.MIN_HISTORY_HOURS:
                    context["history_hours"] = int(hours or 0)
            # the notices replace the generic notes of this section
            context.pop("partial_notes", None)
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class QuotaView(_ProjectComputeMixin, generic.TemplateView):
    """One bar per compute quota, with its forecast."""

    template_name = "project/telemetry_compute/_quota.html"
    error_text = _NOVA_ERROR

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_project_compute_thresholds()
            retention = config.get_metrics_retention_days()
            nova = self.get_nova()
            _note_age(context, nova)
            rows = pc.quota_rows(nova["servers"], nova["limits"], thresholds)
            forecast = self.get_optional(
                context, "forecast", self.get_forecast
            )
            for row in rows:
                slope = hours = None
                if forecast is not None:
                    slope = forecast["slopes"].get(row["key"])
                    hours = forecast.get("hours")
                row["forecast"] = pc.forecast(row, slope, hours, retention)
                row["mismatch"] = row["listed"] != row["used"]
            context.update(
                rows=rows,
                retention_days=retention,
                forecast_days=pc.forecast_lookback_days(retention),
                min_history_days=pc.MIN_HISTORY_DAYS,
                fetched_ts=_timestamp(nova),
                forecast_failed=forecast is None,
                any_mismatch=any(r["mismatch"] for r in rows),
            )
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class TrendView(_ProjectComputeMixin, generic.TemplateView):
    """What running instances held over the range, against the quota."""

    template_name = "project/telemetry_compute/_trend.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        range_key, resource = self.get_range_key(), self.get_resource()
        context.update(
            trend_range=range_key,
            resource=resource,
            resource_label=pc.QUOTA_LABELS[resource],
        )
        try:
            thresholds = config.get_project_compute_thresholds()
            trend = self.get_trend(resource, range_key)
            _note_age(context, trend)
            _key, window, step = pc.trend_range(range_key)
            timestamps, values = pc.fill(
                trend["points"], window, step, trend["fetched_at"]
            )
            nova = self.get_optional(context, "nova", self.get_nova)
            quota_now = limit = None
            if nova is not None:
                row = next(
                    r
                    for r in pc.quota_rows(
                        nova["servers"], nova["limits"], thresholds
                    )
                    if r["key"] == resource
                )
                quota_now, limit = row["used"], row["limit"]
            running_now = pc.last_value(values)
            context["has_data"] = running_now is not None
            if quota_now is not None and running_now is not None:
                gap = quota_now - running_now
                if gap > 0:
                    context["gap"] = {
                        "quota": pc.format_quota(resource, quota_now),
                        "running": pc.format_quota(resource, running_now),
                        "gap": pc.format_quota(resource, gap),
                    }
            first = next(
                (i for i, v in enumerate(values) if v is not None), None
            )
            if first:
                # nothing ran, or no metrics yet, before that point
                context["data_since_ts"] = str(int(timestamps[first]))
            summary = pc.series_summary(values)
            context["summary"] = (
                {k: pc.format_quota(resource, v) for k, v in summary.items()}
                if summary
                else None
            )
            context["chart"] = {
                "range": range_key,
                "resource": resource,
                "timestamps": timestamps,
                "values": values,
                "quota_now": quota_now,
                "limit": limit,
            }
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class IdleView(_ProjectComputeMixin, generic.TemplateView):
    """Instances in ERROR or SHUTOFF, largest first."""

    template_name = "project/telemetry_compute/_idle.html"
    error_text = _NOVA_ERROR

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            nova = self.get_nova()
            _note_age(context, nova)
            rows, count, totals = pc.idle_holders(
                nova["servers"], pc.ram_limit(nova["limits"])
            )
            base = instance_metrics_base()
            for row in rows:
                row["metrics_url"] = metrics_link(base, row["id"])
                row["instance_url"] = instance_url(row["id"])
            context.update(
                rows=rows,
                count=count,
                more=count - len(rows),
                totals=totals,
                idle_limit=pc.IDLE_LIMIT,
            )
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class RightsizingView(_ProjectComputeMixin, generic.TemplateView):
    """Running instances against their flavor (7 days)."""

    template_name = "project/telemetry_compute/_rightsizing.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        group = self.get_group()
        context.update(group=group, table_limit=pc.TABLE_LIMIT)
        try:
            thresholds = config.get_project_rightsizing_thresholds()
            context["thresholds"] = thresholds
            stats = self.get_rightsizing()
            _note_age(context, stats)
            snapshot = self.get_optional(context, "metrics", self.get_snapshot)
            nova = self.get_optional(context, "nova", self.get_nova)
            current = (snapshot or {}).get("instances", {})
            running = pc.running_instances(
                nova["servers"] if nova is not None else None, current
            )
            rows = pc.rightsizing_rows(
                running, stats["instances"], current, thresholds
            )
            summary = pc.rightsizing_summary(rows)
            shown = [
                r for r in rows if group is None or r["assessment"] == group
            ]
            shown = shown[: pc.TABLE_LIMIT]
            base = instance_metrics_base()
            for row in shown:
                row["metrics_url"] = metrics_link(base, row["id"])
                if row["assessment"]:
                    css, icon = ASSESSMENT_STYLE[row["assessment"]]
                    row.update(
                        tag_class=css,
                        tag_icon=icon,
                        label=pc.ASSESSMENT_LABELS[row["assessment"]],
                    )
            context.update(
                rows=shown,
                summary=summary,
                total=len(rows),
                matching=(
                    len(rows) if group is None else summary["counts"][group]
                ),
                filters=[
                    {"key": "", "label": _("All"), "count": len(rows)},
                ]
                + [
                    {
                        "key": key,
                        "label": pc.ASSESSMENT_LABELS[key],
                        "count": summary["counts"][key],
                    }
                    for key in pc.ASSESSMENTS
                ],
                fetched_ts=_timestamp(stats),
            )
            context["sparks"] = {}
            ids = [r["id"] for r in shown if r["assessment"]]
            if ids:
                found = self.get_optional(
                    context, "sparks", lambda: self.get_sparks(ids)
                )
                if found:
                    for row in shown:
                        points = found["series"].get(row["id"])
                        if not points:
                            continue
                        values = pc.fill(
                            points,
                            pc.RIGHTSIZING_WINDOW,
                            pc.SPARK_STEP,
                            found["fetched_at"],
                        )[1]
                        context["sparks"][row["id"]] = {
                            "values": values,
                            "max": row["vcpus"],
                            "busy": row["cpu_level"] == "busy",
                        }
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class InstanceView(_ProjectComputeMixin, generic.TemplateView):
    """One rightsizing row opened: CPU and memory against the flavor."""

    template_name = "project/telemetry_compute/_instance.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        instance_id = self.request.GET.get("instance") or ""
        context["instance_id"] = instance_id
        if not queries.is_instance_id(instance_id):
            context["invalid"] = True
            return context
        context["metrics_url"] = metrics_link(
            instance_metrics_base(), instance_id
        )
        context["resize_url"] = resize_url(instance_id)
        try:
            thresholds = config.get_project_rightsizing_thresholds()
            detail = self.get_detail(instance_id)
            _note_age(context, detail)
            snapshot = self.get_optional(context, "metrics", self.get_snapshot)
            nova = self.get_optional(context, "nova", self.get_nova)
            stats = self.get_optional(
                context, "rightsizing", self.get_rightsizing
            )
            current = (snapshot or {}).get("instances", {})
            running = pc.running_instances(
                nova["servers"] if nova is not None else None, current
            )
            instance = next(
                (i for i in running if i["id"] == instance_id), None
            )
            if instance is None:
                metrics = current.get(instance_id) or {}
                instance = {
                    "id": instance_id,
                    "name": metrics.get("name") or instance_id,
                    "vcpus": metrics.get("vcpus"),
                    "ram": metrics.get("memory_alloc"),
                }
            context["name"] = instance["name"]
            if stats is not None:
                context["assessment"] = pc.assess(
                    stats["instances"].get(instance_id),
                    instance["vcpus"],
                    instance["ram"],
                    thresholds,
                )
            fetched_at = detail["fetched_at"]
            window, step = pc.RIGHTSIZING_WINDOW, pc.DETAIL_STEP
            timestamps, cpu = pc.fill(detail["cpu"], window, step, fetched_at)
            memory = pc.fill(detail["memory"], window, step, fetched_at)[1]
            context["has_data"] = any(v is not None for v in cpu + memory)
            cpu_summary = pc.series_summary(cpu)
            memory_summary = pc.series_summary(memory)
            context["cpu_summary"] = (
                {k: pc.format_cores(v) for k, v in cpu_summary.items()}
                if cpu_summary
                else None
            )
            context["memory_summary"] = (
                {k: pc.format_mib(v) for k, v in memory_summary.items()}
                if memory_summary
                else None
            )
            context["chart"] = {
                "timestamps": timestamps,
                "cpu": cpu,
                "memory": memory,
                "vcpus": instance["vcpus"],
                "ram": instance["ram"],
            }
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class AttentionView(_ProjectComputeMixin, generic.TemplateView):
    """Instances in ERROR or with symptoms, worst first."""

    template_name = "project/telemetry_compute/_attention.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["table_limit"] = pc.TABLE_LIMIT
        context["current_window_text"] = pc.CURRENT_WINDOW_TEXT
        try:
            thresholds = config.get_project_compute_thresholds()
            nova = self.get_optional(context, "nova", self.get_nova)
            snapshot = self.get_optional(context, "metrics", self.get_snapshot)
            if nova is None and snapshot is None:
                # nothing to show: report the failure (or keep loading)
                if context.get("loading"):
                    raise cache.StillComputing("both sources are loading")
                raise cache.ComputationFailed("both sources failed")
            rows, count = pc.attention_rows(
                nova["servers"] if nova is not None else None,
                snapshot["instances"] if snapshot is not None else None,
                thresholds,
            )
            base = instance_metrics_base()
            for row in rows:
                row["metrics_url"] = metrics_link(base, row["id"])
                row["advice_text"] = (
                    ADVICE[row["advice"]] if row["advice"] else ""
                )
            context.update(rows=rows, count=count, more=count - len(rows))
        except Exception as exc:
            self.handle_error(context, exc)
        return context
