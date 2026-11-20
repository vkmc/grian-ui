#
# SPDX-License-Identifier: Apache-2.0

"""
Project -> Telemetry -> Storage views.

The index page is a light shell; every section is an HTMX partial that
loads and refreshes itself every REFRESH_SECONDS and renders a bounded
amount of data (four cards, four quota rows, at most CLEANUP_LIMIT,
VOLUMES_LIMIT and IMAGES_LIMIT rows, one point per day in the chart),
whatever the size of the project.

Data of the current project only:

* the Block Storage API (limits, volumes, snapshots), the Compute API
  (instances, for attachment names and image use) and the Image API
  (private images), read with the user's token, at most once per
  INVENTORY_CACHE_TTL per project whatever the number of partials,
  members and refreshes;
* Ceilometer metrics through Aetos, with the project's matcher in every
  selector: the footprint per range (FOOTPRINT_CACHE_TTL) and the
  forecast history (TREND_CACHE_TTL).

Every value is cached with grian_ui.cache.project_cache_key(), so two
projects never share one. When a refresh fails, the last good copy is
shown and marked; error details go to the Horizon log, not to the page.
"""

import logging
import time
import uuid

from django.urls import NoReverseMatch
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _
from django.views import generic

from grian_ui import cache
from grian_ui import config
from grian_ui import project as grian_project
from grian_ui import project_storage as ps
from grian_ui import project_storage_inventory
from grian_ui.datasource import prometheus_project_storage as queries
from grian_ui.datasource.api import get_datasource


LOG = logging.getLogger(__name__)

#: Refresh interval of the partials, in seconds (storage changes slowly).
REFRESH_SECONDS = 300
#: Seconds the API lists (Cinder, Nova, Glance) are shared by the partials
#: and the members of a project: a new or deleted volume shows up to this
#: late.
INVENTORY_CACHE_TTL = 60
#: Seconds the footprint of each range and the forecast history are shared.
FOOTPRINT_CACHE_TTL = 300
TREND_CACHE_TTL = 900
#: A shared value older than its lifetime plus this many seconds is flagged
#: as possibly out of date (the rule of the other Telemetry dashboards).
OLD_COPY_MARGIN = 30
#: Bump when the format of a cached value changes.
CACHE_VERSION = 1
CACHE_NAMESPACE = "project_storage"

_LOGGED_CONFIG_ERRORS = set()

# Notes for optional data that is not available: (failed, still loading)
OPTIONAL_NOTES = {
    "servers": (
        _("The Compute API could not be read: instances are shown by ID."),
        _("Loading the instances from the Compute API…"),
    ),
    "image_users": (
        _("The Compute API could not be read: unused images are not checked."),
        _("Loading the instances from the Compute API…"),
    ),
    "images": (
        _("The Image API could not be read: private images are not checked."),
        _("Loading the private images from the Image API…"),
    ),
    "forecast": (
        _("The forecasts could not be loaded."),
        _("Loading the forecasts…"),
    ),
    "boot_volumes": (
        _(
            "The Block Storage API could not be read: the instances using "
            "each image are not known (instances booted from a volume use "
            "the image of their volume)."
        ),
        _("Loading the volumes from the Block Storage API…"),
    ),
    "limits": (
        _(
            "The Block Storage API could not be read: the quota limit is "
            "not drawn."
        ),
        _("Loading the quota limit…"),
    ),
}

#: Section error texts: details go to the log.
API_ERROR = _(
    "Error loading data from the Block Storage API. See the Horizon logs "
    "for details."
)
IMAGE_ERROR = _(
    "Error loading data from the Image API. See the Horizon logs for details."
)
METRICS_ERROR = _(
    "Error loading telemetry data. See the Horizon logs for details."
)


def _check_settings():
    """
    Raise ConfigurationError for settings read inside shared computations.

    Only the request that runs a computation sees its cause: checking first
    gives every section the same message.
    """
    config.get_project_storage_thresholds()
    config.get_metrics_retention_days()
    if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
        # read when the datasource client is built
        config.get_openstack_keystone_url()


def _url(name, *args):
    """A Horizon URL, or None when that panel is not installed."""
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def instance_metrics_url():
    """The Instance metrics page (Project -> Telemetry), or None."""
    return _url("horizon:project:instance_metrics:index")


def instance_link(server_id, base_url):
    """Instance metrics page of one instance, or None without the page."""
    if not base_url or not server_id:
        return None
    return f"{base_url}?{urlencode({'instance': server_id})}"


class _StorageMixin:
    """Shared helpers: cached data, data age, errors."""

    #: Shown when the section's main data cannot be loaded.
    error_text = API_ERROR

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # unique per response: element ids inside partials must not repeat
        # across swaps
        context["uid"] = uuid.uuid4().hex[:12]
        context["refresh_seconds"] = REFRESH_SECONDS
        return context

    # Data -----------------------------------------------------------------

    def get_or_compute(self, name, ttl, compute):
        """
        cache.get_or_compute with a per-project key, stamping ``stored_at``.

        The cache marks a last good copy served after a failure
        (``is_stale_copy``), not one served while another request is still
        refreshing it: a copy older than its lifetime plus OLD_COPY_MARGIN
        is marked ``is_old_copy`` here (possibly out of date).
        """
        _check_settings()
        key = cache.project_cache_key(
            self.request, CACHE_NAMESPACE, name, version=CACHE_VERSION
        )

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

    def get_inventory(self):
        if getattr(self, "_inventory", None) is None:
            self._inventory = project_storage_inventory.get_inventory(
                self.request
            )
        return self._inventory

    def get_client(self):
        if getattr(self, "_client", None) is None:
            self._client = get_datasource(self.request)
        return self._client

    def get_block_storage(self):
        """Limits, volumes and snapshots (Block Storage API)."""

        def compute():
            return dict(
                self.get_inventory().block_storage(), fetched_at=time.time()
            )

        return self.get_or_compute(
            "block_storage", INVENTORY_CACHE_TTL, compute
        )

    def get_servers(self):
        """The project's instances (Compute API)."""

        def compute():
            return dict(self.get_inventory().servers(), fetched_at=time.time())

        return self.get_or_compute("servers", INVENTORY_CACHE_TTL, compute)

    def get_images(self):
        """The project's private images (Image API)."""

        def compute():
            return dict(self.get_inventory().images(), fetched_at=time.time())

        return self.get_or_compute("images", INVENTORY_CACHE_TTL, compute)

    def get_footprint(self, range_key):
        """The footprint of ``range_key``, at most the retention back."""
        _key, days = ps.footprint_range(range_key)
        query_days = ps.history_days(days, config.get_metrics_retention_days())
        matcher = grian_project.project_matcher(self.request)

        def compute():
            now = time.time()
            data = self.get_client().get_project_storage_footprint(
                matcher, query_days, at=now
            )
            return dict(data, fetched_at=now)

        return self.get_or_compute(
            f"footprint:{range_key}:{query_days}", FOOTPRINT_CACHE_TTL, compute
        )

    def get_trend(self):
        """History for the forecasts (FORECAST_LOOKBACK_DAYS at most)."""
        days = ps.history_days(
            ps.FORECAST_LOOKBACK_DAYS, config.get_metrics_retention_days()
        )
        matcher = grian_project.project_matcher(self.request)

        def compute():
            now = time.time()
            data = self.get_client().get_project_storage_trend(
                matcher, days, at=now
            )
            return dict(data, fetched_at=now, days=days)

        return self.get_or_compute(f"trend:{days}", TREND_CACHE_TTL, compute)

    def get_optional(self, context, part, fetch):
        """
        Optional data: on DataUnavailable note it and return None.

        The section still renders; ``partial_notes`` says what is missing
        (``text``, and ``loading`` for data still being computed rather
        than failed) and ``loading`` makes the section retry shortly.
        """
        try:
            value = fetch()
        except cache.DataUnavailable as exc:
            loading = isinstance(exc, cache.StillComputing)
            context[f"{part}_unavailable"] = True
            context["loading"] = context.get("loading") or loading
            context.setdefault("partial_notes", []).append(
                {"text": OPTIONAL_NOTES[part][loading], "loading": loading}
            )
            return None
        note_age(context, value)
        return value

    def handle_error(self, context, exc):
        if isinstance(exc, cache.ComputationFailed) and isinstance(
            exc.__cause__, config.ConfigurationError
        ):
            # raised inside a shared computation (e.g. building the client)
            exc = exc.__cause__
        _forget_partial_state(context)
        if isinstance(exc, config.ConfigurationError):
            # the index shows it; log each message once, not on every
            # refresh of every partial
            if str(exc) not in _LOGGED_CONFIG_ERRORS:
                _LOGGED_CONFIG_ERRORS.add(str(exc))
                LOG.error("Invalid project storage setting: %s", exc)
            context["error_message"] = _(
                "Not available: invalid configuration (see the top of the "
                "page)."
            )
            return context
        if isinstance(exc, cache.StillComputing):
            # another request is computing the shared data: show a loading
            # state, the template retries shortly
            LOG.info("Project storage data not ready yet: %s", exc)
            context["loading"] = True
            context["error_message"] = _("Still loading…")
            return context
        if isinstance(exc, cache.DataUnavailable):
            # grian_ui.cache already logged the failure with its traceback
            LOG.warning("Project storage data unavailable: %s", exc)
        else:
            LOG.exception("Error loading project storage data: %s", exc)
        context["error_message"] = self.error_text
        return context


def note_age(context, value):
    """Flag a failed refresh (stale) or a copy being refreshed (old)."""
    context["data_stale"] = context.get("data_stale") or value.get(
        "is_stale_copy", False
    )
    context["data_old"] = context.get("data_old") or value.get(
        "is_old_copy", False
    )
    fetched_at = value.get("fetched_at")
    if fetched_at and (
        not context.get("fetched_at") or fetched_at < context["fetched_at"]
    ):
        # the oldest data the section shows
        context["fetched_at"] = fetched_at
        context["fetched_ts"] = str(int(fetched_at))


def _forget_partial_state(context):
    """Drop the optional-data state of a section replaced by an error."""
    for key in list(context):
        if key.endswith("_unavailable") or key in ("loading", "partial_notes"):
            del context[key]


def _server_names(servers):
    if servers is None:
        return None
    return {s["id"]: s["name"] for s in servers["servers"] if s.get("name")}


class _CandidatesMixin(_StorageMixin):
    """Clean-up candidates: Cinder, plus Glance and Nova when available."""

    def get_candidates(self, context, block, thresholds, with_images=True):
        """
        Every clean-up candidate. Without ``with_images`` (enough for "Could
        be freed": images do not use the volume quota) only Cinder is read.
        """
        images = None
        if with_images:
            images = self.get_optional(context, "images", self.get_images)
        users = None
        if images is not None:
            servers = self.get_optional(
                context, "image_users", self.get_servers
            )
            if servers is not None:
                users = ps.image_users(servers["servers"], block["volumes"])
        return ps.clean_up_candidates(
            block["volumes"],
            block["snapshots"],
            images["images"] if images else None,
            users,
            thresholds,
            time.time(),
        )


class IndexView(_StorageMixin, generic.TemplateView):
    template_name = "project/telemetry_storage/index.html"
    page_title = _("Storage")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["project_name"] = getattr(
            self.request.user, "project_name", None
        )
        context["ranges"] = list(ps.FOOTPRINT_RANGES)
        context["default_range"] = ps.DEFAULT_FOOTPRINT_RANGE
        context["refresh_minutes"] = REFRESH_SECONDS // 60
        context["cleanup_limit"] = ps.CLEANUP_LIMIT
        context["volumes_limit"] = ps.VOLUMES_LIMIT
        context["images_limit"] = ps.IMAGES_LIMIT
        context["window"] = queries.CURRENT_WINDOW
        context["forecast_days"] = ps.FORECAST_LOOKBACK_DAYS
        context["trend_step"] = ps.TREND_STEP
        context["min_history_days"] = ps.MIN_FORECAST_HISTORY_DAYS
        context["inventory_seconds"] = INVENTORY_CACHE_TTL
        # every invalid setting at once, not one per page load
        errors = []
        try:
            context["thresholds"] = config.get_project_storage_thresholds()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            retention = config.get_metrics_retention_days()
            context["retention_days"] = retention
            context["forecast_history_days"] = ps.history_days(
                ps.FORECAST_LOOKBACK_DAYS, retention
            )
            context["ranges_beyond_retention"] = [
                key
                for key, days in ps.FOOTPRINT_RANGES.items()
                if days > retention
            ]
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
                config.get_openstack_keystone_url()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        context["error_messages"] = errors
        return context


class SummaryView(_CandidatesMixin, generic.TemplateView):
    """Four cards: gigabytes quota, volumes, snapshots, could be freed."""

    template_name = "project/telemetry_storage/_summary.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_project_storage_thresholds()
            block = self.get_block_storage()
            note_age(context, block)
            rows = {r["key"]: r for r in ps.quota_rows(block, thresholds)}
            use = ps.usage(block)
            candidates = self.get_candidates(
                context, block, thresholds, with_images=False
            )
            freed_gb, freed_count = ps.reclaimable(candidates)
            context.update(
                gigabytes=rows["gigabytes"],
                volumes=rows["volumes"],
                snapshots=rows["snapshots"],
                usage=use,
                freed_gb=freed_gb,
                freed_count=freed_count,
            )
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class QuotaView(_StorageMixin, generic.TemplateView):
    """The quota rows with their forecasts."""

    template_name = "project/telemetry_storage/_quota.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_project_storage_thresholds()
            block = self.get_block_storage()
            note_age(context, block)
            rows = ps.quota_rows(block, thresholds)
            trend = self.get_optional(context, "forecast", self.get_trend)
            if trend is not None:
                forecasts = ps.quota_forecasts(rows, trend, thresholds)
                for row in rows:
                    if row["key"] in forecasts:
                        row["forecast"] = forecasts[row["key"]]
                context["forecast_days"] = trend.get("days")
            context["rows"] = rows
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class FootprintView(_StorageMixin, generic.TemplateView):
    """Volumes and snapshots over the range, stacked, with the quota."""

    template_name = "project/telemetry_storage/_footprint.html"
    error_text = METRICS_ERROR

    def get_range_key(self):
        return ps.footprint_range(self.request.GET.get("range"))[0]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        range_key = self.get_range_key()
        context["range"] = range_key
        try:
            retention = config.get_metrics_retention_days()
            footprint = self.get_footprint(range_key)
            note_age(context, footprint)
            chart = ps.footprint_chart(
                footprint, range_key, retention, footprint["fetched_at"]
            )
            block = self.get_optional(
                context, "limits", self.get_block_storage
            )
            limit = None
            if block is not None:
                limit = block["limits"].get("gigabytes")
                context["has_storage"] = bool(
                    block["volumes"] or block["snapshots"]
                )
            else:
                context["has_storage"] = chart["has_data"]
            if not chart["has_data"] and not context["has_storage"]:
                # the empty state names private images only when there
                # are none (they are shared with the Private images
                # section; not knowing them is not worth a note here)
                try:
                    images = self.get_images()
                    context["has_images"] = bool(images["images"])
                except cache.DataUnavailable:
                    context["has_images"] = None
            chart["limit"] = limit
            context["chart"] = chart
            context["retention_days"] = retention
            context["first_ts"] = (
                str(int(chart["first_ts"])) if chart["first_ts"] else None
            )
            # history shorter than the range: say where it starts
            context["partial_history"] = chart["has_data"] and (
                chart["first_ts"] > chart["timestamps"][0]
            )
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class CleanupView(_CandidatesMixin, generic.TemplateView):
    """At most CLEANUP_LIMIT clean-up candidates."""

    template_name = "project/telemetry_storage/_cleanup.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_project_storage_thresholds()
            block = self.get_block_storage()
            note_age(context, block)
            candidates = self.get_candidates(context, block, thresholds)
            context["candidates"] = candidates[: ps.CLEANUP_LIMIT]
            context["total"] = len(candidates)
            context["limit"] = ps.CLEANUP_LIMIT
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class VolumesView(_StorageMixin, generic.TemplateView):
    """At most VOLUMES_LIMIT volumes and snapshots."""

    template_name = "project/telemetry_storage/_volumes.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            block = self.get_block_storage()
            note_age(context, block)
            servers = None
            if any(v["server_ids"] for v in block["volumes"]):
                servers = self.get_optional(
                    context, "servers", self.get_servers
                )
            rows, total = ps.volume_rows(
                block, _server_names(servers), time.time()
            )
            metrics_url = instance_metrics_url()
            for row in rows:
                if row["kind"] == "volume":
                    row["url"] = _url(
                        "horizon:project:volumes:detail", row["id"]
                    )
                    for server in row["servers"]:
                        server["url"] = instance_link(
                            server["id"], metrics_url
                        )
                else:
                    row["url"] = _url(
                        "horizon:project:snapshots:detail", row["id"]
                    )
            context.update(
                rows=rows,
                total=total,
                limit=ps.VOLUMES_LIMIT,
                create_url=_url("horizon:project:volumes:create"),
            )
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class ImagesView(_StorageMixin, generic.TemplateView):
    """At most IMAGES_LIMIT private images, with the instances using them."""

    template_name = "project/telemetry_storage/_images.html"
    error_text = IMAGE_ERROR

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            images = self.get_images()
            note_age(context, images)
            users = None
            if images["images"]:
                servers = self.get_optional(
                    context, "image_users", self.get_servers
                )
                # instances booted from a volume use the volume's image:
                # without the volumes, the users are unknown, not 0
                block = self.get_optional(
                    context, "boot_volumes", self.get_block_storage
                )
                if servers is not None and block is not None:
                    users = ps.image_users(
                        servers["servers"], block["volumes"]
                    )
            rows, total, total_gb = ps.image_rows(
                images["images"], users, time.time()
            )
            context.update(
                rows=rows,
                total=total,
                total_gb=total_gb,
                limit=ps.IMAGES_LIMIT,
                truncated=images.get("truncated", False),
            )
        except Exception as exc:
            self.handle_error(context, exc)
        return context
