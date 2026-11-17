#
# SPDX-License-Identifier: Apache-2.0

"""
Compute host health evaluation.

Pure functions shared by the datasources and the Compute Host Health views.
They turn a fleet *snapshot* (one 15 minute average per host) into status,
summary cards, CPU bands and bounded "top N" lists, so that the amount of
data rendered in the browser does not grow with the number of hosts.

Snapshot format (returned by ``DatasourceClient.get_compute_host_snapshot``)::

    {
        "hosts": [  # hosts that reported recently
            {"name": "compute-01", "cpu_avg": 42.0, "iowait_avg": 1.0},
        ],
        "stale": [  # hosts seen in the lookback window but not recently
            {"name": "compute-07", "last_seen": 1700000000.0},
        ],
    }
"""

import math

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from grian_ui import timeseries


#: Hosts without a sample in this window are considered stale.
STALE_AFTER = "10m"
#: How far back to look for hosts that stopped reporting. Hosts silent for
#: longer than this are no longer known to the dashboard.
#: TODO(vkmc): use the Nova hypervisor list (hypervisor hostnames, the names
#: used by the metrics, not service hosts) as the host inventory so dead
#: hosts never drop out (needs agreement on calling the Nova API).
STALE_LOOKBACK = "24h"
#: Resolution of the "last seen" time of stale hosts.
STALE_STEP = "5m"
#: The STALE_LOOKBACK query is the most expensive one, so its result is
#: shared for a few minutes; every snapshot also looks back this far, at a
#: finer step, to catch hosts that stopped reporting since. It covers the
#: oldest list that can be served: STALE_AFTER, plus the list's cache
#: lifetime, plus how long a last good copy is served after failures
#: (grian_ui.cache.LAST_GOOD_TTL), with a margin.
STALE_RECENT_LOOKBACK = "30m"
STALE_RECENT_STEP = "1m"
#: Window used for the status averages.
AVERAGE_WINDOW = "15m"

#: Maximum rows in the attention list and the top hosts table.
TOP_LIMIT = 10

TOP_BY_CPU = "cpu"
TOP_BY_IOWAIT = "iowait"
TOP_BY_STALE = "stale"
TOP_BY_BAND = "band"
TOP_BY_CHOICES = (TOP_BY_CPU, TOP_BY_IOWAIT, TOP_BY_STALE, TOP_BY_BAND)

STATUS_CRITICAL = "critical"
STATUS_WARNING = "warning"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"
STATUS_HEALTHY = "healthy"
STATUS_LABELS = {
    STATUS_CRITICAL: gettext_lazy("Critical"),
    STATUS_WARNING: gettext_lazy("Warning"),
    STATUS_STALE: gettext_lazy("Stale"),
    STATUS_UNKNOWN: gettext_lazy("Unknown"),
    STATUS_HEALTHY: gettext_lazy("Healthy"),
}
SEVERITY = {
    STATUS_CRITICAL: 3,
    STATUS_WARNING: 2,
    STATUS_STALE: 1,
    STATUS_UNKNOWN: 1,
    STATUS_HEALTHY: 0,
}


def quantile(values, q):
    """
    Quantile of ``values`` (``None`` if empty), like PromQL ``quantile()``.

    Linear interpolation between the closest ranks, so the summary cards
    agree with the fleet chart computed by Prometheus.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = q * (len(ordered) - 1)
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def evaluate_host(host, thresholds):
    """
    Evaluate the status of a reporting host.

    Returns:
        dict: ``{"key": ..., "severity": int, "reasons": [str, ...]}``
    """
    cpu = host.get("cpu_avg")
    iowait = host.get("iowait_avg")
    critical, warning = [], []

    if cpu is not None:
        if cpu >= thresholds["cpu_critical"]:
            critical.append(_("CPU %(value)s%%") % {"value": _fmt(cpu)})
        elif cpu >= thresholds["cpu_warning"]:
            warning.append(_("CPU %(value)s%%") % {"value": _fmt(cpu)})
    if iowait is not None:
        if iowait >= thresholds["iowait_critical"]:
            critical.append(
                _("I/O wait %(value)s%%") % {"value": _fmt(iowait)}
            )
        elif iowait >= thresholds["iowait_warning"]:
            warning.append(_("I/O wait %(value)s%%") % {"value": _fmt(iowait)})

    reasons = critical + warning
    if critical:
        key = STATUS_CRITICAL
    elif warning:
        key = STATUS_WARNING
    elif cpu is None:
        # reporting, but nothing that can be assessed
        key = STATUS_UNKNOWN
    else:
        key = STATUS_HEALTHY
    if cpu is None:
        reasons.append(_("no valid CPU data"))
    return {"key": key, "severity": SEVERITY[key], "reasons": reasons}


def stale_status():
    """Status for a host that stopped reporting."""
    return {
        "key": STATUS_STALE,
        "severity": SEVERITY[STATUS_STALE],
        "reasons": [_("no recent data")],
    }


def build_bands(thresholds):
    """
    CPU bands for the distribution chart, aligned with the status thresholds.

    With the default thresholds: 0-25, 25-50, 50-75, 75-90 and >= 90.
    """
    edges = sorted(
        {0, 25, 50, thresholds["cpu_warning"], thresholds["cpu_critical"]}
    )
    edges = [e for e in edges if 0 <= e < 100]
    bands = []
    for i, low in enumerate(edges):
        high = edges[i + 1] if i + 1 < len(edges) else None
        if low >= thresholds["cpu_critical"]:
            level = STATUS_CRITICAL
        elif low >= thresholds["cpu_warning"]:
            level = STATUS_WARNING
        else:
            level = STATUS_HEALTHY
        label = (
            f"{_fmt(low)}–{_fmt(high)}%"
            if high is not None
            else f"≥ {_fmt(low)}%"
        )
        bands.append(
            {"low": low, "high": high, "label": label, "level": level}
        )
    return bands


def band_index(value, bands):
    """Index of the band containing ``value``."""
    for i, band in enumerate(bands):
        if band["high"] is None or value < band["high"]:
            return i
    return len(bands) - 1


def _host_row(host, status):
    return {
        "name": host["name"],
        "status": status,
        "cpu_avg": host.get("cpu_avg"),
        "iowait_avg": host.get("iowait_avg"),
        "last_seen": host.get("last_seen"),
    }


def _attention_sort_key(row):
    # worst first: severity, then the bigger problem, then the hosts that
    # stopped reporting most recently (the most actionable ones; hosts
    # silent for hours are often decommissioned or in maintenance)
    return (
        -row["status"]["severity"],
        # same severity: stale hosts before unknown ones
        row["status"]["key"] == STATUS_UNKNOWN,
        -(row["cpu_avg"] or 0) - (row["iowait_avg"] or 0),
        -(row["last_seen"] or 0),
    )


def summarize(snapshot, thresholds, limit=TOP_LIMIT):
    """
    Build the fleet summary: cards, capped attention list and CPU bands.

    The result size is bounded (``limit`` attention rows, a handful of
    bands) whatever the number of hosts in the snapshot.
    """
    hosts = snapshot.get("hosts", [])
    stale = snapshot.get("stale", [])
    bands = build_bands(thresholds)
    counts = [0] * len(bands)

    attention = []
    critical = warning = unknown = 0
    for host in hosts:
        status = evaluate_host(host, thresholds)
        if status["key"] == STATUS_CRITICAL:
            critical += 1
        elif status["key"] == STATUS_WARNING:
            warning += 1
        elif status["key"] == STATUS_UNKNOWN:
            unknown += 1
        if status["severity"]:
            attention.append(_host_row(host, status))
        if host.get("cpu_avg") is not None:
            counts[band_index(host["cpu_avg"], bands)] += 1
    for host in stale:
        attention.append(_host_row(host, stale_status()))

    attention.sort(key=_attention_sort_key)

    cpu_values = [h["cpu_avg"] for h in hosts if h.get("cpu_avg") is not None]
    iowait_hosts = [h for h in hosts if h.get("iowait_avg") is not None]
    max_iowait = (
        max(iowait_hosts, key=lambda h: h["iowait_avg"])
        if iowait_hosts
        else None
    )
    for band, count in zip(bands, counts):
        band["count"] = count

    pipeline_hosts = _hosts_stopped_together(hosts, stale)
    return {
        "reporting": len(hosts),
        "stale": len(stale),
        "known": len(hosts) + len(stale),
        "critical": critical,
        "warning": warning,
        "unknown": unknown,
        "all_stale": not hosts and bool(stale),
        "pipeline_down": bool(pipeline_hosts),
        "pipeline_hosts": pipeline_hosts,
        "banded": sum(counts),
        "attention_total": len(attention),
        "attention": attention[:limit],
        "cpu_median": quantile(cpu_values, 0.5),
        "cpu_p95": quantile(cpu_values, 0.95),
        "max_iowait": (
            {"name": max_iowait["name"], "value": max_iowait["iowait_avg"]}
            if max_iowait
            else None
        ),
        "bands": bands,
    }


#: Minimum number of hosts, and maximum spread of their "last seen" times,
#: for an "every host went silent" event to be blamed on the pipeline.
PIPELINE_OUTAGE_MIN_HOSTS = 3
PIPELINE_OUTAGE_MAX_SPREAD = 2 * timeseries.duration_seconds(STALE_STEP)


def _hosts_stopped_together(hosts, stale):
    """
    How many hosts stopped together when no host reports, or 0.

    0 unless no host reports and several stopped at about the same time.

    That pattern (at least PIPELINE_OUTAGE_MIN_HOSTS hosts last seen within
    a few minutes of the most recent one) points at the telemetry pipeline
    rather than the hosts. Hosts that went silent long before (e.g.
    decommissioned ones still within STALE_LOOKBACK) do not hide it.
    """
    if hosts or len(stale) < PIPELINE_OUTAGE_MIN_HOSTS:
        return 0
    newest = max(h["last_seen"] for h in stale)
    together = [
        h
        for h in stale
        if newest - h["last_seen"] <= PIPELINE_OUTAGE_MAX_SPREAD
    ]
    return len(together) if len(together) >= PIPELINE_OUTAGE_MIN_HOSTS else 0


def select_top(
    snapshot, thresholds, by=TOP_BY_CPU, band=None, limit=TOP_LIMIT
):
    """
    Select at most ``limit`` hosts for the top hosts table.

    Args:
        by: one of TOP_BY_CHOICES
        band: band index when ``by`` is TOP_BY_BAND

    Returns:
        tuple: (rows, total matching hosts)
    """
    hosts = snapshot.get("hosts", [])
    if by == TOP_BY_STALE:
        stale = sorted(
            snapshot.get("stale", []),
            key=lambda h: h["last_seen"],
            reverse=True,
        )
        rows = [_host_row(h, stale_status()) for h in stale]
        return rows[:limit], len(rows)

    if by == TOP_BY_IOWAIT:
        candidates = [h for h in hosts if h.get("iowait_avg") is not None]
        candidates.sort(key=lambda h: h["iowait_avg"], reverse=True)
    else:
        candidates = [h for h in hosts if h.get("cpu_avg") is not None]
        if by == TOP_BY_BAND:
            bands = build_bands(thresholds)
            if band is None or not 0 <= band < len(bands):
                return [], 0
            candidates = [
                h
                for h in candidates
                if band_index(h["cpu_avg"], bands) == band
            ]
        candidates.sort(key=lambda h: h["cpu_avg"], reverse=True)

    rows = [_host_row(h, evaluate_host(h, thresholds)) for h in candidates]
    return rows[:limit], len(rows)


#: Maximum number of host name suggestions returned for the lookup box.
SUGGESTION_LIMIT = 20


def suggest_hosts(snapshot, thresholds, query, limit=SUGGESTION_LIMIT):
    """
    Host names for the lookup box autocomplete (at most ``limit``).

    Case-insensitive: names starting with ``query`` first, then names
    containing it, each alphabetically. With an empty query, the hosts
    needing attention come first (worst first), then the others
    alphabetically.

    Returns:
        list: ``[{"name": ..., "status": status}, ...]``
    """
    query = (query or "").strip().lower()
    hosts = snapshot.get("hosts", [])
    stale = snapshot.get("stale", [])

    if not query:
        # attention ordering needs every host's status (only on focus)
        rows = [_host_row(h, evaluate_host(h, thresholds)) for h in hosts]
        rows += [_host_row(h, stale_status()) for h in stale]
        attention = sorted(
            (r for r in rows if r["status"]["severity"]),
            key=_attention_sort_key,
        )
        others = sorted(
            (r for r in rows if not r["status"]["severity"]),
            key=lambda r: r["name"],
        )
        return (attention + others)[:limit]

    # while typing: match names first, evaluate status only for the result
    candidates = [(h, False) for h in hosts] + [(h, True) for h in stale]
    prefix, contains = [], []
    for host, is_stale in candidates:
        name = host["name"].lower()
        if name.startswith(query):
            prefix.append((host, is_stale))
        elif query in name:
            contains.append((host, is_stale))
    matches = sorted(prefix, key=lambda m: m[0]["name"]) + sorted(
        contains, key=lambda m: m[0]["name"]
    )
    return [
        _host_row(
            host,
            stale_status() if is_stale else evaluate_host(host, thresholds),
        )
        for host, is_stale in matches[:limit]
    ]


def find_host(snapshot, name):
    """
    Find a host by name: the exact name, else the only host whose name
    differs in case alone (host names are case-insensitive in practice).

    Returns:
        dict or None: host row with status (``name`` is the reported name),
        or None when unknown or ambiguous
    """
    name = (name or "").strip()
    candidates = [(h, False) for h in snapshot.get("hosts", [])] + [
        (h, True) for h in snapshot.get("stale", [])
    ]
    found = [c for c in candidates if c[0]["name"] == name] or [
        c for c in candidates if c[0]["name"].casefold() == name.casefold()
    ]
    if len(found) != 1:
        return None
    host, stale = found[0]
    return dict(host, stale=True) if stale else host


def names_like(snapshot, name):
    """Names of the known hosts equal to ``name`` but for the case."""
    name = (name or "").strip().casefold()
    return sorted(
        h["name"]
        for h in snapshot.get("hosts", []) + snapshot.get("stale", [])
        if h["name"].casefold() == name
    )


def _fmt(value):
    """Format a percentage without a trailing '.0'."""
    if value is None:
        return ""
    rounded = round(float(value), 1)
    return str(int(rounded)) if rounded.is_integer() else str(rounded)
