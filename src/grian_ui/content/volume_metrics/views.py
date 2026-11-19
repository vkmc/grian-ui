"""
Volume metrics views.

The pool capacity, volume and snapshot counts are read once per refresh
(one query per metric, whatever the number of pools) and the capacity
forecasts every FORECAST_CACHE_TTL seconds. Each is computed once and
shared through the Horizon cache by the page, its HTMX partials and the
administrators of a region (see grian_ui.cache). When a refresh fails, the
last good copy is shown with the time it was fetched; error details go to
the Horizon log, not to the page.
"""

import logging
import time
import uuid

from django.utils.translation import gettext_lazy as _
from django.views import generic

from grian_ui import cache
from grian_ui import config
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
#: Bump when the format of a cached value changes (see
#: grian_ui.cache.cache_key).
CACHE_VERSION = 1
CACHE_NAMESPACE = "volume_metrics"

_LOGGED_CONFIG_ERRORS = set()


def pool_totals(pools):
    """Cloud-wide capacity from the pool snapshot (GiB, pool counts)."""
    return {
        "total_storage": round(sum(p["total"] for p in pools)),
        "allocated_storage": round(sum(p["allocated"] for p in pools)),
        "pools_total": len(pools),
        "pools_stale": sum(1 for p in pools if p["stale"]),
    }


def _usage_percent(pool):
    if not pool["total"]:
        return 0
    return round(100.0 * pool["allocated"] / pool["total"])


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
        # settings read inside the shared computations: checked first so
        # that every section gets the same message
        config.get_storage_stale_minutes()
        if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
            # read when the client is built
            config.get_openstack_keystone_url()

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

    @staticmethod
    def note_age(context, value):
        """
        Note when ``value`` was fetched and whether it is out of date.

        ``data_stale``: the last good copy, shown after a failure;
        ``data_old``: older than its lifetime plus a refresh interval.
        ``data_ts`` is the epoch second the value was fetched at, shown in
        the browser's time zone.
        """
        if value.get("is_stale_copy"):
            context["data_stale"] = True
        elif value.get("is_old_copy"):
            context["data_old"] = True
        fetched_at = value.get("fetched_at")
        if fetched_at:
            context["data_ts"] = str(int(fetched_at))
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
    page_title = _("Volume Metrics")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        try:
            snapshot = self.note_age(context, self.get_snapshot())
            context["storage_pools"] = [
                dict(
                    pool,
                    usage_percent=_usage_percent(pool),
                    last_seen_ts=str(int(pool["last_seen"]))
                    if pool.get("last_seen")
                    else "",
                )
                for pool in snapshot["pools"]
            ]
        except Exception as exc:
            self.handle_error(context, exc)
            context["storage_pools"] = []
        return context


class MetricsUpdateView(_VolumeMetricsMixin, generic.TemplateView):
    """HTMX endpoint for updating metrics cards."""

    template_name = "admin/volume_metrics/_metrics_cards.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            snapshot = self.note_age(context, self.get_snapshot())
            context["overview"] = pool_totals(snapshot["pools"])
            context["volumes"] = snapshot["volumes"]
            context["snapshots"] = snapshot["snapshots"]
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class ChartUpdateView(_VolumeMetricsMixin, generic.TemplateView):
    """HTMX endpoint for updating the storage chart."""

    template_name = "admin/volume_metrics/_storage_chart.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            snapshot = self.note_age(context, self.get_snapshot())
            context["volumes"] = snapshot["volumes"]
            context["snapshots"] = snapshot["snapshots"]
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class CapacityPredictionView(_VolumeMetricsMixin, generic.TemplateView):
    """HTMX endpoint for capacity prediction visualization."""

    template_name = "admin/volume_metrics/_capacity_prediction.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            forecast = self.note_age(context, self.get_forecast())
            context["predictions"] = forecast["predictions"]
        except Exception as exc:
            self.handle_error(context, exc)
        return context
