#
# SPDX-License-Identifier: Apache-2.0

"""
Project usage views (Project -> Telemetry -> Usage).

The index page is a light shell with the period selector; the summary
cards, the daily chart and the by-instance table are HTMX partials that
load and refresh themselves. They share one report per project and period
(two Prometheus queries, see grian_ui.datasource.prometheus_project_usage),
computed once and cached with grian_ui.cache.project_cache_key, so two
projects never share a value. The table also reads the project's server
list from the Compute API with the user's token (names, flavors, and the
instances that hold quota without running); it is optional: without it,
names come from the metrics.

The CSV export returns the table of the selected period from the same
cached report.
"""

import csv
import datetime
import io
import logging
import time
import urllib.parse
import uuid

from django.http import Http404
from django.http import HttpResponse
from django.urls import NoReverseMatch
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import generic

from grian_ui import cache
from grian_ui import config
from grian_ui import project_usage as pu
from grian_ui.datasource.api import get_datasource


LOG = logging.getLogger(__name__)

#: Refresh interval of the partials, in seconds (used by the templates).
REFRESH_SECONDS = 300
#: Seconds the report of the current month is shared: slightly shorter
#: than the refresh interval, so every refresh gets new data, computed
#: once per project.
CURRENT_CACHE_TTL = REFRESH_SECONDS - 5
#: Seconds the report of a past month is shared (it only changes when
#: retention deletes its oldest day, which changes the cache key).
PAST_CACHE_TTL = 3600
#: Seconds the Compute API server list is shared per project.
SERVERS_CACHE_TTL = 60
#: A copy older than its lifetime plus this margin is flagged "possibly
#: out of date" (still being refreshed, not a failure).
OLD_COPY_MARGIN = 30
#: Bump when the format of a cached value changes (see
#: grian_ui.cache.project_cache_key).
CACHE_VERSION = 2
CACHE_NAMESPACE = "project_usage"

_LOGGED_CONFIG_ERRORS = set()

#: Clock of the views (patched in tests).
_now = pu.utcnow


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


def list_servers(request):
    """
    The project's servers from the Compute API (or the fake datasource).

    Called with the user's token, as Horizon's own instance list: only the
    current project's servers, also for administrators.
    """
    if config.get_datasource_type() == config.DATASOURCE_FAKE:
        from grian_ui.datasource import fake_project_usage

        return fake_project_usage.get_servers()
    # imported lazily: unit tests run without horizon
    from openstack_dashboard.api import nova

    servers, _more = nova.server_list(request)
    flavor_names = None
    result = []
    for server in servers:
        flavor = getattr(server, "flavor", None) or {}
        name = flavor.get("original_name")
        if not name and flavor.get("id"):
            # older compute API microversions only give the flavor ID
            if flavor_names is None:
                try:
                    flavor_names = {
                        f.id: f.name for f in nova.flavor_list(request)
                    }
                except Exception as exc:
                    LOG.info("Could not list the flavors: %s", exc)
                    flavor_names = {}
            name = flavor_names.get(flavor["id"], flavor["id"])
        result.append(
            {
                "id": server.id,
                "name": server.name,
                "status": server.status,
                "flavor": name,
            }
        )
    return result


def instance_metrics_url():
    """The Instance metrics page, or None without that panel."""
    try:
        return reverse("horizon:project:instance_metrics:index")
    except NoReverseMatch:
        return None


def instance_link(base_url, instance_id):
    """Link to one instance on the Instance metrics page, or None."""
    if not base_url:
        return None
    return f"{base_url}?{urllib.parse.urlencode({'instance': instance_id})}"


def _note_age(context, value):
    """Flag a failed refresh (stale) or a copy being refreshed (old)."""
    context["data_stale"] = context.get("data_stale") or value.get(
        "is_stale_copy", False
    )
    context["data_old"] = context.get("data_old") or value.get(
        "is_old_copy", False
    )


def coverage(period, report=None):
    """
    What the totals cover: ``range`` (UTC), ``days_covered``,
    ``days_total``, ``complete`` and ``first_data`` (the first hour with
    usage, when later than the start of the range).
    """
    end = period["end"]
    if report and report.get("end"):
        end = min(end, datetime.datetime.fromtimestamp(report["end"], pu.UTC))
    first = pu.first_data(report) if report else None
    first_at = None
    if first is not None and first > period["start"].timestamp():
        first_at = pu.format_moment(
            datetime.datetime.fromtimestamp(first, pu.UTC)
        )
    return {
        "range": pu.format_range(period["start"], end),
        "days_covered": period["days_covered"],
        "days_total": period["days_total"],
        "complete": period["complete"],
        "first_data": first_at,
    }


def retention_notes(period):
    """
    ``lost_range``: the deleted days of an incomplete month
    (``11/1–11/3``). Dates are formatted here, in UTC: the date filter
    would convert them to the server's time zone.
    """
    notes = {"lost_range": None}
    if period["incomplete"]:
        first = period["month_start"]
        last = period["start"] - datetime.timedelta(days=1)
        notes["lost_range"] = (
            pu.format_day(first)
            if first.date() == last.date()
            else f"{pu.format_day(first)}–{pu.format_day(last)}"
        )
    return notes


class _UsageMixin:
    """Shared helpers: period, cached data, errors."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Unique per response: element ids inside partials must not repeat
        # across swaps (htmx settles new elements with the old attributes).
        context["uid"] = uuid.uuid4().hex[:12]
        return context

    # Parameters -----------------------------------------------------------

    def get_now(self):
        if getattr(self, "_now_value", None) is None:
            self._now_value = _now()
        return self._now_value

    def get_period(self):
        """The selected month (default: the current one)."""
        now = self.get_now()
        retention = config.get_metrics_retention_days()
        year_month = pu.parse_period(self.request.GET.get("period"), now)
        if year_month is None:
            year_month = (now.year, now.month)
        return pu.period_info(*year_month, now, retention)

    # Data -----------------------------------------------------------------

    def get_or_compute(self, name, ttl, compute):
        """
        cache.get_or_compute with a project key, after checking settings.

        Values are stamped with ``stored_at`` when computed; one older than
        its lifetime plus OLD_COPY_MARGIN is marked ``is_old_copy``.
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

    def get_report(self, period):
        """The usage report of ``period``, shared per project."""
        start, end = period["start"], period["end"]

        def compute():
            report = self.get_client().get_project_usage(
                self.request, start, end
            )
            return dict(report, fetched_at=time.time())

        ttl = CURRENT_CACHE_TTL if period["is_current"] else PAST_CACHE_TTL
        # the start moves when retention deletes a day: a new value
        name = f"report:{period['key']}:{start:%Y%m%d}"
        return self.get_or_compute(name, ttl, compute)

    def get_servers(self, context):
        """
        The project's servers (Compute API), or None if not available.

        Optional: the table renders without it (names from the metrics);
        ``servers_failed`` or ``servers_loading`` says why it is missing.
        """
        try:
            value = self.get_or_compute(
                "servers",
                SERVERS_CACHE_TTL,
                lambda: {"servers": list_servers(self.request)},
            )
        except cache.DataUnavailable as exc:
            loading = isinstance(exc, cache.StillComputing)
            LOG.info("Compute API server list not available: %s", exc)
            context["servers_loading" if loading else "servers_failed"] = True
            context["loading"] = context.get("loading") or loading
            return None
        context["servers_stale"] = value.get("is_stale_copy", False)
        return value["servers"]

    def handle_error(self, context, exc):
        if isinstance(exc, cache.ComputationFailed) and isinstance(
            exc.__cause__, config.ConfigurationError
        ):
            # raised inside a shared computation (e.g. building the client)
            exc = exc.__cause__
        if isinstance(exc, config.ConfigurationError):
            if str(exc) not in _LOGGED_CONFIG_ERRORS:
                _LOGGED_CONFIG_ERRORS.add(str(exc))
                LOG.error("Invalid project usage setting: %s", exc)
            context.pop("loading", None)
            context["error_message"] = _(
                "Not available: invalid configuration (see the top of the "
                "page)."
            )
            return context
        if isinstance(exc, cache.StillComputing):
            # another request is computing the shared data: show a loading
            # state, the template retries shortly
            LOG.info("Project usage data not ready yet: %s", exc)
            context["loading"] = True
            context["error_message"] = _("Still loading telemetry data…")
            return context
        # an error is not "loading" (no quick retry)
        context.pop("loading", None)
        if isinstance(exc, cache.DataUnavailable):
            # grian_ui.cache already logged the failure with its traceback
            LOG.warning("Project usage data unavailable: %s", exc)
        else:
            # details (backend messages) go to the log, not to the page
            LOG.exception("Error loading project usage data: %s", exc)
        context["error_message"] = _(
            "Error loading telemetry data. See the Horizon logs for details."
        )
        return context

    def section_context(self, context):
        """
        The period of a partial, or None (an error or a locked month).

        Fills ``period`` and ``coverage``.
        """
        try:
            period = self.get_period()
        except config.ConfigurationError as exc:
            self.handle_error(context, exc)
            return None
        context["period"] = period
        context["coverage"] = coverage(period)
        if not period["available"]:
            context["locked"] = True
            return None
        return period


class UsageIndexView(_UsageMixin, generic.TemplateView):
    template_name = "project/telemetry_usage/index.html"
    page_title = _("Usage")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["refresh_seconds"] = REFRESH_SECONDS
        context["refresh_minutes"] = REFRESH_SECONDS // 60
        context["step_minutes"] = pu.STEP_SECONDS // 60
        # every invalid setting at once, not one per page load
        errors = []
        try:
            context["thresholds"] = config.get_project_usage_thresholds()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            retention = config.get_metrics_retention_days()
            context["retention_days"] = retention
            periods = pu.list_periods(self.get_now(), retention)
            context["periods"] = periods
            context["default_period"] = periods[0]["key"]
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            # read when the datasource client is built: shown here, once
            _check_datasource_settings()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        context["error_messages"] = errors
        return context


class SummaryView(_UsageMixin, generic.TemplateView):
    """Coverage, retention notes and the four summary cards."""

    template_name = "project/telemetry_usage/_summary.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        period = self.section_context(context)
        if period is None:
            return context
        context.update(retention_notes(period))
        try:
            report = self.get_report(period)
            rows = pu.instance_rows(
                report, None, config.DEFAULT_PROJECT_USAGE_THRESHOLDS
            )
            totals = pu.totals(rows, report)
            context["coverage"] = coverage(period, report)
            context["totals"] = totals
            context["cards"] = {
                "cpu_used": pu.format_amount(totals["cpu_hours"]),
                "cpu_held": pu.format_amount(totals["vcpu_hours"]),
                "cpu_ratio": pu.format_percent(totals["cpu_ratio"]),
                "ram_used": pu.format_amount(totals["ram_gb_hours_used"]),
                "ram_held": pu.format_amount(totals["ram_gb_hours_held"]),
                "ram_ratio": pu.format_percent(totals["ram_ratio"]),
                "net_total": pu.format_bytes(totals["net_total"]),
                "net_in": pu.format_bytes(totals["net_in"]),
                "net_out": pu.format_bytes(totals["net_out"]),
                "storage": pu.format_amount(totals["storage_gb_hours"]),
                "volumes": pu.format_amount(totals["volume_gb_hours"]),
                "snapshots": pu.format_amount(totals["snapshot_gb_hours"]),
            }
            context["has_usage"] = bool(rows) or totals["storage_gb_hours"] > 0
            fetched_at = report.get("fetched_at")
            context["fetched_ts"] = (
                str(int(fetched_at)) if fetched_at else None
            )
            _note_age(context, report)
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class ChartView(_UsageMixin, generic.TemplateView):
    """Daily held and used amounts (CPU, memory, network)."""

    template_name = "project/telemetry_usage/_chart.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        period = self.section_context(context)
        if period is None:
            return context
        try:
            report = self.get_report(period)
            chart = pu.daily(report, period)
            context["chart"] = chart
            context["has_data"] = any(
                v for v in chart["cpu"]["held"] if v is not None
            ) or any(v for v in chart["net"]["used"] if v is not None)
            context["has_bands"] = bool(chart["bands"])
            context["coverage"] = coverage(period, report)
            _note_age(context, report)
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class TableView(_UsageMixin, generic.TemplateView):
    """Every instance with usage in the period, with totals."""

    template_name = "project/telemetry_usage/_table.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        period = self.section_context(context)
        if period is None:
            return context
        try:
            thresholds = config.get_project_usage_thresholds()
            context["thresholds"] = thresholds
            report = self.get_report(period)
            _note_age(context, report)
            servers = self.get_servers(context)
            rows = pu.instance_rows(report, servers, thresholds)
            base_url = instance_metrics_url()
            for row in rows:
                row["url"] = instance_link(base_url, row["id"])
                row["shown"] = _shown(row)
                row["cpu_ratio_shown"] = pu.format_percent(row["cpu_ratio"])
                row["cpu_bar"] = min(100.0, row["cpu_ratio"] or 0.0)
            totals = pu.totals(rows, report)
            totals["shown"] = _shown(totals)
            totals["cpu_ratio_shown"] = pu.format_percent(totals["cpu_ratio"])
            context["rows"] = rows
            context["totals"] = totals
            # what holds quota now without usage: only meaningful for the
            # current month (the list is the current state)
            if period["is_current"] and servers is not None:
                context["not_listed"] = pu.not_listed(servers, rows)
        except Exception as exc:
            self.handle_error(context, exc)
        return context


def _shown(values):
    """Display forms of the table fields."""
    shown = {
        field: pu.format_amount(values[field])
        for field in pu.SUM_FIELDS
        if field not in ("net_in", "net_out")
    }
    shown["net_in"] = pu.format_bytes(values["net_in"])
    shown["net_out"] = pu.format_bytes(values["net_out"])
    return shown


class CsvView(_UsageMixin, generic.View):
    """The by-instance table of the selected period, as CSV."""

    def get(self, request, *args, **kwargs):
        try:
            period = self.get_period()
        except config.ConfigurationError as exc:
            raise Http404(str(exc)) from exc
        if not period["available"]:
            raise Http404("this period is outside the kept metrics")
        try:
            report = self.get_report(period)
        except (config.ConfigurationError, cache.DataUnavailable) as exc:
            # a setting checked before the cache (to build the client), or
            # read inside the shared computation (ComputationFailed with
            # the ConfigurationError as its cause): the same answer
            if isinstance(exc, config.ConfigurationError) or isinstance(
                exc.__cause__, config.ConfigurationError
            ):
                self.handle_error({}, exc)
                return HttpResponse(
                    _("Not available: invalid configuration."),
                    status=503,
                    content_type="text/plain; charset=utf-8",
                )
            LOG.warning("Project usage data unavailable for CSV: %s", exc)
            return HttpResponse(
                _("Telemetry data is not available right now. Try again."),
                status=503,
                content_type="text/plain; charset=utf-8",
            )
        # without the server list, names and flavors come from the metrics
        servers = self.get_servers({})
        rows = pu.instance_rows(
            report, servers, config.DEFAULT_PROJECT_USAGE_THRESHOLDS
        )
        end = min(
            period["end"],
            datetime.datetime.fromtimestamp(report["end"], pu.UTC)
            if report.get("end")
            else period["end"],
        )
        covered = dict(period, end=end)
        output = io.StringIO()
        csv.writer(output, lineterminator="\n").writerows(
            pu.csv_rows(rows, covered)
        )
        response = HttpResponse(
            output.getvalue(), content_type="text/csv; charset=utf-8"
        )
        project_name = getattr(request.user, "project_name", None)
        filename = pu.csv_filename(project_name, covered)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "no-store"
        return response
