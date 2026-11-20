#
# SPDX-License-Identifier: Apache-2.0

"""
Instance metrics views (Project -> Telemetry -> Instance metrics).

One instance of the current project, chosen with ``?instance=<server
UUID>`` (other panels link here) or with the picker. The index page is a
light shell: the instance header, the current-value tiles and each group
of charts are HTMX partials that load and refresh themselves.

Every partial checks the instance again: a valid UUID, found by the
Compute API with the user's token, and owned by the current project. The
Compute API returns other projects' instances to administrators, so the
ownership check is what keeps an administrator browsing the Project
dashboard to the current project, with the project matcher in every query.

Values are cached per project (grian_ui.cache.project_cache_key) and
instance, and shared by the partials and the members of the project: the
server for SERVER_CACHE_TTL seconds, each group of series per range.
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
from grian_ui import project
from grian_ui import project_instance_metrics as pim
from grian_ui import timeseries
from grian_ui.datasource import fake_project_instance_metrics as fake_data
from grian_ui.datasource.api import get_datasource


LOG = logging.getLogger(__name__)

#: Refresh interval of the partials, in seconds (used by the templates).
REFRESH_SECONDS = 30
#: Seconds the server (status, flavor, addresses) is shared by the
#: partials of the project. Slightly shorter than the refresh interval.
SERVER_CACHE_TTL = REFRESH_SECONDS - 5
#: Seconds the instance list of the picker is shared.
SERVERS_CACHE_TTL = 60
#: Seconds each group of series is shared, per range (coarser ranges
#: change slower).
SERIES_CACHE_TTL = {"1h": 25, "6h": 55, "24h": 240, "7d": 600}
#: A copy older than its lifetime plus this margin is flagged "possibly out
#: of date" (still being refreshed, not a failure).
OLD_COPY_MARGIN = 30
#: Instances listed in the picker at most.
PICKER_LIMIT = 500
#: Bump when the format of a cached value changes (see
#: grian_ui.cache.project_cache_key).
CACHE_VERSION = 1
CACHE_NAMESPACE = "project_instance_metrics"

_LOGGED_CONFIG_ERRORS = set()


class InstanceNotFound(Exception):
    """No such instance in the current project (or not visible)."""


def _cache_key(request, name):
    return cache.project_cache_key(
        request, CACHE_NAMESPACE, name, version=CACHE_VERSION
    )


def _is_fake():
    return config.get_datasource_type() == config.DATASOURCE_FAKE


def _check_settings():
    """
    Raise ConfigurationError for the settings the computations read.

    Only the request that runs a shared computation sees its cause:
    checking first gives every section (and the index) the same message.
    """
    config.get_project_instance_metrics_thresholds()
    config.get_project_instance_metrics_rate_window()
    config.get_metrics_retention_days()
    if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
        config.get_openstack_keystone_url()


def _is_not_found(exc):
    """A Compute API 404 (novaclient NotFound, or an SDK exception)."""
    return (
        getattr(exc, "code", None) == 404
        or getattr(exc, "status_code", None) == 404
        or type(exc).__name__ in ("NotFound", "ResourceNotFound")
    )


def list_servers(request):
    """
    ``[{"id", "name", "status"}]``: the current project's instances.

    From the Compute API with the user's token (the project's instances,
    for administrators too), or the sample servers of the fake datasource.
    """
    if _is_fake():
        found = fake_data.servers(project.project_id(request))
    else:
        # imported lazily: unit tests run without horizon
        from openstack_dashboard.api import nova

        servers, _more = nova.server_list(request)
        found = [
            {"id": s.id, "name": s.name, "status": s.status} for s in servers
        ]
    rows = [
        {"id": s["id"], "name": s["name"] or s["id"], "status": s["status"]}
        for s in found
    ]
    return sorted(rows, key=lambda s: (s["name"].lower(), s["id"]))


def fetch_server(request, instance_id):
    """
    The instance as the page shows it, or None if not found.

    Returns:
        dict: ``id``, ``name``, ``status``, ``project_id``, ``flavor``
        (see project_instance_metrics.flavor_of), ``addresses`` (see
        addresses_of), ``created_at`` (epoch) and ``fault`` (message)
    """
    instance_id = pim.validate_instance_id(instance_id)
    if _is_fake():
        return fake_data.server(project.project_id(request), instance_id)
    from openstack_dashboard.api import nova

    try:
        server = nova.server_get(request, instance_id)
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise
    flavor = getattr(server, "flavor", None) or {}
    details = None
    if "vcpus" not in flavor and flavor.get("id"):
        # before microversion 2.47 the server only names its flavor; a
        # deleted or private flavor cannot be read: show what is known
        try:
            found = nova.flavor_get(request, flavor["id"])
            details = {
                "name": found.name,
                "vcpus": found.vcpus,
                "ram": found.ram,
                "disk": found.disk,
            }
        except Exception as exc:
            LOG.info("Could not read flavor %s: %s", flavor["id"], exc)
    fault = getattr(server, "fault", None)
    return {
        "id": server.id,
        "name": server.name or server.id,
        "status": server.status,
        "project_id": getattr(server, "tenant_id", None),
        "flavor": pim.flavor_of(flavor, details),
        "addresses": pim.addresses_of(getattr(server, "addresses", None)),
        "created_at": pim.parse_time(getattr(server, "created", None)),
        "fault": fault.get("message") if isinstance(fault, dict) else None,
    }


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def instance_links(instance_id):
    """Horizon's instance pages, each None when not installed."""
    detail = _url("horizon:project:instances:detail", instance_id)
    return {
        "detail": detail,
        "console": f"{detail}?tab=instance_details__console"
        if detail
        else None,
        "resize": _url("horizon:project:instances:resize", instance_id),
    }


def compute_url():
    """The project Compute (Telemetry) page, or None without it."""
    return _url("horizon:project:telemetry_compute:index")


class _InstanceMetricsMixin:
    """Shared helpers: request parameters, cached data, errors."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Unique per response: element ids inside partials must not repeat
        # across swaps (htmx settles new elements with the old attributes,
        # which resets the size of a canvas after its chart is drawn).
        context["uid"] = uuid.uuid4().hex[:12]
        return context

    # Parameters -----------------------------------------------------------

    def get_instance_param(self):
        """``(raw value, validated UUID or None)`` of ``?instance=``."""
        raw = (self.request.GET.get("instance") or "").strip()
        return raw, raw if pim.is_instance_id(raw) else None

    def get_ranges(self):
        return pim.available_ranges(config.get_metrics_retention_days())

    def get_range_key(self, ranges=None):
        ranges = ranges or self.get_ranges()
        key = self.request.GET.get("range")
        return key if key in ranges else pim.default_range(ranges)

    # Data -----------------------------------------------------------------

    def get_or_compute(self, name, ttl, compute):
        """
        cache.get_or_compute with a project key, after the settings check.

        Values are stamped with ``stored_at``: one older than its lifetime
        plus OLD_COPY_MARGIN is marked ``is_old_copy`` (probably still
        being refreshed, not a failure).
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
            and time.time() - stored_at > ttl + OLD_COPY_MARGIN
        ):
            value = dict(value, is_old_copy=True)
        return value

    def get_client(self):
        if getattr(self, "_client", None) is None:
            self._client = get_datasource(self.request)
        return self._client

    def get_server(self, instance_id):
        """
        The instance, if it is one of the current project's.

        Raises InstanceNotFound otherwise: not found, not visible, or owned
        by another project (the Compute API shows those to administrators).
        """

        def compute():
            return {"server": fetch_server(self.request, instance_id)}

        value = self.get_or_compute(
            f"server:{instance_id}", SERVER_CACHE_TTL, compute
        )
        server = value.get("server")
        if not pim.instance_belongs_to_project(
            server, project.project_id(self.request)
        ):
            if server is not None:
                LOG.warning(
                    "Refused instance %s of another project (current "
                    "project %s)",
                    instance_id,
                    project.project_id(self.request),
                )
            raise InstanceNotFound(instance_id)
        return server, value

    def get_servers(self):
        """The picker's list (at most PICKER_LIMIT), shared per project."""

        def compute():
            return {"servers": list_servers(self.request)}

        return self.get_or_compute("servers", SERVERS_CACHE_TTL, compute)

    def get_series(self, group, instance_id, range_key):
        """
        One group of series over ``range_key``, on the range's grid.

        Returns:
            tuple: ``({series: [[t, v_or_None], ...]}, cached value)``
        """

        def compute():
            now = time.time()
            series = self.get_client().get_project_instance_metrics(
                project.project_matcher(self.request),
                group,
                instance_id,
                range_key,
                config.get_project_instance_metrics_rate_window(),
                at=now,
            )
            return {"series": series, "fetched_at": now}

        value = self.get_or_compute(
            f"series:{group}:{range_key}:{instance_id}",
            SERIES_CACHE_TTL[range_key],
            compute,
        )
        fetched_at = value["fetched_at"]
        filled = {
            name: timeseries.gap_filled_series(
                value["series"].get(name, []), range_key, fetched_at
            )
            for name in pim.SERIES[group]
        }
        return filled, value

    def handle_error(self, context, exc):
        """Fill ``context`` with the error state of a section."""
        if isinstance(exc, InstanceNotFound):
            context["not_found"] = True
            context["error_message"] = _(
                "This instance does not exist, or it is not in the current "
                "project."
            )
            return context
        if isinstance(exc, cache.ComputationFailed) and isinstance(
            exc.__cause__, config.ConfigurationError
        ):
            exc = exc.__cause__
        if isinstance(exc, config.ConfigurationError):
            if str(exc) not in _LOGGED_CONFIG_ERRORS:
                _LOGGED_CONFIG_ERRORS.add(str(exc))
                LOG.error("Invalid instance metrics setting: %s", exc)
            context["error_message"] = _(
                "Not available: invalid configuration (see the top of the "
                "page)."
            )
            return context
        if isinstance(exc, cache.StillComputing):
            # another request is computing the shared data: the template
            # retries shortly
            LOG.info("Instance metrics not ready yet: %s", exc)
            context["loading"] = True
            context["error_message"] = _("Still loading telemetry data…")
            return context
        if isinstance(exc, cache.DataUnavailable):
            # grian_ui.cache already logged the failure with its traceback
            LOG.warning("Instance metrics unavailable: %s", exc)
        else:
            # details (backend messages) go to the log, not to the page
            LOG.exception("Error loading instance metrics: %s", exc)
        context["error_message"] = _(
            "Error loading telemetry data. See the Horizon logs for details."
        )
        return context


def _note_age(context, value):
    """Flag a failed refresh (stale) or a copy being refreshed (old)."""
    context["data_stale"] = context.get("data_stale") or value.get(
        "is_stale_copy", False
    )
    context["data_old"] = context.get("data_old") or value.get(
        "is_old_copy", False
    )


class IndexView(_InstanceMetricsMixin, generic.TemplateView):
    template_name = "project/instance_metrics/index.html"
    page_title = _("Instance metrics")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["refresh_seconds"] = REFRESH_SECONDS
        context["groups"] = pim.GROUPS
        context["tile_range"] = pim.TILE_RANGE
        context["compute_url"] = compute_url()
        raw, instance_id = self.get_instance_param()
        context["instance_id"] = instance_id
        # every invalid setting at once, not one per page load
        errors = []
        for read in (
            config.get_project_instance_metrics_thresholds,
            config.get_project_instance_metrics_rate_window,
            config.get_metrics_retention_days,
        ):
            try:
                context[read.__name__[len("get_") :]] = read()
            except config.ConfigurationError as exc:
                errors.append(str(exc))
        try:
            if config.get_datasource_type() == config.DATASOURCE_PROMETHEUS:
                config.get_openstack_keystone_url()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        context["error_messages"] = errors
        if errors:
            return context
        ranges = self.get_ranges()
        context["ranges"] = ranges
        context["range"] = self.get_range_key(ranges)
        context["range_steps"] = [
            _("%(range)s: every %(step)s, rates over %(window)s")
            % {
                "range": key,
                "step": timeseries.RANGES[key][1],
                "window": pim.rate_window(
                    key, context["project_instance_metrics_rate_window"]
                ),
            }
            for key in ranges
        ]
        self._picker(context, instance_id)
        if raw and instance_id is None:
            context["invalid"] = True
        elif instance_id is not None:
            try:
                context["server"], _value = self.get_server(instance_id)
            except cache.StillComputing:
                # the partials show "loading" and retry
                context["server"] = None
            except Exception as exc:
                self.handle_error(context, exc)
        return context

    def _picker(self, context, instance_id):
        try:
            servers = self.get_servers()["servers"]
        except Exception as exc:
            if not isinstance(exc, cache.DataUnavailable):
                LOG.exception("Could not list the project's instances")
            context["picker_error"] = True
            servers = []
        context["picker_more"] = max(0, len(servers) - PICKER_LIMIT)
        context["servers"] = servers[:PICKER_LIMIT]
        context["picked"] = any(s["id"] == instance_id for s in servers)


class _InstanceSectionView(_InstanceMetricsMixin, generic.TemplateView):
    """A partial of one instance: checks it, then fills the section."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        _raw, instance_id = self.get_instance_param()
        context["instance_id"] = instance_id
        try:
            if instance_id is None:
                raise InstanceNotFound("missing or invalid instance ID")
            server, value = self.get_server(instance_id)
            context["server"] = server
            context["state"] = pim.state_of(server["status"])
            _note_age(context, value)
            self.fill(context, server)
        except Exception as exc:
            self.handle_error(context, exc)
        return context

    def fill(self, context, server):
        raise NotImplementedError


class InfoView(_InstanceSectionView):
    """Status, flavor, ID, addresses and links; why there are no metrics."""

    template_name = "project/instance_metrics/_info.html"

    def fill(self, context, server):
        ranges = self.get_ranges()
        range_key = self.get_range_key(ranges)
        context["range"] = range_key
        context["links"] = instance_links(server["id"])
        context["compute_url"] = compute_url()
        context["retention_days"] = config.get_metrics_retention_days()
        context["coverage"] = pim.coverage(
            server.get("created_at"), range_key, time.time()
        )
        flavor = server["flavor"]
        context["ram"] = (
            pim.format_mib(flavor["ram_mib"])
            if flavor.get("ram_mib")
            else None
        )


class TilesView(_InstanceSectionView):
    """Current values, from the last TILE_RANGE of every group."""

    template_name = "project/instance_metrics/_tiles.html"

    def fill(self, context, server):
        if context["state"] != pim.STATE_ACTIVE:
            return
        groups = {}
        failed = []
        fetched = []
        for group in pim.GROUPS:
            try:
                series, value = self.get_series(
                    group, server["id"], pim.TILE_RANGE
                )
            except cache.DataUnavailable as exc:
                failed.append(exc)
                continue
            groups[group] = series
            fetched.append(value["fetched_at"])
            _note_age(context, value)
        if not groups:
            # nothing to show: the state of the first failure
            raise failed[0]
        if failed:
            context["partial_failure"] = True
            context["loading"] = any(
                isinstance(exc, cache.StillComputing) for exc in failed
            )
        thresholds = config.get_project_instance_metrics_thresholds()
        context["tiles"] = pim.tiles(groups, server["flavor"], thresholds)
        context["has_data"] = any(
            v is not None
            for series in groups.values()
            for points in series.values()
            for _t, v in points
        )
        context["fetched_ts"] = str(int(min(fetched)))
        context["tile_range"] = pim.TILE_RANGE


class ChartsView(_InstanceSectionView):
    """The charts of one group over the selected range."""

    template_name = "project/instance_metrics/_charts.html"

    def get_context_data(self, **kwargs):
        self.group = kwargs.get("group")
        if self.group not in pim.GROUPS:
            self.group = pim.GROUP_CPU
        context = super().get_context_data(**kwargs)
        context["group"] = self.group
        context["group_label"] = pim.GROUP_LABELS[self.group]
        return context

    def fill(self, context, server):
        if context["state"] != pim.STATE_ACTIVE:
            return
        range_key = self.get_range_key()
        context["range"] = range_key
        series, value = self.get_series(self.group, server["id"], range_key)
        _note_age(context, value)
        thresholds = config.get_project_instance_metrics_thresholds()
        charts = pim.charts(self.group, series, server["flavor"], thresholds)
        first = next(iter(series.values()))
        context["charts"] = charts
        context["chart"] = {
            "range": range_key,
            "timestamps": [t for t, _v in first],
            # user-controlled: serialised with json_script only
            "instance": server["name"],
            "charts": charts,
        }
