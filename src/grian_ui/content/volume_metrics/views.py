"""
Storage Health views (the Volume Metrics panel).

The pool capacity, volume and snapshot counts are read once per refresh
(one query per metric, whatever the number of pools) and the capacity
forecasts every FORECAST_CACHE_TTL seconds. Volumes and snapshots needing
attention come from the Block Storage API, read with the administrator's
token (grian_ui.storage_inventory) every INVENTORY_CACHE_TTL seconds: page
refreshes never call Cinder themselves, they read that shared summary.

Each value is computed once and shared through the Horizon cache by the
page, its HTMX partials and the administrators of a region (see
grian_ui.cache), and every partial renders a bounded amount of data. When
a refresh fails, the last good copy is shown with the time it was fetched;
error details go to the Horizon log, not to the page.
"""

import logging
import time
import uuid

from django.urls import NoReverseMatch
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import generic

from grian_ui import cache
from grian_ui import config
from grian_ui import storage_health
from grian_ui import storage_inventory
from grian_ui.datasource.api import get_datasource


LOG = logging.getLogger(__name__)

#: Refresh interval of the partials, in seconds (used by the templates).
REFRESH_SECONDS = 60
#: Seconds the pool snapshot (capacity, volume and snapshot counts) is
#: shared. Slightly shorter than the refresh interval so every refresh
#: gets new data, computed once.
POOLS_CACHE_TTL = REFRESH_SECONDS - 5
#: Seconds the capacity forecasts (7-day trends, which change slowly) are
#: shared.
FORECAST_CACHE_TTL = 300
#: A shared value older than its lifetime plus this many seconds is flagged
#: as possibly out of date (the rule of the other Telemetry dashboards).
OLD_COPY_GRACE_SECONDS = 30
#: Seconds the Block Storage API summary (every volume and snapshot of
#: every project, two paginated listings) is shared: state changes show up
#: to this late.
INVENTORY_CACHE_TTL = 120
#: Seconds the project names (Identity API) are shared.
PROJECTS_CACHE_TTL = 300
#: Seconds the 24-hour Cinder service history and the image and backup
#: totals are shared.
HISTORY_CACHE_TTL = 300
IMAGES_CACHE_TTL = 300
#: Seconds each capacity trend is shared, per range.
TREND_CACHE_TTL = {"24h": 60, "7d": 300, "14d": 600}
#: Bump when the format of a cached value changes (see
#: grian_ui.cache.cache_key).
CACHE_VERSION = 2
CACHE_NAMESPACE = "volume_metrics"

_LOGGED_CONFIG_ERRORS = set()

# Notes for optional data that is not available: (failed, still loading)
OPTIONAL_NOTES = {
    "forecast": (
        _("The capacity forecasts could not be loaded."),
        _("Loading the capacity forecasts…"),
    ),
    "inventory": (
        _(
            "The Block Storage API data could not be loaded: volumes and "
            "snapshots needing attention are not checked."
        ),
        _("Loading the volumes and snapshots from the Block Storage API…"),
    ),
    "projects": (
        _("Project names could not be loaded: showing project IDs."),
        _("Loading the project names…"),
    ),
    "history": (
        _("The 24-hour service history could not be loaded."),
        _("Loading the 24-hour service history…"),
    ),
}


def _check_settings():
    """
    Raise ConfigurationError for settings read inside shared computations.

    Only the request that runs a computation sees its cause: checking first
    gives every section the same message.
    """
    config.get_storage_thresholds()
    config.get_storage_stale_minutes()
    if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
        # read when the client is built
        config.get_openstack_keystone_url()


def _detail_url(kind, resource_id):
    """Admin > Volume detail page of a volume or snapshot (None outside
    Horizon)."""
    name = "snapshots" if kind == "snapshot" else "volumes"
    try:
        return reverse(f"horizon:admin:{name}:detail", args=[resource_id])
    except NoReverseMatch:
        return None


class _VolumeMetricsMixin:
    """Shared helpers: cached data, data age, errors."""

    #: Shown when the section's data cannot be loaded.
    error_text = _("Error loading telemetry data. See the Horizon logs.")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # unique per response: element ids inside partials must not repeat
        # across swaps
        context["uid"] = uuid.uuid4().hex[:12]
        context["refresh_seconds"] = REFRESH_SECONDS
        return context

    def get_client(self):
        if getattr(self, "_client", None) is None:
            self._client = get_datasource(self.request)
        return self._client

    def get_or_compute(self, name, ttl, compute):
        """
        cache.get_or_compute, stamping values with ``stored_at``.

        The cache marks a last good copy served after a failure
        (``is_stale_copy``), not one served while another request is still
        refreshing it: a copy older than its lifetime plus
        OLD_COPY_GRACE_SECONDS is marked ``is_old_copy`` here, possibly out
        of date.
        (The age compares the clocks of the Horizon nodes sharing the
        cache: keep them in sync.)
        """
        _check_settings()

        def stamped():
            return dict(compute(), stored_at=time.time())

        value = cache.get_or_compute(
            cache.cache_key(
                self.request, CACHE_NAMESPACE, name, version=CACHE_VERSION
            ),
            ttl,
            stamped,
        )
        stored_at = value.get("stored_at")
        if (
            not value.get("is_stale_copy")
            and stored_at is not None
            and time.time() - stored_at > ttl + OLD_COPY_GRACE_SECONDS
        ):
            value = dict(value, is_old_copy=True)
        return value

    def get_snapshot(self):
        """Pools, volume and snapshot counts, shared for POOLS_CACHE_TTL."""

        def compute():
            client = self.get_client()
            return {
                "pools": client.get_storage_pools(),
                "volumes": client.get_volumes_count(),
                "snapshots": client.get_snapshots_count(),
                "services": client.get_volume_services(),
                "fetched_at": time.time(),
            }

        return self.get_or_compute("pools", POOLS_CACHE_TTL, compute)

    def get_forecast(self):
        """Capacity forecasts, shared for FORECAST_CACHE_TTL."""

        def compute():
            return {
                "predictions": self.get_client().get_capacity_predictions(),
                "fetched_at": time.time(),
            }

        return self.get_or_compute("forecast", FORECAST_CACHE_TTL, compute)

    def get_service_history(self):
        """The 24-hour Cinder service history, shared for
        HISTORY_CACHE_TTL."""

        def compute():
            now = time.time()
            return {
                "history": self.get_client().get_volume_service_history(
                    at=now
                ),
                "fetched_at": now,
            }

        return self.get_or_compute("history", HISTORY_CACHE_TTL, compute)

    def get_images(self):
        """Image store and backup totals, shared for IMAGES_CACHE_TTL."""

        def compute():
            return dict(
                self.get_client().get_storage_images(), fetched_at=time.time()
            )

        return self.get_or_compute("images", IMAGES_CACHE_TTL, compute)

    def get_capacity_trend(self, range_key, view, pools):
        """
        Capacity over a range, shared per range and view.

        ``pools`` (evaluated rows) choose the fullest pools when the trend
        is computed.
        """

        def compute():
            names = (
                storage_health.fullest_pools(pools)
                if view == storage_health.VIEW_POOLS
                else None
            )
            # evaluated at the time the chart grid is built from
            now = time.time()
            series = {}
            if names or view == storage_health.VIEW_CLOUD:
                series = self.get_client().get_storage_capacity_trend(
                    range_key, pools=names, at=now
                )
            return {"series": series, "names": names, "fetched_at": now}

        return self.get_or_compute(
            f"trend:{view}:{range_key}", TREND_CACHE_TTL[range_key], compute
        )

    def get_storage_inventory(self):
        if getattr(self, "_inventory", None) is None:
            self._inventory = storage_inventory.get_inventory(self.request)
        return self._inventory

    def get_inventory(self):
        """
        Volumes and snapshots needing attention, shared for
        INVENTORY_CACHE_TTL: a bounded summary of every volume and snapshot.
        """

        def compute():
            inventory = self.get_storage_inventory()
            volumes = inventory.list_volumes()
            snapshots = inventory.list_snapshots()
            now = time.time()
            return dict(
                storage_health.summarize_inventory(
                    volumes, snapshots, config.get_storage_thresholds(), now
                ),
                fetched_at=now,
            )

        return self.get_or_compute("inventory", INVENTORY_CACHE_TTL, compute)

    def get_projects(self):
        def compute():
            return {"names": self.get_storage_inventory().project_names()}

        return self.get_or_compute("projects", PROJECTS_CACHE_TTL, compute)

    def get_optional(self, context, part, fetch):
        """
        Optional data: on DataUnavailable note it and return None.

        The section still renders; ``partial_notes`` says what is missing,
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
        return self.note_age(context, value, part)

    def get_project_names(self, context):
        projects = self.get_optional(context, "projects", self.get_projects)
        return projects["names"] if projects else {}

    def get_pool_rows(self, context, snapshot, thresholds):
        """The pools, evaluated against the thresholds and forecasts."""
        forecast = self.get_optional(context, "forecast", self.get_forecast)
        return storage_health.evaluate_pools(
            snapshot["pools"],
            forecast["predictions"] if forecast else None,
            thresholds,
        )

    @staticmethod
    def note_age(context, value, part="metrics"):
        """
        Note when ``value`` was fetched and whether it is out of date.

        ``data_stale``: the last good copy, shown after a failure;
        ``data_old``: older than its lifetime plus a refresh interval.
        ``data_ts`` (metrics) and ``inventory_ts`` (Block Storage API) are
        the epoch second the value was fetched at, shown in the browser's
        time zone.
        """
        if value.get("is_stale_copy"):
            context["data_stale"] = True
            # the oldest copy shown
            fetched = str(int(value.get("fetched_at") or 0))
            if int(context.get("stale_ts", fetched)) >= int(fetched):
                context["stale_ts"] = fetched
        elif value.get("is_old_copy"):
            context["data_old"] = True
        fetched_at = value.get("fetched_at")
        if fetched_at and part == "inventory":
            context["inventory_ts"] = str(int(fetched_at))
            context["inventory_minutes"] = INVENTORY_CACHE_TTL // 60
        elif fetched_at and part in ("metrics", "forecast"):
            context.setdefault("data_ts", str(int(fetched_at)))
        return value

    def handle_error(self, context, exc):
        """Plain message in the page, details in the log."""
        if isinstance(exc, cache.ComputationFailed) and isinstance(
            exc.__cause__, config.ConfigurationError
        ):
            exc = exc.__cause__
        if isinstance(exc, config.ConfigurationError):
            if str(exc) not in _LOGGED_CONFIG_ERRORS:
                _LOGGED_CONFIG_ERRORS.add(str(exc))
                LOG.error("Invalid Grian setting: %s", exc)
            context["error_message"] = _(
                "Not available: invalid configuration. See the Horizon logs."
            )
        elif isinstance(exc, cache.StillComputing):
            LOG.info("Volume metrics not ready yet: %s", exc)
            context["loading"] = True
            context["error_message"] = _("Still loading…")
        else:
            if isinstance(exc, cache.DataUnavailable):
                # grian_ui.cache already logged the failure
                LOG.warning("Volume metrics unavailable: %s", exc)
            else:
                LOG.exception("Error loading volume metrics: %s", exc)
            context["error_message"] = self.error_text
        return context


def _choice(request, name, choices, default):
    value = request.GET.get(name)
    return value if value in choices else default


def _pool_row(row):
    """Template values of an evaluated pool."""
    forecast = row["forecast"] or {}
    return dict(
        row,
        physical_width=min(100, round(row["physical_percent"] or 0)),
        virtual_width=min(100, round(row["virtual_percent"] or 0)),
        last_seen_ts=str(int(row["last_seen"]))
        if row.get("last_seen")
        else "",
        status_label=storage_health.POOL_LABELS[row["status"]],
        days=forecast.get("days_until_full"),
        limiting=forecast.get("limiting"),
        enough_history=forecast.get("enough_history"),
        history_days=forecast.get("history_days"),
    )


def _volumes_filter_url():
    """Admin > Volume > Volumes (None outside Horizon)."""
    try:
        return reverse("horizon:admin:volumes:index")
    except NoReverseMatch:
        return None


class VolumesMetricsIndexView(_VolumeMetricsMixin, generic.TemplateView):
    template_name = "admin/volume_metrics/index.html"
    page_title = _("Storage Health")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["attention_limit"] = storage_health.LIST_LIMIT
        context["attention_store_limit"] = storage_health.ATTENTION_STORE_LIMIT
        context["pool_limit"] = storage_health.POOL_LIMIT
        context["project_limit"] = storage_health.PROJECT_LIMIT
        context["chart_pools"] = storage_health.CHART_POOLS
        context["ranges"] = list(storage_health.RANGES)
        context["default_range"] = storage_health.DEFAULT_RANGE
        context["inventory_minutes"] = INVENTORY_CACHE_TTL // 60
        context["history_minutes"] = HISTORY_CACHE_TTL // 60
        try:
            context["thresholds"] = config.get_storage_thresholds()
            if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
                config.get_openstack_keystone_url()
            context["stale_minutes"] = config.get_storage_stale_minutes()
        except config.ConfigurationError as exc:
            # shown once, at the top of the page; the shell queries nothing
            context["config_error"] = str(exc)
        return context


class MetricsUpdateView(_VolumeMetricsMixin, generic.TemplateView):
    """HTMX endpoint for the verdict and the summary cards."""

    template_name = "admin/volume_metrics/_metrics_cards.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_storage_thresholds()
            snapshot = self.note_age(context, self.get_snapshot())
            pools = self.get_pool_rows(context, snapshot, thresholds)
            inventory = self.get_optional(
                context, "inventory", self.get_inventory
            )
            context["summary"] = storage_health.summarize(
                pools, inventory, thresholds, snapshot["services"]
            )
            context["volumes"] = snapshot["volumes"]
            context["snapshots"] = snapshot["snapshots"]
            context["thresholds"] = thresholds
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class AttentionView(_VolumeMetricsMixin, generic.TemplateView):
    """Volumes and snapshots needing attention (Block Storage API)."""

    template_name = "admin/volume_metrics/_attention.html"
    error_text = _(
        "Error loading the volumes and snapshots from the Block Storage "
        "API. See the Horizon logs."
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_storage_thresholds()
            inventory = self.note_age(
                context, self.get_inventory(), "inventory"
            )
            names = self.get_project_names(context)
            context["rows"] = [
                dict(
                    row,
                    project_name=names.get(
                        row["project_id"], row["project_id"] or ""
                    ),
                    for_text=storage_health.format_duration(
                        row["for_seconds"]
                    ),
                    url=_detail_url(row["kind"], row["id"]),
                )
                for row in inventory["rows"][: storage_health.LIST_LIMIT]
            ]
            context["attention"] = inventory
            context["thresholds"] = thresholds
            context["limit"] = storage_health.LIST_LIMIT
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class CapacityView(_VolumeMetricsMixin, generic.TemplateView):
    """Capacity over time: the whole cloud, or the fullest pools."""

    template_name = "admin/volume_metrics/_capacity.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        range_key = _choice(
            self.request,
            "range",
            storage_health.RANGES,
            storage_health.DEFAULT_RANGE,
        )
        view = _choice(
            self.request,
            "view",
            storage_health.VIEWS,
            storage_health.VIEW_CLOUD,
        )
        context["range_key"] = range_key
        context["view"] = view
        try:
            thresholds = config.get_storage_thresholds()
            pools = []
            if view == storage_health.VIEW_POOLS:
                snapshot = self.get_snapshot()
                pools = storage_health.evaluate_pools(
                    snapshot["pools"], None, thresholds
                )
            trend = self.note_age(
                context, self.get_capacity_trend(range_key, view, pools)
            )
            if TREND_CACHE_TTL[range_key] > REFRESH_SECONDS:
                context["data_minutes"] = TREND_CACHE_TTL[range_key] // 60
            _key, window, step = storage_health.get_range(range_key)
            timestamps = storage_health.grid(window, step, trend["fetched_at"])
            series = trend["series"]
            if view == storage_health.VIEW_POOLS:
                order = trend["names"] or []
            else:
                order = ["allocated", "provisioned", "total"]
            context["has_data"] = any(series.get(name) for name in order)
            context["chart"] = {
                "view": view,
                "labels": timestamps,
                "series": [
                    {
                        "name": name,
                        "values": storage_health.fill(
                            series.get(name, []), timestamps
                        ),
                    }
                    for name in order
                ],
                "warning": thresholds["physical_warning"],
                "critical": thresholds["physical_critical"],
            }
            context["thresholds"] = thresholds
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class PoolsView(_VolumeMetricsMixin, generic.TemplateView):
    """The pools table: top POOL_LIMIT pools for a sort order."""

    template_name = "admin/volume_metrics/_pools.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sort = _choice(
            self.request,
            "sort",
            storage_health.SORTS,
            storage_health.SORT_SOON,
        )
        context["sort"] = sort
        try:
            thresholds = config.get_storage_thresholds()
            snapshot = self.note_age(context, self.get_snapshot())
            rows = self.get_pool_rows(context, snapshot, thresholds)
            context["rows"] = [
                _pool_row(row)
                for row in storage_health.sort_pools(rows, sort)[
                    : storage_health.POOL_LIMIT
                ]
            ]
            context["total"] = len(rows)
            context["limit"] = storage_health.POOL_LIMIT
            context["thresholds"] = thresholds
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class PoolLookupView(_VolumeMetricsMixin, generic.TemplateView):
    """One pool by name, also outside the top POOL_LIMIT."""

    template_name = "admin/volume_metrics/_pool_lookup.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        name = (self.request.GET.get("name") or "").strip()[:255]
        context["name"] = name
        if not name:
            return context
        try:
            thresholds = config.get_storage_thresholds()
            snapshot = self.note_age(context, self.get_snapshot())
            rows = self.get_pool_rows(context, snapshot, thresholds)
            row = storage_health.find_pool(rows, name)
            context["row"] = _pool_row(row) if row else None
            context["thresholds"] = thresholds
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class PoolSuggestionsView(_VolumeMetricsMixin, generic.TemplateView):
    """Pool names for the look-up box (at most SUGGESTION_LIMIT)."""

    template_name = "admin/volume_metrics/_pool_suggestions.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            snapshot = self.get_snapshot()
            context["names"] = storage_health.suggest_pools(
                snapshot["pools"], self.request.GET.get("name", "")[:255]
            )
        except Exception as exc:
            # suggestions are a convenience: no message in a datalist
            LOG.debug("No pool suggestions: %s", exc)
            context["names"] = []
        return context


class ServicesView(_VolumeMetricsMixin, generic.TemplateView):
    """Cinder services with their 24-hour availability."""

    template_name = "admin/volume_metrics/_services.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            snapshot = self.note_age(context, self.get_snapshot())
            history = self.get_optional(
                context, "history", self.get_service_history
            )
            context["rows"] = storage_health.service_rows(
                snapshot["services"],
                history["history"] if history else None,
                history["fetched_at"] if history else time.time(),
            )
            context["history_minutes"] = HISTORY_CACHE_TTL // 60
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class BackendsView(_VolumeMetricsMixin, generic.TemplateView):
    """Pools summed per backend."""

    template_name = "admin/volume_metrics/_backends.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            snapshot = self.note_age(context, self.get_snapshot())
            rows, total = storage_health.backend_rows(
                snapshot["pools"], snapshot["services"]
            )
            context["rows"] = [
                dict(
                    row,
                    ratio_width=min(
                        100, round(50 * (row["oversubscription"] or 0))
                    ),
                )
                for row in rows
            ]
            context["total"] = total
            context["limit"] = storage_health.BACKEND_LIMIT
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class ProjectsView(_VolumeMetricsMixin, generic.TemplateView):
    """Volumes and snapshots per project (Block Storage API)."""

    template_name = "admin/volume_metrics/_projects.html"
    error_text = AttentionView.error_text

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            inventory = self.note_age(
                context, self.get_inventory(), "inventory"
            )
            names = self.get_project_names(context)
            usage = inventory["projects"]
            context["rows"] = [
                dict(
                    row,
                    project_name=names.get(row["project_id"]),
                    share_width=round(row["share"] or 0),
                )
                for row in usage["rows"]
            ]
            context["total"] = usage["total"]
            context["limit"] = storage_health.PROJECT_LIMIT
            context["volumes_url"] = _volumes_filter_url()
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class ImagesView(_VolumeMetricsMixin, generic.TemplateView):
    """Image store and volume backups."""

    template_name = "admin/volume_metrics/_images.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            images = self.note_age(context, self.get_images())
            context["data_minutes"] = IMAGES_CACHE_TTL // 60
            # counts as integers (for plurals); None when not reported
            context["images"] = {
                key: (
                    int(images[key])
                    if images.get(key) is not None
                    and key not in ("image_bytes", "backup_gib")
                    else images.get(key)
                )
                for key in (
                    "images",
                    "image_bytes",
                    "image_projects",
                    "images_downloaded",
                    "backups",
                    "backup_gib",
                )
            }
            snapshot = self.get_snapshot()
            context["backup_service"] = any(
                s["binary"] == "cinder-backup" for s in snapshot["services"]
            )
        except Exception as exc:
            self.handle_error(context, exc)
        return context
