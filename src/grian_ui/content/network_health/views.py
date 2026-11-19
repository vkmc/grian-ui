#
# SPDX-License-Identifier: Apache-2.0

"""
Network health views.

The index page is a light shell; every section is an HTMX partial that
loads and refreshes itself. Each partial renders a bounded amount of data
(a verdict, four cards, at most 25 agents, 25 routers and 10 projects, two
traffic series) whatever the size of the cloud.

Data comes from two sources, each computed once and shared through the
Horizon cache by the partials and by the administrators of a region (see
grian_ui.cache):

* the Networking API, read with the administrator's token
  (grian_ui.network_inventory): one inventory snapshot (agents, routers,
  gateway ports, floating IPs) refreshed every INVENTORY_CACHE_TTL
  seconds, and the full port listing, the most expensive call, every
  TOPOLOGY_CACHE_TTL seconds. Page refreshes never call Neutron
  themselves: they read these shared values;
* the interface telemetry, read through the configured datasource and
  refreshed with the page.

Each process joins them once per set of inputs. A section that needs both
renders with what is available and says which checks could not be done.
"""

import collections
import logging
import threading
import time
import uuid

from django.utils.translation import gettext_lazy as _
from django.views import generic

from grian_ui import cache
from grian_ui import config
from grian_ui import network_health
from grian_ui import network_inventory
from grian_ui import timeseries
from grian_ui.datasource.api import get_datasource


LOG = logging.getLogger(__name__)

#: Refresh interval of the partials, in seconds (used by the templates).
REFRESH_SECONDS = 30
#: Seconds the interface telemetry is shared. Slightly shorter than the
#: refresh interval so every refresh gets new data, computed once.
INTERFACES_CACHE_TTL = REFRESH_SECONDS - 5
#: Seconds the Networking API inventory (agents, routers, gateway ports,
#: floating IPs: four listings) is shared: agent and router changes show
#: up to this late.
INVENTORY_CACHE_TTL = 120
#: Seconds the router topology is shared. It needs every port of every
#: project (one paginated listing, by far the largest call), and routers
#: gain or lose instances slowly, so it is refreshed less often than the
#: inventory.
TOPOLOGY_CACHE_TTL = 300
#: Seconds the project names (Identity API) are shared.
PROJECTS_CACHE_TTL = 300
#: Ranges of the traffic chart and how long each trend is shared.
RANGES = ("6h", "24h")
DEFAULT_RANGE = "24h"
TRAFFIC_CACHE_TTL = {"6h": 60, "24h": 300}
#: Bump when the format of a cached value changes (see
#: grian_ui.cache.cache_key).
CACHE_VERSION = 1
CACHE_NAMESPACE = "network_health"

_LOGGED_CONFIG_ERRORS = set()

#: Joins of the shared values, per process: (region key, input stamps) ->
#: result. A handful is enough: inputs change at most every refresh.
_JOINS = collections.OrderedDict()
_JOINS_LOCK = threading.Lock()
JOINS_KEPT = 8

# Notes for optional data that is not available: (failed, still loading)
OPTIONAL_NOTES = {
    "inventory": (
        _(
            "The Networking API data could not be loaded: agents, routers "
            "and floating IPs are not checked."
        ),
        _("Loading the network data from the Networking API…"),
    ),
    "topology": (
        _(
            "The ports behind the routers could not be loaded: routers "
            "are not checked for lost traffic."
        ),
        _("Loading the ports behind the routers…"),
    ),
    "interfaces": (
        _(
            "Interface telemetry could not be loaded: lost traffic, drops "
            "and errors are not checked."
        ),
        _("Loading the interface telemetry…"),
    ),
    "projects": (
        _("Project names could not be loaded: showing project IDs."),
        _("Loading the project names…"),
    ),
}


def _cache_key(request, name):
    return cache.cache_key(
        request, CACHE_NAMESPACE, name, version=CACHE_VERSION
    )


def _check_settings():
    """
    Raise ConfigurationError for settings read inside shared computations.

    Only the request that runs a computation sees its cause: checking first
    gives every section (and the index) the same message.
    """
    config.get_network_health_rate_window()
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


def _range_key(request):
    key = request.GET.get("range")
    return key if key in RANGES else DEFAULT_RANGE


class _NetworkHealthMixin:
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

        The cache marks a last good copy served after a failure
        (``is_stale_copy``), not one served while another request is still
        refreshing it (a slow or hanging backend). Values are stamped with
        ``stored_at`` when their computation ends: one older than its
        lifetime plus a refresh interval is marked ``is_old_copy`` here:
        probably still being refreshed, possibly out of date. (The age
        compares the clocks of the Horizon nodes sharing the cache: keep
        them in sync.)
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
            self._network = network_inventory.get_inventory(self.request)
        return self._network

    def get_inventory(self):
        """The Networking API inventory, shared for INVENTORY_CACHE_TTL."""

        def compute():
            return dict(
                self.get_network().get_inventory(), fetched_at=time.time()
            )

        return self.get_or_compute("inventory", INVENTORY_CACHE_TTL, compute)

    def get_topology(self):
        """Ports behind each router, shared for TOPOLOGY_CACHE_TTL."""

        def compute():
            return dict(
                self.get_network().get_topology(), fetched_at=time.time()
            )

        return self.get_or_compute("topology", TOPOLOGY_CACHE_TTL, compute)

    def get_projects(self):
        def compute():
            return {"names": self.get_network().project_names()}

        return self.get_or_compute("projects", PROJECTS_CACHE_TTL, compute)

    def get_interfaces(self):
        def compute():
            return dict(
                self.get_client().get_network_interfaces(),
                fetched_at=time.time(),
            )

        return self.get_or_compute("interfaces", INTERFACES_CACHE_TTL, compute)

    def get_traffic_trend(self, range_key):
        def compute():
            # evaluated at the time the chart grid is built from
            now = time.time()
            return {
                "series": self.get_client().get_network_traffic_trend(
                    range_key, at=now
                ),
                "fetched_at": now,
            }

        return self.get_or_compute(
            f"traffic:{range_key}", TRAFFIC_CACHE_TTL[range_key], compute
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

    def get_project_names(self, context):
        projects = self.get_optional(context, "projects", self.get_projects)
        return projects["names"] if projects else {}

    def use_required(self, context, part, value):
        """Note the age of the section's main data."""
        return self.note_age(context, part, value)

    @staticmethod
    def note_age(context, part, value):
        """
        Note when ``value`` was fetched and whether it is out of date.

        ``data_stale``: a last good copy shown after a failure;
        ``data_old``: older than its lifetime plus a refresh interval,
        probably still being refreshed. ``<part>_ts`` is the epoch second
        the value was fetched at, shown in the browser's time zone.
        """
        if value.get("is_stale_copy"):
            context["data_stale"] = True
        elif value.get("is_old_copy"):
            context["data_old"] = True
        fetched_at = value.get("fetched_at")
        if fetched_at:
            # the interface telemetry and the trend are both "metrics"
            name = "metrics" if part == "interfaces" else part
            context[f"{name}_ts"] = str(int(fetched_at))
        return value

    def joined(self, name, inputs, build):
        """
        ``build()``, computed once per process for the same inputs.

        Inputs are the shared values (or None) the result is built from;
        their ``stored_at`` stamps identify them. Callers must not modify
        the result.
        """
        key = (
            _cache_key(self.request, name),
            tuple(
                (v.get("stored_at"), v.get("fetched_at")) if v else None
                for v in inputs
            ),
        )
        with _JOINS_LOCK:
            found = _JOINS.get(key)
        if found is None:
            found = build()
            with _JOINS_LOCK:
                _JOINS[key] = found
                while len(_JOINS) > JOINS_KEPT:
                    _JOINS.popitem(last=False)
        return found

    def note_network_refresh(self, context):
        """For "Networking API data from <time> (refreshed every …)"."""
        context["inventory_minutes"] = INVENTORY_CACHE_TTL // 60

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
                LOG.error("Invalid network health setting: %s", exc)
            # the index shows the message once, at the top of the page
            context["error_message"] = _(
                "Not available: invalid configuration (see the top of the "
                "page)."
            )
            return context
        if isinstance(exc, cache.StillComputing):
            LOG.info("Network health data not ready yet: %s", exc)
            context["loading"] = True
            context["error_message"] = _("Still loading…")
            return context
        if isinstance(exc, cache.DataUnavailable):
            # grian_ui.cache already logged the failure with its traceback
            LOG.warning("Network health data unavailable: %s", exc)
        else:
            # details (backend messages) go to the log, not to the page
            LOG.exception("Error loading network health data: %s", exc)
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


class NetworkHealthIndexView(_NetworkHealthMixin, generic.TemplateView):
    template_name = "admin/network_health/index.html"
    page_title = _("Network Health")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["ranges"] = list(RANGES)
        context["default_range"] = DEFAULT_RANGE
        context["refresh_seconds"] = REFRESH_SECONDS
        context["usual_offset"] = network_health.USUAL_OFFSET
        context["agent_limit"] = network_health.AGENT_LIMIT
        context["router_limit"] = network_health.ROUTER_LIMIT
        context["project_limit"] = network_health.PROJECT_LIMIT
        context["topology_minutes"] = TOPOLOGY_CACHE_TTL // 60
        context["inventory_minutes"] = INVENTORY_CACHE_TTL // 60
        context["range_steps"] = [
            _("%(range)s: every %(step)s")
            % {"range": key, "step": timeseries.RANGES[key][1]}
            for key in RANGES
        ]
        # every invalid setting at once, not one per page load
        errors = []
        try:
            context["thresholds"] = config.get_network_health_thresholds()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            context["rate_window"] = config.get_network_health_rate_window()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            # read when the datasource client is built: shown here, once
            if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
                config.get_openstack_keystone_url()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        context["error_messages"] = errors
        return context


class SummaryView(_NetworkHealthMixin, generic.TemplateView):
    """The verdict and the summary cards."""

    template_name = "admin/network_health/_summary.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_network_health_thresholds()
            self.note_network_refresh(context)
            # each source is shown as soon as it is available: when one
            # fails or is still computed, the verdict uses the other
            inventory = self.get_optional(
                context, "inventory", self.get_inventory
            )
            topology = (
                self.get_optional(context, "topology", self.get_topology)
                if inventory is not None
                else None
            )
            telemetry = self.get_optional(
                context, "interfaces", self.get_interfaces
            )
            if inventory is None and telemetry is None:
                if context.get("loading"):
                    raise cache.StillComputing("no network data yet")
                raise cache.ComputationFailed("no network data")
            projects = self.get_optional(
                context, "projects", self.get_projects
            )
            names = projects["names"] if projects else {}
            summary = dict(
                self.joined(
                    "summary",
                    (inventory, topology, telemetry, projects),
                    lambda: network_health.summarize(
                        inventory, topology, telemetry, thresholds, names
                    ),
                )
            )
            summary["in_text"] = format_rate(summary["in_bps"])
            summary["out_text"] = format_rate(summary["out_bps"])
            context["summary"] = summary
            context["thresholds"] = thresholds
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class AgentsView(_NetworkHealthMixin, generic.TemplateView):
    """Network agents: not alive first (Networking API)."""

    template_name = "admin/network_health/_agents.html"
    error_text = _(
        "Error loading the network agents from the Networking API. See "
        "the Horizon logs."
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            self.note_network_refresh(context)
            inventory = self.use_required(
                context, "inventory", self.get_inventory()
            )
            agents = inventory.get("agents", [])
            rows, types = network_health.agent_rows(agents)
            now = time.time()
            context["rows"] = [
                dict(
                    row,
                    heartbeat_age=timeseries.format_age(now - row["heartbeat"])
                    if row.get("heartbeat") is not None
                    else "",
                )
                for row in rows
            ]
            context["types"] = types
            context["total"] = len(agents)
            context["limit"] = network_health.AGENT_LIMIT
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class TrafficView(_NetworkHealthMixin, generic.TemplateView):
    """Cloud inbound and outbound traffic over the selected range."""

    template_name = "admin/network_health/_traffic.html"
    error_text = _("Error loading telemetry data. See the Horizon logs.")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        range_key = _range_key(self.request)
        context["range_key"] = range_key
        try:
            trend = self.use_required(
                context, "metrics", self.get_traffic_trend(range_key)
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


class RoutersView(_NetworkHealthMixin, generic.TemplateView):
    """Routers of every project: problems first, at most ROUTER_LIMIT."""

    template_name = "admin/network_health/_routers.html"
    error_text = _(
        "Error loading the routers from the Networking API. See the "
        "Horizon logs."
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_network_health_thresholds()
            self.note_network_refresh(context)
            inventory = self.use_required(
                context, "inventory", self.get_inventory()
            )
            topology = self.get_optional(
                context, "topology", self.get_topology
            )
            telemetry = self.get_optional(
                context, "interfaces", self.get_interfaces
            )
            names = self.get_project_names(context)
            rows = self.joined(
                "routers",
                (inventory, topology, telemetry),
                lambda: network_health.evaluate_routers(
                    inventory, topology, telemetry, thresholds
                ),
            )
            # copies: the joined rows are shared by the requests
            selected = [
                dict(
                    row,
                    project_name=names.get(
                        row.get("project_id"), row.get("project_id") or ""
                    ),
                    state_label=network_health.ROUTER_LABELS[row["state"]],
                )
                for row in network_health.select_routers(rows)
            ]
            context["rows"] = selected
            context["total"] = len(rows)
            context["judged_total"] = sum(1 for r in rows if r["judged"])
            context["limit"] = network_health.ROUTER_LIMIT
            context["thresholds"] = thresholds
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class DropsView(_NetworkHealthMixin, generic.TemplateView):
    """Drops and errors by project: at most PROJECT_LIMIT projects."""

    template_name = "admin/network_health/_drops.html"
    error_text = _("Error loading telemetry data. See the Horizon logs.")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_network_health_thresholds()
            telemetry = self.use_required(
                context, "metrics", self.get_interfaces()
            )
            names = self.get_project_names(context)
            rows, total = network_health.drops_by_project(
                telemetry, thresholds
            )
            context["rows"] = [
                dict(
                    row,
                    project_name=names.get(
                        row["project_id"], row["project_id"]
                    ),
                )
                for row in rows
            ]
            context["total"] = total
            context["limit"] = network_health.PROJECT_LIMIT
            interfaces = telemetry.get("interfaces", [])
            context["interfaces_total"] = len(interfaces)
            context["lossy_total"] = sum(
                1 for i in interfaces if network_health.is_lossy(i, thresholds)
            )
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class FloatingIPsView(_NetworkHealthMixin, generic.TemplateView):
    """Floating IP counts by state (Networking API)."""

    template_name = "admin/network_health/_floating_ips.html"
    error_text = _(
        "Error loading the floating IPs from the Networking API. See the "
        "Horizon logs."
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            self.note_network_refresh(context)
            inventory = self.use_required(
                context, "inventory", self.get_inventory()
            )
            counts = network_health.floating_ip_counts(
                inventory.get("floating_ips", [])
            )
            context["counts"] = counts
            total = counts["total"] or 1
            context["shares"] = {
                key: round(100.0 * counts[key] / total, 1)
                for key in ("active", "unassociated", "associated_down")
            }
        except Exception as exc:
            self.handle_error(context, exc)
        return context
