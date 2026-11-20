#
# SPDX-License-Identifier: Apache-2.0

"""
Project network views (Project > Telemetry > Network).

The index page is a light shell; every section is an HTMX partial that
loads and refreshes itself. Every section renders a bounded amount of data
(a verdict, four cards, the project's routers and networks, at most 25
interfaces and 25 floating IPs, two traffic series).

Data comes from three sources, each computed once and shared through the
Horizon cache by the partials and by the members of the project (keys from
grian_ui.cache.project_cache_key, so two projects never share a value):

* the Networking API, read with the user's token
  (grian_ui.project_network_inventory): the project's routers, networks,
  ports and floating IPs, and separately the network quota, refreshed
  every INVENTORY_CACHE_TTL seconds;
* the Compute API: the project's instances (names, states), refreshed
  every INSTANCES_CACHE_TTL seconds;
* the interface telemetry, read through the configured datasource with
  the project matcher in every selector, refreshed with the page.

A section that needs several sources renders with what is available and
says which checks could not be done.
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
from grian_ui import project_network
from grian_ui import project_network_inventory
from grian_ui import timeseries
from grian_ui.datasource.api import get_datasource


LOG = logging.getLogger(__name__)

#: Refresh interval of the partials, in seconds (used by the templates).
REFRESH_SECONDS = 30
#: Seconds the interface telemetry is shared. Slightly shorter than the
#: refresh interval so every refresh gets new data, computed once.
INTERFACES_CACHE_TTL = REFRESH_SECONDS - 5
#: Seconds the Networking API inventory (at most seven calls), the network
#: quota and the Compute API instances are shared: changes show up to this
#: late.
INVENTORY_CACHE_TTL = 60
INSTANCES_CACHE_TTL = 60
#: Ranges of the charts and how long each trend is shared.
RANGES = ("1h", "6h", "24h")
DEFAULT_RANGE = "6h"
TREND_CACHE_TTL = {"1h": 60, "6h": 60, "24h": 300}
#: Range of the series used to find when a router went quiet.
SINCE_RANGE = "6h"
#: Bump when the format of a cached value changes.
CACHE_VERSION = 2
CACHE_NAMESPACE = "project_network"
#: The instance metrics detail page (another Project > Telemetry panel).
INSTANCE_METRICS_URL_NAME = "horizon:project:instance_metrics:index"

_LOGGED_CONFIG_ERRORS = set()

# Notes for optional data that is not available: (failed, still loading)
OPTIONAL_NOTES = {
    "inventory": (
        _(
            "The Networking API data could not be loaded: routers, ports "
            "and floating IPs are not checked."
        ),
        _("Loading your network resources from the Networking API…"),
    ),
    "instances": (
        _(
            "The instance list could not be loaded: interfaces of stopped "
            "instances are included and may show as down."
        ),
        _("Loading your instances…"),
    ),
    "interfaces": (
        _(
            "Interface telemetry could not be loaded: quiet interfaces, "
            "drops and errors are not checked."
        ),
        _("Loading the interface telemetry…"),
    ),
    "trend": (
        _("The traffic history could not be loaded."),
        _("Loading the traffic history…"),
    ),
}


def _cache_key(request, name):
    return cache.project_cache_key(
        request, CACHE_NAMESPACE, name, version=CACHE_VERSION
    )


def _check_settings():
    """
    Raise ConfigurationError for settings read inside shared computations.

    Only the request that runs a computation sees its cause: checking first
    gives every section (and the index) the same message.
    """
    if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
        config.get_openstack_keystone_url()


_RATE_UNITS = ("B/s", "kB/s", "MB/s", "GB/s", "TB/s")


def format_rate(value):
    """Bytes per second, in decimal units: ``"1.6 MB/s"``, or a dash."""
    if value is None:
        return "–"
    value = float(value)
    unit = 0
    while abs(value) >= 1000 and unit < len(_RATE_UNITS) - 1:
        value /= 1000.0
        unit += 1
    text = f"{value:.0f}" if unit == 0 or abs(value) >= 100 else f"{value:.1f}"
    return f"{text} {_RATE_UNITS[unit]}"


def instance_metrics_url():
    """URL of the instance metrics page, or None when it is not enabled."""
    try:
        return reverse(INSTANCE_METRICS_URL_NAME)
    except NoReverseMatch:
        return None


def instance_link(base_url, instance_id):
    """Link to an instance on the instance metrics page, or None."""
    if not base_url or not instance_id:
        return None
    return f"{base_url}?{urllib.parse.urlencode({'instance': instance_id})}"


def _range_key(request):
    key = request.GET.get("range")
    return key if key in RANGES else DEFAULT_RANGE


class _ProjectNetworkMixin:
    """Shared helpers: lazy clients, cached data, errors."""

    #: Shown when the section's required data fails to load.
    error_text = _("Error loading network data. See the Horizon logs.")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Unique per response: element ids inside partials must not repeat
        # across swaps (htmx would settle the new canvas with the old
        # element's attributes and clear the chart).
        context["uid"] = uuid.uuid4().hex[:12]
        return context

    def get_or_compute(self, name, ttl, compute):
        """
        cache.get_or_compute, after checking the settings.

        Values are stamped with ``stored_at`` when their computation ends:
        one older than its lifetime plus a refresh interval is marked
        ``is_old_copy`` (probably still being refreshed).
        """
        _check_settings()

        def stamped():
            return dict(compute(), stored_at=time.time())

        value = cache.get_or_compute(
            _cache_key(self.request, name), ttl, stamped
        )
        stored_at = value.get("stored_at")
        if (
            not value.get("is_stale_copy")
            and stored_at is not None
            and time.time() - stored_at > ttl + REFRESH_SECONDS
        ):
            value = dict(value, is_old_copy=True)
        return value

    def get_client(self):
        if getattr(self, "_client", None) is None:
            self._client = get_datasource(self.request)
        return self._client

    def get_network(self):
        if getattr(self, "_network", None) is None:
            self._network = project_network_inventory.get_inventory(
                self.request
            )
        return self._network

    def matcher(self):
        return project.project_matcher(self.request)

    def get_inventory(self):
        def compute():
            return dict(
                self.get_network().get_inventory(), fetched_at=time.time()
            )

        return self.get_or_compute("inventory", INVENTORY_CACHE_TTL, compute)

    def get_quota(self):
        """The network quota: its own entry, so a failure hides only it."""

        def compute():
            return dict(self.get_network().get_quota(), fetched_at=time.time())

        return self.get_or_compute("quota", INVENTORY_CACHE_TTL, compute)

    def get_instances(self):
        def compute():
            return dict(
                self.get_network().get_instances(), fetched_at=time.time()
            )

        return self.get_or_compute("instances", INSTANCES_CACHE_TTL, compute)

    def get_interfaces(self):
        matcher = self.matcher()

        def compute():
            return dict(
                self.get_client().get_project_network_interfaces(matcher),
                fetched_at=time.time(),
            )

        return self.get_or_compute("interfaces", INTERFACES_CACHE_TTL, compute)

    def get_traffic_trend(self, range_key):
        matcher = self.matcher()

        def compute():
            # evaluated at the time the chart grid is built from
            now = time.time()
            return {
                "series": self.get_client().get_project_network_traffic_trend(
                    matcher, range_key, at=now
                ),
                "fetched_at": now,
            }

        return self.get_or_compute(
            f"traffic:{range_key}", TREND_CACHE_TTL[range_key], compute
        )

    def get_interface_trend(self, range_key):
        matcher = self.matcher()

        def compute():
            now = time.time()
            client = self.get_client()
            return {
                "series": client.get_project_network_interface_trend(
                    matcher, range_key, at=now
                ),
                "fetched_at": now,
            }

        return self.get_or_compute(
            f"interface_trend:{range_key}",
            TREND_CACHE_TTL[range_key],
            compute,
        )

    def get_optional(self, context, part, fetch):
        """
        Optional data: on DataUnavailable note it and return None.

        The section still renders; ``<part>_unavailable`` says what is
        missing, ``partial_notes`` says it in words, and ``loading`` makes
        the section retry shortly.
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
        return self.note_age(context, part, value)

    @staticmethod
    def note_age(context, part, value):
        """
        Note when ``value`` was fetched and whether it is out of date.

        ``data_stale``: a last good copy shown after a failure;
        ``data_old``: older than its lifetime plus a refresh interval.
        ``<source>_ts`` is the epoch second the value was fetched at,
        shown in the browser's time zone.
        """
        if value.get("is_stale_copy"):
            context["data_stale"] = True
        elif value.get("is_old_copy"):
            context["data_old"] = True
        fetched_at = value.get("fetched_at")
        if fetched_at:
            name = {
                "interfaces": "metrics",
                "trend": "metrics",
                "instances": "inventory",
            }.get(part, part)
            context.setdefault(f"{name}_ts", str(int(fetched_at)))
        return value

    def get_sources(self, context, inventory=True, telemetry=True):
        """
        The inventory, instances and telemetry a section uses (each may be
        None), raising when none of the required ones is available.
        """
        context["inventory_seconds"] = INVENTORY_CACHE_TTL
        found = {"inventory": None, "instances": None, "telemetry": None}
        if inventory:
            found["inventory"] = self.get_optional(
                context, "inventory", self.get_inventory
            )
            if found["inventory"] is not None:
                found["instances"] = self.get_optional(
                    context, "instances", self.get_instances
                )
        if telemetry:
            found["telemetry"] = self.get_optional(
                context, "interfaces", self.get_interfaces
            )
        if found["inventory"] is None and found["telemetry"] is None:
            if context.get("loading"):
                raise cache.StillComputing("no network data yet")
            raise cache.ComputationFailed("no network data")
        return found

    def summarize(self, context, thresholds, **sources):
        found = self.get_sources(context, **sources)
        return project_network.summarize(
            found["inventory"],
            found["instances"],
            found["telemetry"],
            thresholds,
        ), found

    def handle_error(self, context, exc):
        if isinstance(exc, cache.ComputationFailed) and isinstance(
            exc.__cause__, config.ConfigurationError
        ):
            # raised inside a shared computation (e.g. building a client)
            exc = exc.__cause__
        _forget_partial_state(context)
        if isinstance(exc, config.ConfigurationError):
            if str(exc) not in _LOGGED_CONFIG_ERRORS:
                _LOGGED_CONFIG_ERRORS.add(str(exc))
                LOG.error("Invalid project network setting: %s", exc)
            # the index shows the message once, at the top of the page
            context["error_message"] = _(
                "Not available: invalid configuration (see the top of the "
                "page)."
            )
            return context
        if isinstance(exc, cache.StillComputing):
            LOG.info("Project network data not ready yet: %s", exc)
            context["loading"] = True
            context["error_message"] = _("Still loading…")
            return context
        if isinstance(exc, cache.DataUnavailable):
            # grian_ui.cache already logged the failure with its traceback
            LOG.warning("Project network data unavailable: %s", exc)
        else:
            # details (backend messages) go to the log, not to the page
            LOG.exception("Error loading project network data: %s", exc)
        context["error_message"] = self.error_text
        return context


def _forget_partial_state(context):
    """Drop the optional-data state of a section replaced by an error."""
    for key in list(context):
        if key.endswith("_unavailable") or key in (
            "loading",
            "partial_notes",
            "data_stale",
            "data_old",
        ):
            del context[key]


class IndexView(_ProjectNetworkMixin, generic.TemplateView):
    template_name = "project/telemetry_network/index.html"
    page_title = _("Network")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["ranges"] = list(RANGES)
        context["default_range"] = DEFAULT_RANGE
        context["refresh_seconds"] = REFRESH_SECONDS
        context["inventory_seconds"] = INVENTORY_CACHE_TTL
        context["quiet_window"] = project_network.QUIET_WINDOW
        context["rate_window"] = project_network.RATE_WINDOW
        context["usual_offset"] = project_network.USUAL_OFFSET
        context["usual_fallback"] = project_network.USUAL_FALLBACK
        context["interface_limit"] = project_network.INTERFACE_LIMIT
        # every invalid setting at once, not one per page load
        errors = []
        try:
            context["thresholds"] = config.get_project_network_thresholds()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            # read when the datasource client is built: shown here, once
            _check_settings()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        context["error_messages"] = errors
        return context


class SummaryView(_ProjectNetworkMixin, generic.TemplateView):
    """The verdict and the summary cards."""

    template_name = "project/telemetry_network/_summary.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_project_network_thresholds()
            summary, found = self.summarize(context, thresholds)
            verdict = summary["verdict"]
            if verdict["key"] == project_network.VERDICT_ROUTER_QUIET:
                verdict = dict(
                    verdict,
                    since_ts=self.quiet_since(
                        verdict["keys"], found["telemetry"], thresholds
                    ),
                )
            summary = dict(
                summary,
                verdict=verdict,
                in_text=format_rate(summary["in_bps"]),
                out_text=format_rate(summary["out_bps"]),
            )
            context["summary"] = summary
            context["thresholds"] = thresholds
            context["rate_window"] = project_network.RATE_WINDOW
        except Exception as exc:
            self.handle_error(context, exc)
        return context

    def quiet_since(self, keys, telemetry, thresholds):
        """When the router went quiet (epoch second as text), or None."""
        try:
            trend = self.get_interface_trend(SINCE_RANGE)
        except cache.DataUnavailable:
            return None
        interfaces = {i["key"]: i for i in telemetry.get("interfaces", [])}
        since = project_network.quiet_since(
            trend["series"], keys, interfaces, thresholds
        )
        return str(int(since)) if since is not None else None


class TopologyView(_ProjectNetworkMixin, generic.TemplateView):
    """Routers, each followed by its networks."""

    template_name = "project/telemetry_network/_topology.html"
    error_text = _(
        "Error loading your routers and networks from the Networking API. "
        "See the Horizon logs."
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_project_network_thresholds()
            # required: without it there is nothing to show
            self.note_age(context, "inventory", self.get_inventory())
            summary, _found = self.summarize(context, thresholds)
            context["rows"] = summary["topology"]
            context["metadata_ports_down"] = summary["metadata_ports_down"]
            # every "usual" rate is the 6-hour average (a new project)
            bases = {r["usual_basis"] for r in summary["rows"]}
            context["usual_fallback_only"] = bases == {"6h"}
            context["thresholds"] = thresholds
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class TrafficView(_ProjectNetworkMixin, generic.TemplateView):
    """Project inbound and outbound traffic over the selected range."""

    template_name = "project/telemetry_network/_traffic.html"
    error_text = _("Error loading telemetry data. See the Horizon logs.")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        range_key = _range_key(self.request)
        context["range_key"] = range_key
        try:
            trend = self.note_age(
                context, "trend", self.get_traffic_trend(range_key)
            )
            fetched_at = trend["fetched_at"]
            series = {
                direction: timeseries.gap_filled_series(
                    points, range_key, fetched_at
                )
                for direction, points in trend["series"].items()
            }
            context["has_data"] = any(trend["series"].values())
            latest = {}
            for direction, points in series.items():
                values = [v for _t, v in points[-2:] if v is not None]
                latest[direction] = format_rate(values[-1] if values else None)
            context["latest"] = latest
            context["chart"] = {"range": range_key, "series": series}
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class QuotaView(_ProjectNetworkMixin, generic.TemplateView):
    """Network quota (Networking API)."""

    template_name = "project/telemetry_network/_quota.html"
    error_text = _(
        "Error loading the network quota from the Networking API. See the "
        "Horizon logs."
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_project_network_thresholds()
            quota = self.note_age(context, "inventory", self.get_quota())
            context["inventory_seconds"] = INVENTORY_CACHE_TTL
            context["rows"] = project_network.quota_rows(quota, thresholds)
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class FloatingIPsView(_ProjectNetworkMixin, generic.TemplateView):
    """The project's floating IPs (Networking API)."""

    template_name = "project/telemetry_network/_floating_ips.html"
    error_text = _(
        "Error loading the floating IPs from the Networking API. See the "
        "Horizon logs."
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_project_network_thresholds()
            self.note_age(context, "inventory", self.get_inventory())
            summary, found = self.summarize(
                context, thresholds, telemetry=False
            )
            rows, total = project_network.floating_ip_rows(
                found["inventory"], summary["rows"]
            )
            context["rows"] = rows
            context["total"] = total
            context["limit"] = project_network.FLOATING_IP_LIMIT
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class InterfacesView(_ProjectNetworkMixin, generic.TemplateView):
    """Instance interfaces: problems first, at most INTERFACE_LIMIT."""

    template_name = "project/telemetry_network/_interfaces.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        range_key = _range_key(self.request)
        context["range_key"] = range_key
        try:
            thresholds = config.get_project_network_thresholds()
            summary, _found = self.summarize(context, thresholds)
            trend = self.get_optional(
                context,
                "trend",
                lambda: self.get_interface_trend(range_key),
            )
            series = (trend or {}).get("series") or {}
            base_url = instance_metrics_url()
            rows = []
            sparks = {}
            for n, row in enumerate(
                project_network.select_interfaces(summary["rows"])
            ):
                spark = f"s{n}"
                points = series.get(row["key"])
                if points:
                    sparks[spark] = {
                        "quiet": row["quiet"],
                        "points": timeseries.gap_filled_series(
                            points, range_key, trend["fetched_at"]
                        ),
                    }
                rows.append(
                    dict(
                        row,
                        spark=spark if points else None,
                        link=instance_link(base_url, row["device_id"]),
                        state_label=project_network.STATE_LABELS[row["state"]],
                        in_text=format_rate(row["in_bps"]),
                        out_text=format_rate(row["out_bps"]),
                    )
                )
            context["rows"] = rows
            context["sparks"] = sparks
            context["total"] = summary["interfaces_total"]
            context["not_running"] = summary["not_running"]
            context["limit"] = project_network.INTERFACE_LIMIT
            context["thresholds"] = thresholds
            context["inventory_checked"] = summary["inventory_checked"]
        except Exception as exc:
            self.handle_error(context, exc)
        return context
