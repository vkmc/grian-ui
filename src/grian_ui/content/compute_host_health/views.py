#
# SPDX-License-Identifier: Apache-2.0

"""
Compute host health views.

The index page is a light shell; every section is an HTMX partial that
loads and refreshes itself. Each partial renders a bounded amount of data
(cards, at most 10 hosts, a few bands, 3 fleet series) whatever the number
of compute hosts.

Fleet-wide data (the per-host snapshot, the list of stale hosts and the
fleet trend) and the details and trends of the hosts on display are cached
in the Horizon cache and shared by the partials and by the administrators of
a region. A short lock prevents the partials of one refresh, which fire at the
same moment, from all missing the cache and querying in parallel (see
grian_ui.cache).
"""

import logging
import time
import uuid

from django.template import defaultfilters
from django.utils.translation import gettext_lazy as _
from django.views import generic

from grian_ui import cache
from grian_ui import compute_health
from grian_ui import config
from grian_ui import timeseries
from grian_ui.datasource.api import get_datasource


LOG = logging.getLogger(__name__)

#: Refresh interval of the partials, in seconds (used by the templates).
REFRESH_SECONDS = 30
#: Seconds the per-host snapshot is shared. Slightly shorter than the
#: refresh interval so every refresh gets new data, computed once.
SNAPSHOT_CACHE_TTL = REFRESH_SECONDS - 5
#: Seconds the fleet trend and the per-host trends are shared, per range
#: (coarser ranges change slower).
FLEET_TREND_CACHE_TTL = {"1h": 25, "6h": 60, "24h": 300, "7d": 900}
#: Seconds the list of hosts silent for up to STALE_LOOKBACK is shared (its
#: query is the most expensive one; newer stale hosts come with every
#: snapshot, see compute_health.STALE_RECENT_LOOKBACK).
STALE_LIST_CACHE_TTL = 300
#: Bump when the format of a cached value changes (see
#: grian_ui.cache.cache_key).
CACHE_VERSION = 2
CACHE_NAMESPACE = "compute_host_health"


_LOGGED_CONFIG_ERRORS = set()

# Notes for optional data that is not available: (failed, still loading)
OPTIONAL_NOTES = {
    "details": (
        _("The CPU breakdown could not be loaded."),
        _("Loading the CPU breakdown…"),
    ),
    "trends": (
        _("The trends could not be loaded."),
        _("Loading the trends…"),
    ),
    "fleet": (
        _("The fleet median could not be loaded."),
        _("Loading the fleet median…"),
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


class _ComputeHostHealthMixin:
    """Shared helpers: lazy datasource client, cached data, errors."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Unique per response: element ids inside partials must not repeat
        # across swaps, or htmx "settles" the new element with the old
        # element's attributes after the chart is drawn, which resets the
        # canvas size and clears the chart.
        context["uid"] = uuid.uuid4().hex[:12]
        return context

    def get_or_compute(self, name, ttl, compute):
        """
        cache.get_or_compute, after checking the datasource settings.

        The cache marks a last good copy served after a failure, not one
        served while another request is still refreshing it (a slow or
        hanging backend). Values are stamped with ``stored_at`` when their
        computation ends: one older than its lifetime plus a refresh
        interval is marked here as well. (The age compares the clocks of
        the Horizon nodes sharing the cache: keep them in sync.)
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
            and time.time() - stored_at > ttl + REFRESH_SECONDS
        ):
            value = dict(value, is_stale_copy=True)
        return value

    def get_client(self):
        if getattr(self, "_client", None) is None:
            self._client = get_datasource(self.request)
        return self._client

    def get_snapshot(self):
        """
        The fleet snapshot: reporting hosts plus every stale host.

        Joins the short-lived snapshot (reporting and recently stale hosts)
        with the longer-lived list of hosts silent for up to STALE_LOOKBACK.
        """

        def compute():
            snapshot = self.get_client().get_compute_host_snapshot()
            return dict(snapshot, fetched_at=time.time())

        def compute_stale():
            return {
                "stale": self.get_client().get_compute_stale_hosts(),
                "fetched_at": time.time(),
            }

        snapshot = self.get_or_compute("snapshot", SNAPSHOT_CACHE_TTL, compute)
        try:
            stale_list = self.get_or_compute(
                "stale",
                STALE_LIST_CACHE_TTL,
                compute_stale,
            )
            # an old copy misses nothing: the snapshot's lookback covers
            # the oldest copy that can be served (see
            # compute_health.STALE_RECENT_LOOKBACK)
            older, incomplete = stale_list["stale"], False
        except cache.StillComputing:
            # cold cache: show "loading" and retry, like the other data
            raise
        except cache.DataUnavailable:
            # the list failed and there is no copy: never block the page,
            # but hosts silent for longer than the lookback are missing
            older, incomplete = [], True
        reporting = {h["name"] for h in snapshot["hosts"]}
        stale = {h["name"]: h for h in older if h["name"] not in reporting}
        stale.update({h["name"]: h for h in snapshot.get("stale", [])})
        return dict(
            snapshot,
            stale=list(stale.values()),
            stale_list_incomplete=incomplete,
        )

    def get_host_details(self, names):
        """
        Latest CPU breakdown of a page of hosts, shared like the snapshot.

        Every administrator viewing the same hosts gets the same cached
        result: two queries per refresh.

        Returns:
            dict: ``{"details": {name: {...}}}``, with ``is_stale_copy``
            when a last good copy was served after a failure
        """
        names = sorted(set(names))

        def compute():
            return {
                "details": self.get_client().get_compute_host_details(names)
            }

        return self.get_or_compute(
            f"details:{','.join(names)}", SNAPSHOT_CACHE_TTL, compute
        )

    def get_host_trends(self, names, range_key):
        """
        CPU trends of a page of hosts for ``range_key``: one query.

        Returns:
            dict: ``{"trends": {name: points}, "fetched_at": epoch}``;
            grids are built from ``fetched_at``, the time the trends were
            evaluated at
        """
        names = sorted(set(names))

        def compute():
            now = time.time()
            return {
                "trends": self.get_client().get_compute_host_trends(
                    names, range_key, at=now
                ),
                "fetched_at": now,
            }

        return self.get_or_compute(
            f"trends:{range_key}:{','.join(names)}",
            FLEET_TREND_CACHE_TTL[range_key],
            compute,
        )

    def get_optional(self, context, part, fetch):
        """
        Optional data: on DataUnavailable note it and return None.

        The section still renders; ``<part>_unavailable`` and
        ``<part>_loading`` describe what is missing, ``partial_notes``
        says it, and ``loading`` makes the section retry shortly.
        """
        try:
            value = fetch()
        except cache.DataUnavailable as exc:
            loading = isinstance(exc, cache.StillComputing)
            context[f"{part}_unavailable"] = True
            context[f"{part}_loading"] = loading
            context["loading"] = context.get("loading") or loading
            context.setdefault("partial_notes", []).append(
                OPTIONAL_NOTES[part][loading]
            )
            return None
        context["data_stale"] = context.get("data_stale") or value.get(
            "is_stale_copy", False
        )
        return value

    def get_fleet_trend(self, range_key):
        """
        Fleet trend for ``range_key`` with the time it was fetched.

        Returns:
            dict: ``{"series": {"p50": ..., "p95": ..., "max": ...},
            "fetched_at": epoch}``; grids must be built from ``fetched_at``
            so a cached trend does not grow a false gap at the end.
        """

        def compute():
            # the query is evaluated at the time the grid is built from
            now = time.time()
            return {
                "series": self.get_client().get_compute_fleet_trend(
                    range_key, at=now
                ),
                "fetched_at": now,
            }

        return self.get_or_compute(
            f"fleet:{range_key}",
            FLEET_TREND_CACHE_TTL[range_key],
            compute,
        )

    def get_range_key(self):
        key, _window, _step = timeseries.get_range(
            self.request.GET.get("range")
        )
        return key

    def handle_error(self, context, exc):
        if isinstance(exc, cache.ComputationFailed) and isinstance(
            exc.__cause__, config.ConfigurationError
        ):
            # raised inside a shared computation (e.g. building the client)
            exc = exc.__cause__
        if isinstance(exc, config.ConfigurationError):
            # the index shows it too; log each message once, not on every
            # refresh of every partial
            if str(exc) not in _LOGGED_CONFIG_ERRORS:
                _LOGGED_CONFIG_ERRORS.add(str(exc))
                LOG.error("Invalid compute host health setting: %s", exc)
            # the index shows the message once, at the top of the page
            _forget_partial_state(context)
            context["error_message"] = _(
                "Not available: invalid configuration (see the top of the "
                "page)."
            )
            return context
        if isinstance(exc, cache.StillComputing):
            # another request is still computing the shared data: show a
            # loading state, the template retries shortly
            context.pop("announce", None)
            LOG.info("Compute host health data not ready yet: %s", exc)
            context["loading"] = True
            context["error_message"] = _("Still loading telemetry data…")
            return context
        # the screen reader announces the error, not an earlier outcome,
        # and an error is not "loading" (no quick retry)
        context.pop("announce", None)
        _forget_partial_state(context)
        if isinstance(exc, cache.DataUnavailable):
            # grian_ui.cache already logged the failure with its traceback
            LOG.warning("Compute host health data unavailable: %s", exc)
        else:
            # details (backend messages) go to the log, not to the page
            LOG.exception("Error loading compute host health data: %s", exc)
        context["error_message"] = _(
            "Error loading telemetry data. See the Horizon logs for details."
        )
        return context


def _forget_partial_state(context):
    """Drop the optional-data state of a section replaced by an error."""
    for key in list(context):
        if key.endswith(("_unavailable", "_loading")) or key in (
            "loading",
            "partial_notes",
        ):
            del context[key]


def _percent(value):
    """``40%`` or ``12.5%``, or a dash alone when the meter is missing."""
    if value is None:
        return "–"
    return f"{defaultfilters.floatformat(value, '-1')}%"


def _meters(detail):
    """The CPU breakdown as display strings (a new dict: never mutate
    cached data)."""
    return {
        key: _percent(detail.get(key))
        for key in ("user", "kernel", "iowait", "idle")
    }


# The values at the end of a chart: its last two steps (the last point
# can lag behind the time the trend was evaluated at)
LATEST_SLOTS = 2


def _latest_values(series, fetched_at):
    """
    What the end of the fleet chart shows, for its text alternative.

    Returns:
        dict: ``latest`` (stat: value at the end of the chart, or None),
        or, when the chart ends in a gap, ``last_data_age`` (how long ago
        the last value was, or None without data)
    """
    latest = {}
    for stat, points in series.items():
        values = [v for _t, v in points[-LATEST_SLOTS:] if v is not None]
        latest[stat] = values[-1] if values else None
    if any(v is not None for v in latest.values()):
        return {"latest": latest}
    seen = [
        t for points in series.values() for t, v in points if v is not None
    ]
    return {
        "latest": None,
        "last_data_age": (
            timeseries.format_age(fetched_at - max(seen)) if seen else None
        ),
    }


def _trend_summary(values):
    """Latest, lowest and highest value of a trend (for screen readers)."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    return {
        "latest": round(known[-1], 1),
        "min": round(min(known), 1),
        "max": round(max(known), 1),
    }


def _selected_band(request):
    """The band listed in the top hosts table, or None."""
    if request.GET.get("by") != compute_health.TOP_BY_BAND:
        return None
    try:
        return int(request.GET.get("band", ""))
    except ValueError:
        return None


def _with_ages(rows, now):
    for row in rows:
        if row.get("last_seen") is not None:
            row["last_seen_age"] = timeseries.format_age(
                now - row["last_seen"]
            )
    return rows


class ComputeHostHealthIndexView(
    _ComputeHostHealthMixin, generic.TemplateView
):
    template_name = "admin/compute_host_health/index.html"
    page_title = _("Compute Host Health")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["ranges"] = list(timeseries.RANGES)
        context["default_range"] = timeseries.DEFAULT_RANGE
        context["refresh_seconds"] = REFRESH_SECONDS
        context["stale_after"] = compute_health.STALE_AFTER
        context["stale_lookback"] = compute_health.STALE_LOOKBACK
        # for "How this page is calculated"
        context["stale_step"] = compute_health.STALE_STEP
        context["stale_recent_lookback"] = compute_health.STALE_RECENT_LOOKBACK
        context["average_window"] = compute_health.AVERAGE_WINDOW
        context["top_limit"] = compute_health.TOP_LIMIT
        context["range_steps"] = [
            _("%(range)s: every %(step)s") % {"range": key, "step": step}
            for key, (_window, step) in timeseries.RANGES.items()
        ]
        # every invalid setting at once, not one per page load
        errors = []
        try:
            context["thresholds"] = config.get_compute_host_thresholds()
            context["band_labels"] = [
                band["label"]
                for band in compute_health.build_bands(context["thresholds"])
            ]
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        try:
            # read when the datasource client is built: shown here, once
            _check_datasource_settings()
        except config.ConfigurationError as exc:
            errors.append(str(exc))
        context["error_messages"] = errors
        return context


class SummaryView(_ComputeHostHealthMixin, generic.TemplateView):
    """Summary cards and the capped attention list."""

    template_name = "admin/compute_host_health/_summary.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_compute_host_thresholds()
            snapshot = self.get_snapshot()
            summary = compute_health.summarize(snapshot, thresholds)
            _with_ages(summary["attention"], time.time())
            context["summary"] = summary
            context["thresholds"] = thresholds
            context["top_limit"] = compute_health.TOP_LIMIT
            context["stale_lookback"] = compute_health.STALE_LOOKBACK
            context["stale_list_incomplete"] = snapshot.get(
                "stale_list_incomplete", False
            )
            context["stale_recent_lookback"] = (
                compute_health.STALE_RECENT_LOOKBACK
            )
            context["fetched_at"] = snapshot.get("fetched_at")
            context["fetched_ts"] = str(int(snapshot.get("fetched_at") or 0))
            context["data_stale"] = snapshot.get("is_stale_copy", False)
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class DistributionView(_ComputeHostHealthMixin, generic.TemplateView):
    """Number of hosts per CPU band."""

    template_name = "admin/compute_host_health/_distribution.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            thresholds = config.get_compute_host_thresholds()
            snapshot = self.get_snapshot()
            summary = compute_health.summarize(snapshot, thresholds)
            context["data_stale"] = snapshot.get("is_stale_copy", False)
            context["bands"] = summary["bands"]
            context["selected_band"] = _selected_band(self.request)
            context["total"] = summary["banded"]
            context["chart"] = {
                "labels": [b["label"] for b in summary["bands"]],
                "counts": [b["count"] for b in summary["bands"]],
                "levels": [b["level"] for b in summary["bands"]],
                "total": summary["banded"],
            }
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class FleetTrendView(_ComputeHostHealthMixin, generic.TemplateView):
    """Fleet median, 95th percentile and max CPU over time."""

    template_name = "admin/compute_host_health/_fleet_trend.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        range_key = self.get_range_key()
        context["range_key"] = range_key
        try:
            thresholds = config.get_compute_host_thresholds()
            fleet = self.get_fleet_trend(range_key)
            trend, fetched_at = fleet["series"], fleet["fetched_at"]
            # the grid ends at the time the trend was evaluated at, so a
            # cached trend does not grow a false gap at its end
            series = {
                stat: timeseries.gap_filled_series(
                    points, range_key, fetched_at
                )
                for stat, points in trend.items()
            }
            context.update(_latest_values(series, fetched_at))
            context["has_data"] = any(trend.values())
            context["data_stale"] = fleet.get("is_stale_copy", False)
            context["chart"] = {
                "range": range_key,
                "series": series,
                "warning": thresholds["cpu_warning"],
                "critical": thresholds["cpu_critical"],
            }
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class TopHostsView(_ComputeHostHealthMixin, generic.TemplateView):
    """At most TOP_LIMIT hosts: busiest CPU, I/O wait, stale or a band."""

    template_name = "admin/compute_host_health/_top_hosts.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        by = self.request.GET.get("by")
        if by not in compute_health.TOP_BY_CHOICES:
            by = compute_health.TOP_BY_CPU
        try:
            band = int(self.request.GET.get("band", ""))
        except ValueError:
            band = None
        range_key = self.get_range_key()
        context.update(by=by, band=band, range_key=range_key)

        try:
            thresholds = config.get_compute_host_thresholds()
            snapshot = self.get_snapshot()
            context["data_stale"] = snapshot.get("is_stale_copy", False)
            # the stale list may miss hosts: never show it as complete
            context["stale_list_incomplete"] = snapshot.get(
                "stale_list_incomplete", False
            )
            context["stale_recent_lookback"] = (
                compute_health.STALE_RECENT_LOOKBACK
            )
            rows, total = compute_health.select_top(
                snapshot, thresholds, by=by, band=band
            )
            names = [
                r["name"]
                for r in rows
                if r["status"]["key"] != compute_health.STATUS_STALE
            ]
            details, trends = {}, {}
            now = trends_at = time.time()
            if names:
                # the rows come from the snapshot: render them even when
                # their breakdown or trends are not available
                found = self.get_optional(
                    context, "details", lambda: self.get_host_details(names)
                )
                if found:
                    details = found["details"]
                found = self.get_optional(
                    context,
                    "trends",
                    lambda: self.get_host_trends(names, range_key),
                )
                if found:
                    trends, trends_at = found["trends"], found["fetched_at"]
            for row in rows:
                detail = details.get(row["name"])
                if detail:
                    row["detail"] = detail
                    row["meters"] = _meters(detail)
                    if row["last_seen"] is None:
                        row["last_seen"] = detail.get("last_seen")
            _with_ages(rows, now)

            bands = compute_health.build_bands(thresholds)
            context["band_label"] = (
                bands[band]["label"]
                if band is not None and 0 <= band < len(bands)
                else None
            )
            context["rows"] = rows
            context["total"] = total
            context["trends"] = {
                name: timeseries.fill_gaps(points, range_key, trends_at)[1]
                for name, points in trends.items()
            }
            for row in rows:
                row["trend_summary"] = _trend_summary(
                    context["trends"].get(row["name"], [])
                )
            context["thresholds"] = thresholds
            context["top_limit"] = compute_health.TOP_LIMIT
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class HostLookupView(_ComputeHostHealthMixin, generic.TemplateView):
    """Status, CPU breakdown and trend of one host."""

    template_name = "admin/compute_host_health/_host_lookup.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        name = (self.request.GET.get("name") or "").strip()
        range_key = self.get_range_key()
        context.update(
            name=name, range_key=range_key, refresh_seconds=REFRESH_SECONDS
        )
        if not name:
            return context

        try:
            thresholds = config.get_compute_host_thresholds()
            snapshot = self.get_snapshot()
            context["data_stale"] = snapshot.get("is_stale_copy", False)
            host = compute_health.find_host(snapshot, name)
            context["found"] = host is not None
            context["stale_list_incomplete"] = snapshot.get(
                "stale_list_incomplete", False
            )
            if host is None:
                similar = compute_health.names_like(snapshot, name)
                if len(similar) > 1:
                    # several hosts differ from the name in case alone
                    context["ambiguous"] = similar
                    context["announce"] = _(
                        "Several hosts match %(name)s: type the exact name"
                    ) % {"name": name}
                    return context
                context["stale_lookback"] = compute_health.STALE_LOOKBACK
                context["announce"] = _("No compute host named %(name)s") % {
                    "name": name
                }
                return context
            # the reported name, also for a name typed in another case
            name = host["name"]
            context["name"] = name

            now = time.time()
            if host.get("stale"):
                context["status"] = compute_health.stale_status()
                context["announce"] = _("%(name)s: %(status)s") % {
                    "name": name,
                    "status": compute_health.STATUS_LABELS[
                        compute_health.STATUS_STALE
                    ],
                }
                context["last_seen_age"] = timeseries.format_age(
                    now - host["last_seen"]
                )
                return context

            # the status comes from the snapshot; the breakdown, the trend
            # and the fleet median are optional
            detail, trend, trends_at = {}, [], now
            found = self.get_optional(
                context, "details", lambda: self.get_host_details([name])
            )
            if found:
                detail = found["details"].get(name, {})
            found = self.get_optional(
                context,
                "trends",
                lambda: self.get_host_trends([name], range_key),
            )
            if found:
                trend = found["trends"].get(name, [])
                trends_at = found["fetched_at"]
            fleet = self.get_optional(
                context, "fleet", lambda: self.get_fleet_trend(range_key)
            ) or {"series": {}, "fetched_at": now}
            context["status"] = compute_health.evaluate_host(host, thresholds)
            context["announce"] = _("%(name)s: %(status)s") % {
                "name": name,
                "status": compute_health.STATUS_LABELS[
                    context["status"]["key"]
                ],
            }
            context["host"] = host
            context["detail"] = detail
            context["meters"] = _meters(detail)
            if detail.get("last_seen") is not None:
                context["last_seen_age"] = timeseries.format_age(
                    now - detail["last_seen"]
                )
            host_series = timeseries.gap_filled_series(
                trend, range_key, trends_at
            )
            # read to screen readers: the values the chart draws
            context["trend_summary"] = _trend_summary(
                [v for _t, v in host_series]
            )
            context["chart"] = {
                "name": name,
                "range": range_key,
                "host": host_series,
                "fleet_median": timeseries.gap_filled_series(
                    fleet["series"].get("p50", []),
                    range_key,
                    fleet["fetched_at"],
                ),
                "warning": thresholds["cpu_warning"],
                "critical": thresholds["cpu_critical"],
            }
        except Exception as exc:
            self.handle_error(context, exc)
        return context


class HostSuggestionsView(_ComputeHostHealthMixin, generic.TemplateView):
    """
    Host name suggestions for the lookup box (``<option>`` elements).

    Served from the cached snapshot: no extra queries, at most
    SUGGESTION_LIMIT names whatever the number of hosts.
    """

    template_name = "admin/compute_host_health/_host_suggestions.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            context["suggestions"] = compute_health.suggest_hosts(
                self.get_snapshot(),
                config.get_compute_host_thresholds(),
                self.request.GET.get("name"),
            )
        except (cache.DataUnavailable, config.ConfigurationError) as exc:
            # loading, failed or misconfigured (shown by the other
            # sections): the next focus or keystroke fetches again
            LOG.debug("No compute host suggestions: %s", exc)
            context["suggestions"] = []
        except Exception:
            # suggestions are a convenience: never show an error here
            LOG.exception("Error loading compute host suggestions")
            context["suggestions"] = []
        return context
