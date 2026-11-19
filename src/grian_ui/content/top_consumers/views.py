#
# SPDX-License-Identifier: Apache-2.0

"""
Top consumers views.

The index page is a light shell; every section is an HTMX partial that
loads and refreshes itself, and renders a bounded amount of data (four
cards, at most TOP_LIMIT rows, CHART_SERIES series plus "Other") whatever
the number of projects and instances.

The per-project snapshot, the top instance lists and the trends come from
Prometheus and follow the page refresh. The only other service called is
Keystone, for the project names: its project list is a separate shared
value, refreshed at most every PROJECT_NAMES_CACHE_TTL seconds per region
whatever the number of administrators and refreshes, and it never fails a
section (project IDs are shown). Every value is cached in the Horizon cache
and shared by the partials and by the administrators of a region (see
grian_ui.cache); each process joins the metrics with the names once per
pair of inputs.
"""

import collections
import logging
import threading
import time
import uuid

from django.core.cache import cache as django_cache
from django.urls import NoReverseMatch
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import generic

from grian_ui import cache
from grian_ui import config
from grian_ui import timeseries
from grian_ui import top_consumers as tc
from grian_ui.datasource import prometheus_top_consumers as queries
from grian_ui.datasource.api import get_datasource


LOG = logging.getLogger(__name__)

#: Refresh interval of the partials, in seconds (used by the templates).
REFRESH_SECONDS = 60
#: Seconds the snapshot and the instance lists are shared. Slightly shorter
#: than the refresh interval so every refresh gets new data, computed once.
SNAPSHOT_CACHE_TTL = REFRESH_SECONDS - 5
#: Seconds the trends are shared, per range (coarser ranges change slower).
TREND_CACHE_TTL = {"1h": 55, "6h": 60, "24h": 300, "7d": 900}
#: Seconds the Keystone project list (project names) is shared: Keystone is
#: called at most once per this interval and region, also when it fails.
PROJECT_NAMES_CACHE_TTL = 300
#: How long the last project names Keystone returned are kept, to show
#: while it fails.
PROJECT_NAMES_KEPT_SECONDS = 86400
#: A copy older than its lifetime plus this margin is flagged "possibly out
#: of date" (still being refreshed, not a failure).
OLD_COPY_MARGIN = 30
#: Joins of the metrics with the project names kept per process.
JOINS_KEPT = 32
#: Bump when the format of a cached value changes (see
#: grian_ui.cache.cache_key).
CACHE_VERSION = 2
CACHE_NAMESPACE = "top_consumers"

#: Icons of the resource buttons (Font Awesome 4).
RESOURCE_ICONS = {
    tc.RESOURCE_CPU: "fa-microchip",
    tc.RESOURCE_MEMORY: "fa-server",
    tc.RESOURCE_DISK: "fa-hdd-o",
    tc.RESOURCE_NETWORK: "fa-exchange",
}

_LOGGED_CONFIG_ERRORS = set()

# Notes for optional data that is not available: (failed, still loading)
OPTIONAL_NOTES = {
    "sparks": (
        _("The CPU trends could not be loaded."),
        _("Loading the CPU trends…"),
    ),
}


def _cache_key(request, name):
    return cache.cache_key(
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


_JOINS = collections.OrderedDict()
_JOINS_LOCK = threading.Lock()


def list_project_names(request):
    """Project id -> name, from Keystone (or the fake datasource)."""
    if config.get_datasource_type() == config.DATASOURCE_FAKE:
        from grian_ui.datasource import fake_top_consumers

        return dict(fake_top_consumers.FAKE_PROJECT_NAMES)
    # imported lazily: unit tests run without horizon
    from openstack_dashboard.api import keystone

    projects, _more = keystone.tenant_list(request, admin=True)
    return {p.id: p.name for p in projects}


def instance_url(instance_id):
    """Admin instance detail page, or None without that panel."""
    try:
        return reverse("horizon:admin:instances:detail", args=[instance_id])
    except NoReverseMatch:
        return None


class _TopConsumersMixin:
    """Shared helpers: request parameters, cached data, errors."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Unique per response: element ids inside partials must not repeat
        # across swaps (htmx settles new elements with the old attributes,
        # which resets the size of a canvas after its chart is drawn).
        context["uid"] = uuid.uuid4().hex[:12]
        return context

    # Parameters -----------------------------------------------------------

    def get_resource(self):
        resource = self.request.GET.get("resource")
        return resource if resource in tc.RESOURCES else tc.RESOURCE_CPU

    def get_by(self):
        by = self.request.GET.get("by")
        return by if by in tc.BY_CHOICES else tc.BY_PROJECT

    def get_range_key(self):
        key = self.request.GET.get("range")
        return key if key in tc.TREND_RANGES else tc.DEFAULT_TREND_RANGE

    def get_project_filter(self):
        """Project whose instances are listed (by instance only), or None."""
        if self.get_by() != tc.BY_INSTANCE:
            return None
        project_id = (self.request.GET.get("project") or "").strip()
        return project_id if queries.is_project_id(project_id) else None

    # Data -----------------------------------------------------------------

    def get_or_compute(self, name, ttl, compute):
        """
        cache.get_or_compute, after checking the datasource settings.

        The cache marks a last good copy served after a failure, not one
        served while another request is still refreshing it (a slow or
        hanging backend). Values are stamped with ``stored_at`` when their
        computation ends: one older than its lifetime plus OLD_COPY_MARGIN
        is marked ``is_old_copy`` here (possibly out of date: probably
        still being refreshed, not a failure). (The age compares the
        clocks of the Horizon nodes sharing the cache: keep them in sync.)
        """
        _check_datasource_settings()

        def stamped():
            return dict(compute(), stored_at=time.time())

        value = cache.get_or_compute(
            _cache_key(self.request, name), ttl, stamped
        )
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

    def get_snapshot(self):
        """Use and allocation of every project with running instances."""

        def compute():
            snapshot = self.get_client().get_top_consumers_projects()
            return dict(snapshot, fetched_at=time.time())

        return self.get_or_compute("projects", SNAPSHOT_CACHE_TTL, compute)

    def get_project_names(self):
        """
        The Keystone project list, shared per region.

        Computed once per PROJECT_NAMES_CACHE_TTL by one request of the
        region, never per refresh. A Keystone failure is cached like a
        success, so Keystone is not called more often while it fails: the
        value then holds the last names it returned (or none) and
        ``failed``. Never fails the section: without names (Keystone
        failing, or the first computation still running) the IDs are shown.

        Returns:
            dict: ``names`` (id -> name), ``fetched_at`` (when Keystone
            returned them, or None), ``failed``, ``loading``,
            ``is_old_copy``, ``stored_at``
        """
        kept_key = _cache_key(self.request, "project_names_kept")

        def compute():
            try:
                names = list_project_names(self.request)
            except Exception:
                LOG.exception(
                    "Could not list the projects in Keystone; trying again "
                    "in %s seconds",
                    PROJECT_NAMES_CACHE_TTL,
                )
                kept = _cache_read(kept_key) or {}
                return {
                    "names": kept.get("names", {}),
                    "fetched_at": kept.get("fetched_at"),
                    "failed": True,
                }
            value = {"names": names, "fetched_at": time.time()}
            _cache_write(kept_key, value, PROJECT_NAMES_KEPT_SECONDS)
            return dict(value, failed=False)

        try:
            return self.get_or_compute(
                "project_names", PROJECT_NAMES_CACHE_TTL, compute
            )
        except cache.DataUnavailable as exc:
            loading = isinstance(exc, cache.StillComputing)
            LOG.info("Project names not available: %s", exc)
            return {
                "names": {},
                "fetched_at": None,
                "failed": not loading,
                "loading": loading,
                "stored_at": None,
            }

    def get_instances(self, resource, project_id):
        """The top instances of ``resource`` (of one project, or all)."""

        def compute():
            return {
                "instances": self.get_client().get_top_consumers_instances(
                    resource, project_id=project_id, limit=tc.TOP_LIMIT
                )
            }

        return self.get_or_compute(
            f"instances:{resource}:{project_id or '*'}",
            SNAPSHOT_CACHE_TTL,
            compute,
        )

    def get_trend(self, resource, by, keys, project_id, range_key):
        """
        Trend of ``keys`` and the total over ``range_key``.

        Returns:
            dict: ``{"series": ..., "total": ..., "fetched_at": epoch}``;
            grids are built from ``fetched_at``, the evaluation time
        """
        keys = list(keys)

        def compute():
            now = time.time()
            trend = self.get_client().get_top_consumers_trend(
                resource,
                by,
                keys,
                project_id=project_id,
                range_key=range_key,
                at=now,
            )
            return dict(trend, fetched_at=now)

        return self.get_or_compute(
            f"trend:{range_key}:{resource}:{by}:{project_id or '*'}:"
            f"{','.join(keys)}",
            TREND_CACHE_TTL[range_key],
            compute,
        )

    def get_optional(self, context, part, fetch):
        """
        Optional data: on DataUnavailable note it and return None.

        The section still renders; ``partial_notes`` says what is missing
        and ``loading`` makes the section retry shortly.
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

    def get_rows(self, context, resource, by, project_id, thresholds):
        """
        Ranked rows of the table (and of the chart), with the scope.

        Fills ``context`` with the scope (``total``, ``count``, project
        filter), the age of the data it read and the state of the project
        names. The join of the metrics with the names runs once per
        process for each pair of inputs.

        Returns:
            list: at most TOP_LIMIT rows (new dicts), biggest first
        """
        snapshot = self.get_snapshot()
        names_value = self.get_project_names()
        context["fetched_at"] = snapshot.get("fetched_at")
        context["has_instances"] = bool(snapshot.get("projects"))
        _note_age(context, snapshot)
        _note_names(context, names_value)
        found = None
        if by == tc.BY_INSTANCE and (
            project_id is None or tc.find_project(snapshot, project_id)
        ):
            found = self.get_instances(resource, project_id)
            _note_age(context, found)

        inputs = (snapshot, found, names_value)
        key = None
        if all(v is None or v.get("stored_at") for v in inputs):
            key = (
                _cache_key(self.request, "rows"),
                resource,
                by,
                project_id,
                tuple(sorted(thresholds.items())),
            ) + tuple(v.get("stored_at") if v else None for v in inputs)
        joined = None
        if key is not None:
            with _JOINS_LOCK:
                joined = _JOINS.get(key)
        if joined is None:
            joined = join_rows(
                snapshot,
                found,
                names_value["names"],
                resource,
                by,
                project_id,
                thresholds,
            )
            if key is not None:
                with _JOINS_LOCK:
                    _JOINS[key] = joined
                    while len(_JOINS) > JOINS_KEPT:
                        _JOINS.popitem(last=False)
        context.update(joined["scope"])
        rows = [dict(row) for row in joined["rows"]]
        if by == tc.BY_INSTANCE:
            for row in rows:
                row["url"] = instance_url(row["id"])
        return rows

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
                LOG.error("Invalid top consumers setting: %s", exc)
            _forget_partial_state(context)
            context["error_message"] = _(
                "Not available: invalid configuration (see the top of the "
                "page)."
            )
            return context
        if isinstance(exc, cache.StillComputing):
            # another request is still computing the shared data: show a
            # loading state, the template retries shortly
            LOG.info("Top consumers data not ready yet: %s", exc)
            context["loading"] = True
            context["error_message"] = _("Still loading telemetry data…")
            return context
        # an error is not "loading" (no quick retry)
        _forget_partial_state(context)
        if isinstance(exc, cache.DataUnavailable):
            # grian_ui.cache already logged the failure with its traceback
            LOG.warning("Top consumers data unavailable: %s", exc)
        else:
            # details (backend messages) go to the log, not to the page
            LOG.exception("Error loading top consumers data: %s", exc)
        context["error_message"] = _(
            "Error loading telemetry data. See the Horizon logs for details."
        )
        return context


def join_rows(snapshot, found, names, resource, by, project_id, thresholds):
    """
    Join the metrics with the project names: the table rows and scope.

    Args:
        found: the top instances value (by instance), or None

    Returns:
        dict: ``rows`` and ``scope`` (``count``, ``total`` and, by
        project, ``idle_projects`` and ``idle_more``; by instance of a
        project, ``project_filter``)
    """
    if by == tc.BY_PROJECT:
        rows, count, total = tc.top_projects(
            snapshot, resource, names, thresholds
        )
        idle, idle_count = tc.idle_projects(snapshot, names)
        scope = {
            "count": count,
            "total": total,
            "idle_projects": idle,
            "idle_more": idle_count - len(idle),
        }
        return {"rows": rows, "scope": scope}

    scope = {}
    if project_id is not None:
        project = tc.find_project(snapshot, project_id)
        scope["project_filter"] = {
            "id": project_id,
            "label": names.get(project_id) or project_id,
        }
        if project is None:
            # nothing running in that project (any more)
            scope.update(count=0, total=None)
            return {"rows": [], "scope": scope}
        total = tc.resource_value(project, resource)
        count = project.get("instances") or 0
    else:
        cloud = tc.totals(snapshot)
        total, count = cloud[resource], cloud["instances"] or 0
    rows = tc.instance_rows(
        found["instances"] if found else [], resource, total, names, thresholds
    )
    scope.update(count=max(count, len(rows)), total=total)
    return {"rows": rows, "scope": scope}


def _note_age(context, value):
    """Flag a failed refresh (stale) or a copy being refreshed (old)."""
    context["data_stale"] = context.get("data_stale") or value.get(
        "is_stale_copy", False
    )
    context["data_old"] = context.get("data_old") or value.get(
        "is_old_copy", False
    )


def _note_names(context, value):
    """State of the Keystone project names, for the footer notes."""
    fetched_at = value.get("fetched_at")
    context.update(
        names_missing=not value["names"],
        names_failed=value.get("failed", False),
        names_loading=value.get("loading", False),
        names_old=value.get("is_old_copy", False)
        or value.get("is_stale_copy", False),
        names_ts=str(int(fetched_at)) if fetched_at else None,
        names_minutes=PROJECT_NAMES_CACHE_TTL // 60,
    )


def _cache_read(key):
    try:
        return django_cache.get(key)
    except Exception as exc:
        LOG.debug("Could not read %s from the cache: %s", key, exc)
        return None


def _cache_write(key, value, ttl):
    try:
        django_cache.set(key, value, ttl)
    except Exception as exc:
        LOG.debug("Could not cache %s: %s", key, exc)


def _forget_partial_state(context):
    """Drop the optional-data state of a section replaced by an error."""
    for key in list(context):
        if key.endswith(("_unavailable", "_loading")) or key in (
            "loading",
            "partial_notes",
        ):
            del context[key]


def _with_colors(rows):
    """Colour index of the rows drawn in the chart (None for the others)."""
    for i, row in enumerate(rows):
        row["color"] = i if i < tc.CHART_SERIES else None
    return rows


def _base_context(view, context):
    resource, by = view.get_resource(), view.get_by()
    context.update(
        resource=resource,
        resource_label=tc.RESOURCE_LABELS[resource],
        by=by,
        has_allocation=resource in tc.ALLOCATION_FIELDS,
        top_limit=tc.TOP_LIMIT,
        chart_series=tc.CHART_SERIES,
        trend_range=view.get_range_key(),
        current_window=tc.CURRENT_WINDOW,
    )
    return resource, by, view.get_project_filter()


def _range_step(range_key):
    _window, step, rate_window = tc.trend_window(range_key)
    return _("%(range)s: every %(step)s, rates over %(window)s") % {
        "range": range_key,
        "step": step,
        "window": rate_window,
    }


class TopConsumersIndexView(_TopConsumersMixin, generic.TemplateView):
    template_name = "admin/top_consumers/index.html"
    page_title = _("Top Consumers")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["resources"] = [
            {
                "key": key,
                "label": tc.RESOURCE_LABELS[key],
                "icon": RESOURCE_ICONS[key],
            }
            for key in tc.RESOURCES
        ]
        context["default_resource"] = tc.RESOURCE_CPU
        context["default_by"] = tc.BY_PROJECT
        context["refresh_seconds"] = REFRESH_SECONDS
        context["current_window"] = tc.CURRENT_WINDOW
        context["ranges"] = list(tc.TREND_RANGES)
        context["default_range"] = tc.DEFAULT_TREND_RANGE
        # for "How this page is calculated"
        context["range_steps"] = [_range_step(key) for key in tc.TREND_RANGES]
        context["names_minutes"] = PROJECT_NAMES_CACHE_TTL // 60
        context["top_limit"] = tc.TOP_LIMIT
        context["chart_series"] = tc.CHART_SERIES
        # every invalid setting at once, not one per page load
        errors = []
        try:
            context["thresholds"] = config.get_top_consumers_thresholds()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            # read when the datasource client is built: shown here, once
            _check_datasource_settings()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        context["error_messages"] = errors
        return context


class SummaryView(_TopConsumersMixin, generic.TemplateView):
    """Cloud-wide totals: CPU, memory, disk I/O, network."""

    template_name = "admin/top_consumers/_summary.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            snapshot = self.get_snapshot()
            totals = tc.totals(snapshot)
            context["totals"] = totals
            context["cards"] = {
                "cpu": tc.format_value(tc.RESOURCE_CPU, totals["cpu"]),
                "vcpus": tc.format_allocated(tc.RESOURCE_CPU, totals["vcpus"]),
                "cpu_ratio": tc.ratio_percent(totals["cpu"], totals["vcpus"]),
                "memory": tc.format_value(
                    tc.RESOURCE_MEMORY, totals["memory"]
                ),
                "memory_alloc": tc.format_allocated(
                    tc.RESOURCE_MEMORY, totals["memory_alloc"]
                ),
                "memory_ratio": tc.ratio_percent(
                    totals["memory"], totals["memory_alloc"]
                ),
                "disk": tc.format_value(tc.RESOURCE_DISK, totals["disk"]),
                "disk_read": tc.format_value(
                    tc.RESOURCE_DISK, totals["disk_read"]
                ),
                "disk_write": tc.format_value(
                    tc.RESOURCE_DISK, totals["disk_write"]
                ),
                "network": tc.format_value(
                    tc.RESOURCE_NETWORK, totals["network"]
                ),
                "net_in": tc.format_value(
                    tc.RESOURCE_NETWORK, totals["net_in"]
                ),
                "net_out": tc.format_value(
                    tc.RESOURCE_NETWORK, totals["net_out"]
                ),
            }
            context["current_window"] = tc.CURRENT_WINDOW
            context["fetched_at"] = snapshot.get("fetched_at")
            context["fetched_ts"] = str(int(snapshot.get("fetched_at") or 0))
            _note_age(context, snapshot)
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class TopTableView(_TopConsumersMixin, generic.TemplateView):
    """At most TOP_LIMIT projects or instances for the chosen resource."""

    template_name = "admin/top_consumers/_top.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        resource, by, project_id = _base_context(self, context)
        try:
            thresholds = config.get_top_consumers_thresholds()
            context["thresholds"] = thresholds
            rows = _with_colors(
                self.get_rows(context, resource, by, project_id, thresholds)
            )
            context["rows"] = rows
            context["sparks"] = {}
            if by == tc.BY_PROJECT and rows:
                # CPU over the range per listed project: optional, the
                # table renders without it
                keys = sorted(r["key"] for r in rows)
                range_key = context["trend_range"]
                found = self.get_optional(
                    context,
                    "sparks",
                    lambda: self.get_trend(
                        tc.RESOURCE_CPU, by, keys, None, range_key
                    ),
                )
                if found:
                    for row in rows:
                        values = timeseries.fill_gaps(
                            found["series"].get(row["key"], []),
                            range_key,
                            found["fetched_at"],
                        )[1]
                        row["spark_summary"] = _spark_summary(values)
                        context["sparks"][row["key"]] = values
        except Exception as exc:
            self.handle_error(context, exc)
        return context


def _spark_summary(values):
    summary = tc.trend_summary(values)
    if summary is None:
        return None
    return {
        key: tc.format_value(tc.RESOURCE_CPU, value)
        for key, value in summary.items()
    }


class TrendView(_TopConsumersMixin, generic.TemplateView):
    """Stacked chart: the top CHART_SERIES rows plus "Other"."""

    template_name = "admin/top_consumers/_trend.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        resource, by, project_id = _base_context(self, context)
        try:
            thresholds = config.get_top_consumers_thresholds()
            rows = self.get_rows(context, resource, by, project_id, thresholds)
            if not rows:
                # nothing running (in the cloud, or in the project)
                context["has_data"] = False
                return context
            drawn = rows[: tc.CHART_SERIES]
            keys = [r["key"] for r in drawn]
            range_key = context["trend_range"]
            trend = self.get_trend(
                resource, by, sorted(keys), project_id, range_key
            )
            _note_age(context, trend)
            fetched_at = trend["fetched_at"]
            grid, total = timeseries.fill_gaps(
                trend["total"], range_key, fetched_at
            )
            series = {
                key: timeseries.fill_gaps(points, range_key, fetched_at)[1]
                for key, points in trend["series"].items()
            }
            labels = {r["key"]: r["label"] for r in drawn}
            stacked = tc.stack_with_other(series, total, keys)
            datasets = []
            for i, (key, values) in enumerate(stacked):
                datasets.append(
                    {
                        "label": (
                            str(_("Other"))
                            if key == tc.OTHER_KEY
                            else labels[key]
                        ),
                        "color": None if key == tc.OTHER_KEY else i,
                        "values": values,
                    }
                )
            context["has_data"] = any(v is not None for v in total)
            summary = tc.trend_summary(total)
            context["total_summary"] = (
                {
                    key: tc.format_value(resource, value)
                    for key, value in summary.items()
                }
                if summary
                else None
            )
            context["chart"] = {
                "range": range_key,
                "timestamps": grid,
                "unit": tc.RESOURCE_UNITS[resource],
                "datasets": datasets,
            }
        except Exception as exc:
            self.handle_error(context, exc)
        return context
