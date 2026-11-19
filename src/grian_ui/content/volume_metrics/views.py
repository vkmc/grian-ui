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
#: Bump when the format of a cached value changes (see
#: grian_ui.cache.cache_key).
CACHE_VERSION = 1
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


class VolumesMetricsIndexView(_VolumeMetricsMixin, generic.TemplateView):
    template_name = "admin/volume_metrics/index.html"
    page_title = _("Storage Health")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["attention_limit"] = storage_health.LIST_LIMIT
        context["attention_store_limit"] = storage_health.ATTENTION_STORE_LIMIT
        context["inventory_minutes"] = INVENTORY_CACHE_TTL // 60
        try:
            config.get_storage_thresholds()
            context["stale_minutes"] = config.get_storage_stale_minutes()
        except config.ConfigurationError as exc:
            # shown once, at the top of the page
            context["config_error"] = str(exc)
        try:
            thresholds = config.get_storage_thresholds()
            context["thresholds"] = thresholds
            snapshot = self.note_age(context, self.get_snapshot())
            context["storage_pools"] = [
                dict(
                    pool,
                    usage_percent=round(pool["physical_percent"] or 0),
                    last_seen_ts=str(int(pool["last_seen"]))
                    if pool.get("last_seen")
                    else "",
                )
                for pool in self.get_pool_rows(context, snapshot, thresholds)
            ]
        except Exception as exc:
            self.handle_error(context, exc)
            context["storage_pools"] = []
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
                pools, inventory, thresholds
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


class CapacityPredictionView(_VolumeMetricsMixin, generic.TemplateView):
    """HTMX endpoint for capacity prediction visualization."""

    template_name = "admin/volume_metrics/_capacity_prediction.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            context["thresholds"] = config.get_storage_thresholds()
            forecast = self.note_age(context, self.get_forecast())
            context["predictions"] = forecast["predictions"]
        except Exception as exc:
            self.handle_error(context, exc)
        return context
