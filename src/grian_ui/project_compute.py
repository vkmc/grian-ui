#
# SPDX-License-Identifier: Apache-2.0

"""
Project Compute logic: quota, idle quota holders, rightsizing, symptoms.

Pure functions, no request handling. They join two sources:

* the Compute API (Nova), read with the user's token: every server of the
  project with its status and flavor, and the absolute limits (the numbers
  of Project > Overview). Only Nova knows what holds quota: instances in
  ERROR or SHUTOFF report no metrics;
* the polled Ceilometer instance meters (Prometheus through Aetos),
  filtered on the current project: what the running instances use.

Server dicts (see grian_ui.project_compute_inventory)::

    {
        "id",
        "name",
        "status",
        "flavor",
        "vcpus",
        "ram",
        "disk",
        "fault",
        "updated",
    }

Current metrics, per instance UUID (see
grian_ui.datasource.prometheus_project_compute.get_snapshot)::

    {"name", "vcpus", "memory_alloc", "cpu", "cpu_wait", "memory", "swap",
     "disks": {device: {"used", "capacity", "write", "latency"}}}

7-day statistics, per instance UUID (get_rightsizing)::

    {"cpu_avg", "cpu_p95", "memory_avg", "swap", "hours"}

CPU values are in cores, memory in MiB, swap in MiB/s (current) or MiB
(7 days), disk in bytes and latency in ms. Percent thresholds come from
grian_ui.config (get_project_compute_thresholds,
get_project_rightsizing_thresholds).
"""

import math
import time

from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from grian_ui import timeseries


# Quotas ------------------------------------------------------------------

QUOTA_INSTANCES = "instances"
QUOTA_CORES = "cores"
QUOTA_RAM = "ram"
QUOTAS = (QUOTA_INSTANCES, QUOTA_CORES, QUOTA_RAM)
QUOTA_LABELS = {
    QUOTA_INSTANCES: _("Instances"),
    QUOTA_CORES: _("vCPUs"),
    QUOTA_RAM: _("RAM"),
}
#: Nova absolute limit names: (limit, used).
LIMIT_KEYS = {
    QUOTA_INSTANCES: ("maxTotalInstances", "totalInstancesUsed"),
    QUOTA_CORES: ("maxTotalCores", "totalCoresUsed"),
    QUOTA_RAM: ("maxTotalRAMSize", "totalRAMUsed"),
}

#: What a server is doing, for the quota bars.
GROUP_RUNNING = "running"
GROUP_STOPPED = "stopped"
GROUP_ERROR = "error"
GROUP_OFFLOADED = "offloaded"
#: Bar segments, in display order.
SEGMENTS = (GROUP_RUNNING, GROUP_STOPPED, GROUP_ERROR)
SEGMENT_LABELS = {
    GROUP_RUNNING: _("Running"),
    GROUP_STOPPED: _("Shut off"),
    GROUP_ERROR: _("Error"),
}
#: Nova statuses of servers that hold quota without running.
STOPPED_STATUSES = frozenset({"SHUTOFF", "SUSPENDED", "PAUSED", "SHELVED"})
#: Shelved and offloaded: nothing on a host, but by default Nova still counts
#: its flavor against the vCPU and RAM quotas (see holds()).
OFFLOADED_STATUS = "SHELVED_OFFLOADED"

#: Rows listed under "Holding quota without running".
IDLE_LIMIT = 10

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"

# Forecast ----------------------------------------------------------------

#: The forecast extends the trend of this many days (fewer if the metrics
#: are not kept that long, see config.get_metrics_retention_days).
FORECAST_LOOKBACK_DAYS = 30
#: History needed for a forecast or a final rightsizing assessment: 7 days,
#: less a few hours of missing polls.
MIN_HISTORY_DAYS = 7
MIN_HISTORY_HOURS = MIN_HISTORY_DAYS * 24 - 8
#: A forecast is red up to this many days, yellow up to the next.
FORECAST_CRITICAL_DAYS = 14
FORECAST_WARNING_DAYS = 30
#: Beyond this the forecast just says "more than a year".
FORECAST_MAX_DAYS = 365
#: Growth up to this (units per day) is "not growing": a vCPU or an
#: instance more per 1000 days, or 1 MiB of RAM per day.
_FLAT_PER_DAY = {QUOTA_INSTANCES: 0.001, QUOTA_CORES: 0.001, QUOTA_RAM: 1}

# Trend -------------------------------------------------------------------

#: Chart ranges: key -> (range, step), 72 to 96 points.
TREND_RANGES = {
    "6h": ("6h", "5m"),
    "24h": ("24h", "15m"),
    "7d": ("7d", "2h"),
    "30d": ("30d", "8h"),
}
DEFAULT_TREND_RANGE = "24h"
#: Trend resources (the quota they belong to).
TREND_RESOURCES = QUOTAS
DEFAULT_TREND_RESOURCE = QUOTA_CORES
#: Window of current values and of their rate(). Holds at least two polls
#: with the Ceilometer default interval (300 s).
CURRENT_WINDOW = "10m"
CURRENT_WINDOW_TEXT = _("10 minutes")

# Rightsizing -------------------------------------------------------------

UNDERSIZED = "under"
OVERSIZED = "over"
RIGHT_SIZED = "ok"
ASSESSMENTS = (UNDERSIZED, OVERSIZED, RIGHT_SIZED)
ASSESSMENT_LABELS = {
    UNDERSIZED: _("Undersized"),
    OVERSIZED: _("Oversized"),
    RIGHT_SIZED: _("Right-sized"),
}
#: Rightsizing looks at this window, whatever the chart range.
RIGHTSIZING_WINDOW = "7d"
#: Rows of the rightsizing table and of "Instances needing attention".
TABLE_LIMIT = 25
#: Step of the 7-day CPU sparklines (84 points).
SPARK_STEP = "2h"
#: Step of the instance detail charts (168 points).
DETAIL_STEP = "1h"

# Symptoms ----------------------------------------------------------------

SEVERITY_CRITICAL = 3
SEVERITY_WARNING = 2
ISSUE_ERROR = "error"
ISSUE_CPU_WAIT = "cpu_wait"
ISSUE_SWAP = "swap"
ISSUE_DISK = "disk_latency"
#: The first issue of a row in this order gives its advice.
_ADVICE_ORDER = (ISSUE_ERROR, ISSUE_CPU_WAIT, ISSUE_SWAP, ISSUE_DISK)

_MIB = 1024 * 1024


# Formatting --------------------------------------------------------------

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def _number(value, digits):
    """Round, without trailing zeros: 3.50 -> "3.5", 4.0 -> "4"."""
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_bytes(value):
    """``1.5 GiB``, ``512 MiB``; a dash for None."""
    if value is None:
        return "–"
    i = 0
    while abs(value) >= 1024 and i < len(_BYTE_UNITS) - 1:
        value /= 1024
        i += 1
    if i == 0:
        return f"{int(round(value))} B"
    digits = 0 if abs(value) >= 100 else 1 if abs(value) >= 10 else 2
    return f"{_number(value, digits)} {_BYTE_UNITS[i]}"


def format_mib(value):
    """Memory in MiB as ``40 GiB``, ``512 MiB``; a dash for None."""
    return "–" if value is None else format_bytes(value * _MIB)


def format_quota(quota, value):
    """A quota amount: ``3``, ``6``, ``40 GiB``; a dash for None."""
    if value is None:
        return "–"
    if quota == QUOTA_RAM:
        return format_mib(value)
    return _number(value, 1 if value % 1 else 0)


def format_cores(value):
    """Cores in use: ``3.5``, ``0.04``; a dash for None."""
    if value is None:
        return "–"
    return _number(value, 2 if value < 10 else 1)


def format_duration(seconds):
    """A duration in whole units: ``45 minutes``, ``7 hours``, ``9 days``."""
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        n = max(1, seconds // 60)
        return ngettext("%(n)s minute", "%(n)s minutes", n) % {"n": n}
    if seconds < 2 * 86400:
        n = seconds // 3600
        return ngettext("%(n)s hour", "%(n)s hours", n) % {"n": n}
    n = seconds // 86400
    return ngettext("%(n)s day", "%(n)s days", n) % {"n": n}


def percent(part, whole):
    """``part`` as a percentage of ``whole``, or None without a whole."""
    if part is None or not whole:
        return None
    return 100.0 * part / whole


def level(value, warning, critical):
    """LEVEL_OK, LEVEL_WARNING (>= warning) or LEVEL_CRITICAL."""
    if value is None:
        return LEVEL_OK
    if value >= critical:
        return LEVEL_CRITICAL
    if value >= warning:
        return LEVEL_WARNING
    return LEVEL_OK


# Servers -----------------------------------------------------------------


def server_group(status):
    """GROUP_* of a Nova status (transitional states count as running)."""
    status = (status or "").upper()
    if status == "ERROR":
        return GROUP_ERROR
    if status in STOPPED_STATUSES:
        return GROUP_STOPPED
    if status == OFFLOADED_STATUS:
        return GROUP_OFFLOADED
    return GROUP_RUNNING


def holds(server, quota):
    """
    How much of ``quota`` a server holds (0 if its flavor is unknown).

    Counted as Nova does by default ([quota]count_usage_from_placement =
    False): every instance that isn't deleted holds its flavor, shelved and
    offloaded ones included. A cloud counting usage from placement leaves
    offloaded instances out of the vCPU and RAM counts; the bars then add
    up to more than Nova's count (see the mismatch note).
    """
    if quota == QUOTA_INSTANCES:
        return 1
    field = "vcpus" if quota == QUOTA_CORES else "ram"
    return server.get(field) or 0


def _limit(limits, quota):
    """(limit, used) from the Nova limits; limit None when unlimited."""
    limit_key, used_key = LIMIT_KEYS[quota]
    limit = limits.get(limit_key)
    used = limits.get(used_key)
    if limit is None or limit < 0 or math.isinf(limit):
        limit = None
    return limit, used


def quota_rows(servers, limits, thresholds):
    """
    One row per quota: Nova's count and limit, split by what holds it.

    ``used`` is what Nova counts (as in Project > Overview); the segments
    add up the server list (``listed`` is their sum), so instances being
    built or deleted, or a cloud counting usage from placement (offloaded
    instances then hold no vCPUs or RAM), can make them differ.

    Returns:
        list: ``{"key", "label", "limit", "used", "percent", "level",
        "segments": [{"group", "label", "value", "width"}], "listed"}``
    """
    rows = []
    for quota in QUOTAS:
        limit, used = _limit(limits, quota)
        totals = dict.fromkeys(SEGMENTS, 0)
        for server in servers:
            group = server_group(server.get("status"))
            if group == GROUP_OFFLOADED:
                group = GROUP_STOPPED
            totals[group] += holds(server, quota)
        listed = sum(totals.values())
        if used is None:
            used = listed
        pct = percent(used, limit)
        scale = max(limit or 0, used, listed) or 1
        rows.append(
            {
                "key": quota,
                "label": QUOTA_LABELS[quota],
                "limit": limit,
                "used": used,
                "percent": pct,
                "level": level(
                    pct,
                    thresholds["quota_warning"],
                    thresholds["quota_critical"],
                ),
                "segments": [
                    {
                        "group": group,
                        "label": SEGMENT_LABELS[group],
                        "value": totals[group],
                        "text": format_quota(quota, totals[group]),
                        "width": 100.0 * totals[group] / scale,
                    }
                    for group in SEGMENTS
                    if totals[group] > 0
                ],
                "listed": listed,
                "used_text": format_quota(quota, used),
                "limit_text": format_quota(quota, limit),
            }
        )
    return rows


def highest_quota(rows):
    """The row with the highest percentage used, or None (all unlimited)."""
    limited = [r for r in rows if r["percent"] is not None]
    if not limited:
        return None
    return max(limited, key=lambda r: r["percent"])


def idle_holders(servers, ram_quota, now=None, limit=IDLE_LIMIT):
    """
    Servers holding quota without running, largest RAM first.

    Shelved-offloaded servers are included: Nova counts their flavor by
    default (see holds()).

    Args:
        ram_quota: the RAM limit in MiB, or None when unlimited

    Returns:
        tuple: (at most ``limit`` rows, number of such servers, totals
        ``{"instances", "vcpus", "ram"}`` of all of them). Rows are
        ``{"id", "name", "status", "flavor", "vcpus", "ram", "fault",
        "since", "ram_percent"}``; ``since`` is the time since Nova last
        updated the server (usually its last state change)
    """
    now = time.time() if now is None else now
    idle = [
        s
        for s in servers
        if server_group(s.get("status"))
        in (GROUP_STOPPED, GROUP_ERROR, GROUP_OFFLOADED)
    ]
    idle.sort(key=lambda s: (-(s.get("ram") or 0), s.get("name") or ""))
    totals = {
        "instances": len(idle),
        "vcpus": sum(s.get("vcpus") or 0 for s in idle),
        "ram": sum(s.get("ram") or 0 for s in idle),
    }
    rows = []
    for server in idle[:limit]:
        updated = server.get("updated")
        rows.append(
            {
                "id": server["id"],
                "name": server.get("name") or server["id"],
                "status": server.get("status"),
                "flavor": server.get("flavor"),
                "vcpus": server.get("vcpus"),
                "ram": server.get("ram"),
                "ram_text": format_mib(server.get("ram")),
                "fault": server.get("fault"),
                "since": format_duration(now - updated) if updated else "",
                "ram_percent": percent(server.get("ram"), ram_quota),
            }
        )
    return rows, len(idle), totals


def server_counts(servers):
    """``{"total", "running", "not_running"}`` (running: not idle)."""
    running = sum(
        1 for s in servers if server_group(s.get("status")) == GROUP_RUNNING
    )
    return {
        "total": len(servers),
        "running": running,
        "not_running": len(servers) - running,
    }


def ram_limit(limits):
    """The RAM quota in MiB, or None when unlimited."""
    return _limit(limits, QUOTA_RAM)[0]


# Forecast ----------------------------------------------------------------


def forecast_lookback_days(retention_days):
    """Days the forecast looks back: FORECAST_LOOKBACK_DAYS or less."""
    return max(1, min(FORECAST_LOOKBACK_DAYS, retention_days))


def forecast(row, slope_per_second, history_hours, retention_days):
    """
    When a quota will be full at the current growth rate.

    Args:
        row: a quota_rows() row (Nova's count and limit)
        slope_per_second: growth of what running instances hold, from
            ``deriv()`` (units per second), or None
        history_hours: hours of metric history in the lookback window

    Returns:
        dict: ``{"state", "days", "level"}``; ``state`` is one of
        ``unlimited``, ``full``, ``short_retention`` (the metrics are not
        kept long enough), ``no_history``, ``no_data``, ``flat`` (not
        growing), ``far`` (more than FORECAST_MAX_DAYS) or ``days``
    """
    result = {"state": "no_data", "days": None, "level": LEVEL_OK}
    limit, used = row["limit"], row["used"]
    if limit is None:
        result["state"] = "unlimited"
        return result
    if used is not None and used >= limit:
        return dict(result, state="full", level=LEVEL_CRITICAL)
    if retention_days < MIN_HISTORY_DAYS:
        result["state"] = "short_retention"
        return result
    if history_hours is None or slope_per_second is None:
        return result
    if history_hours < MIN_HISTORY_HOURS:
        result["state"] = "no_history"
        return result
    per_day = slope_per_second * 86400
    if per_day <= _FLAT_PER_DAY[row["key"]]:
        result["state"] = "flat"
        return result
    days = (limit - (used or 0)) / per_day
    if days > FORECAST_MAX_DAYS:
        result["state"] = "far"
        return result
    days = max(1, int(math.ceil(days)))
    if days <= FORECAST_CRITICAL_DAYS:
        days_level = LEVEL_CRITICAL
    elif days <= FORECAST_WARNING_DAYS:
        days_level = LEVEL_WARNING
    else:
        days_level = LEVEL_OK
    result.update(state="days", days=days, level=days_level)
    return result


# Trend -------------------------------------------------------------------


def trend_range(key):
    """``(key, range, step)``, falling back to the default range."""
    if key not in TREND_RANGES:
        key = DEFAULT_TREND_RANGE
    window, step = TREND_RANGES[key]
    return key, window, step


def range_days(key):
    """Length of a chart range in days (fractional)."""
    return timeseries.duration_seconds(TREND_RANGES[key][0]) / 86400


def available_ranges(retention_days):
    """
    The chart ranges, with ``enabled`` False when longer than retention.

    Returns:
        list: ``[{"key", "enabled"}, ...]``
    """
    return [
        {"key": key, "enabled": range_days(key) <= retention_days}
        for key in TREND_RANGES
    ]


def grid(window, step, now=None):
    """
    Timestamps a subquery ``expr[window:step]`` evaluated at ``now`` returns.

    Same rule as grian_ui.timeseries.range_grid, for any window and step.
    """
    step_s = timeseries.duration_seconds(step)
    count = timeseries.duration_seconds(window) // step_s
    end = int((now if now is not None else time.time()) // step_s * step_s)
    return [end - (count - 1 - i) * step_s for i in range(count)]


def fill(points, window, step, now=None):
    """
    Align ``[[timestamp, value], ...]`` to the subquery grid.

    Returns:
        tuple: (timestamps, values), missing steps as None
    """
    timestamps = grid(window, step, now)
    step_s = timeseries.duration_seconds(step)
    by_slot = {}
    for timestamp, value in points:
        by_slot[int(round(timestamp / step_s)) * step_s] = value
    return timestamps, [by_slot.get(t) for t in timestamps]


def last_value(values):
    """The last value that is not None, or None."""
    for value in reversed(values):
        if value is not None:
            return value
    return None


def series_summary(values):
    """``{"latest", "min", "max"}`` of the values, or None if empty."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return {"latest": present[-1], "min": min(present), "max": max(present)}


# Rightsizing -------------------------------------------------------------


def _ratio(value, whole):
    if value is None or not whole:
        return None
    return value / whole


def assess(stats, vcpus, ram, thresholds):
    """
    Assess one running instance against its flavor.

    Args:
        stats: 7-day statistics (see the module docstring), or None
        vcpus, ram: the flavor (vCPUs, MiB)
        thresholds: config.get_project_rightsizing_thresholds()

    Returns:
        dict: ``assessment`` (UNDERSIZED, OVERSIZED, RIGHT_SIZED, or None
        without data), the ratios (``cpu``, ``cpu_p95``, ``memory``, 0-1),
        ``swap`` (bool), ``notes`` (short reasons) and ``hint`` (advice)
    """
    stats = stats or {}
    cpu = _ratio(stats.get("cpu_avg"), vcpus)
    cpu_p95 = _ratio(stats.get("cpu_p95"), vcpus)
    memory = _ratio(stats.get("memory_avg"), ram)
    swap = (stats.get("swap") or 0) > 0
    result = {
        "assessment": None,
        "cpu": cpu,
        "cpu_p95": cpu_p95,
        "memory": memory,
        "swap": swap,
        "notes": [],
        "hint": "",
    }
    if cpu is None or memory is None:
        return result
    peak = cpu_p95 if cpu_p95 is not None else cpu
    cpu_high = cpu * 100 >= thresholds["cpu_high"]
    memory_full = memory * 100 >= thresholds["memory_high"]
    cpu_low = peak * 100 < thresholds["cpu_low"]
    memory_low = memory * 100 < thresholds["memory_low"]
    notes = []
    if cpu_high:
        notes.append(str(_("CPU busy")))
    if swap:
        notes.append(str(_("swapping")))
    elif memory_full:
        notes.append(str(_("memory full")))
    if cpu_high or memory_full or swap:
        result["assessment"] = UNDERSIZED
        if swap or memory_full:
            hint = _("Resize to a flavor with more RAM.")
        else:
            hint = _(
                "CPU averages %(cpu)s%% of its %(vcpus)s vCPUs. Resize to "
                "a flavor with more vCPUs, or spread the load over more "
                "instances."
            ) % {"cpu": _number(cpu * 100, 0), "vcpus": vcpus}
    elif cpu_low and memory_low:
        result["assessment"] = OVERSIZED
        values = {
            "cpu": _number(peak * 100, 0),
            "memory": _number(memory * 100, 0),
            "free": (vcpus or 1) - 1,
        }
        if vcpus and vcpus > 1:
            hint = (
                _(
                    "CPU peaks at %(cpu)s%% and memory stays at "
                    "%(memory)s%%. A smaller flavor would free quota (up "
                    "to %(free)s vCPUs)."
                )
                % values
            )
        else:
            hint = (
                _(
                    "CPU peaks at %(cpu)s%% and memory stays at "
                    "%(memory)s%%. A smaller flavor would free quota."
                )
                % values
            )
    else:
        result["assessment"] = RIGHT_SIZED
        if memory_low and not cpu_low:
            notes.append(str(_("memory mostly unused")))
            hint = _(
                "CPU fits the flavor. Memory is mostly unused: a flavor "
                "with the same vCPUs and less RAM would free RAM quota."
            )
        else:
            hint = _("The flavor fits the workload.")
    result["notes"] = notes
    result["hint"] = str(hint)
    return result


def _bar_level(ratio, high, low):
    """CSS level of a used ÷ allocated bar: busy, low or normal."""
    if ratio is None:
        return ""
    if ratio * 100 >= high:
        return "busy"
    if ratio * 100 < low:
        return "low"
    return ""


def running_instances(servers, snapshot):
    """
    The running instances to assess: from Nova, or from the metrics.

    Args:
        servers: Nova servers, or None when the Compute API failed
        snapshot: current metrics per instance UUID (may be empty)

    Returns:
        list: ``{"id", "name", "flavor", "vcpus", "ram", "disk"}``
    """
    if servers is not None:
        rows = []
        for server in servers:
            if (server.get("status") or "").upper() != "ACTIVE":
                continue
            metrics = snapshot.get(server["id"]) or {}
            rows.append(
                {
                    "id": server["id"],
                    "name": server.get("name") or server["id"],
                    "flavor": server.get("flavor"),
                    "vcpus": server.get("vcpus") or metrics.get("vcpus"),
                    "ram": server.get("ram") or metrics.get("memory_alloc"),
                    "disk": server.get("disk"),
                }
            )
        return rows
    return [
        {
            "id": instance_id,
            "name": metrics.get("name") or instance_id,
            "flavor": None,
            "vcpus": metrics.get("vcpus"),
            "ram": metrics.get("memory_alloc"),
            "disk": None,
        }
        for instance_id, metrics in snapshot.items()
    ]


def rightsizing_rows(instances, stats, snapshot, thresholds):
    """
    Assess every running instance; undersized first, then oversized.

    Within a group, rows are sorted by how far they are from their flavor:
    the busiest first for undersized and right-sized instances, the idlest
    first for oversized ones. Instances without statistics come last.

    Returns:
        list: every row (callers keep at most TABLE_LIMIT), with the
        assessment, the bar levels, the current disk use and ``hours``
    """
    rows = []
    for instance in instances:
        instance_stats = stats.get(instance["id"]) or {}
        current = snapshot.get(instance["id"]) or {}
        vcpus, ram = instance["vcpus"], instance["ram"]
        result = assess(instance_stats, vcpus, ram, thresholds)
        hours = instance_stats.get("hours")
        _device, disk_used, disk_capacity = root_disk(current)
        row = dict(instance)
        row.update(result)
        row.update(
            cpu_used=instance_stats.get("cpu_avg"),
            memory_used=instance_stats.get("memory_avg"),
            swap_mib=instance_stats.get("swap"),
            hours=int(hours) if hours is not None else None,
            provisional=hours is None or hours < MIN_HISTORY_HOURS,
            cpu_level=_bar_level(
                result["cpu"], thresholds["cpu_high"], thresholds["cpu_low"]
            ),
            memory_level=_bar_level(
                result["memory"],
                thresholds["memory_high"],
                thresholds["memory_low"],
            ),
            disk_ratio=_ratio(disk_used, disk_capacity),
            disk_capacity=disk_capacity,
        )
        row["ram_text"] = format_mib(ram)
        row["memory_used_text"] = format_mib(row["memory_used"])
        row["cpu_used_text"] = format_cores(row["cpu_used"])
        row["disk_capacity_text"] = format_bytes(disk_capacity)
        rows.append(row)

    order = {UNDERSIZED: 0, OVERSIZED: 1, RIGHT_SIZED: 2, None: 3}

    def key(row):
        cpu = row["cpu"] or 0
        # oversized: the idlest first; the others: the busiest first
        far = cpu if row["assessment"] == OVERSIZED else -cpu
        return (order[row["assessment"]], far, row["name"])

    rows.sort(key=key)
    return rows


def rightsizing_summary(rows):
    """
    Totals of the assessed rows: what is used of what is held.

    Returns:
        dict: ``cpu_used``, ``vcpus``, ``memory_used``, ``ram`` (of the
        rows with data), their percentages, ``counts`` per assessment,
        ``assessed``, ``no_data``, ``provisional`` (any assessed row) and
        the shortest ``hours`` of data
    """
    assessed = [r for r in rows if r["assessment"] is not None]
    cpu_used = sum(r["cpu_used"] or 0 for r in assessed)
    vcpus = sum(r["vcpus"] or 0 for r in assessed)
    memory_used = sum(r["memory_used"] or 0 for r in assessed)
    ram = sum(r["ram"] or 0 for r in assessed)
    counts = dict.fromkeys(ASSESSMENTS, 0)
    for row in assessed:
        counts[row["assessment"]] += 1
    hours = [r["hours"] for r in assessed if r["hours"] is not None]
    return {
        "cpu_used": cpu_used,
        "vcpus": vcpus,
        "cpu_percent": percent(cpu_used, vcpus),
        "memory_used": memory_used,
        "ram": ram,
        "memory_percent": percent(memory_used, ram),
        "counts": counts,
        "assessed": len(assessed),
        "no_data": len(rows) - len(assessed),
        "provisional": any(r["provisional"] for r in assessed),
        "hours": min(hours) if hours else None,
        "cpu_used_text": format_cores(cpu_used),
        "memory_used_text": format_mib(memory_used),
        "ram_text": format_mib(ram),
    }


# Disks -------------------------------------------------------------------


def root_disk(metrics):
    """
    ``(device, used, capacity)`` of the instance's first disk, or Nones.

    The root disk is the first device (``vda`` before ``vdb``); volumes
    attached later have later names.
    """
    disks = {
        device: disk
        for device, disk in ((metrics or {}).get("disks") or {}).items()
        if disk.get("capacity")
    }
    if not disks:
        return None, None, None
    device = min(disks)
    return device, disks[device].get("used"), disks[device]["capacity"]


def write_latency(metrics, floor_mib):
    """
    The worst write latency (ms) of the disks writing at least
    ``floor_mib`` MiB/s, or None: a handful of requests gives meaningless
    averages.
    """
    worst = None
    for disk in ((metrics or {}).get("disks") or {}).values():
        write, latency = disk.get("write"), disk.get("latency")
        if write is None or latency is None or write < floor_mib * _MIB:
            continue
        worst = latency if worst is None else max(worst, latency)
    return worst


# Symptoms ----------------------------------------------------------------


def _issue(kind, severity, text):
    return {"kind": kind, "severity": severity, "text": str(text)}


def symptoms(server, metrics, thresholds):
    """
    The issues of one instance: Nova state, then current metrics.

    Args:
        server: Nova server (None when only metrics are known)
        metrics: current metrics of the instance, or None
        thresholds: config.get_project_compute_thresholds()

    Returns:
        list: ``{"kind", "severity", "text"}``, worst first
    """
    issues = []
    status = ((server or {}).get("status") or "").upper()
    if status == "ERROR":
        issues.append(_issue(ISSUE_ERROR, SEVERITY_CRITICAL, _("ERROR state")))
    if server is not None and status != "ACTIVE":
        # only running instances report metrics
        return issues
    if not metrics:
        return issues
    vcpus = metrics.get("vcpus") or (server or {}).get("vcpus")
    wait = percent(metrics.get("cpu_wait"), vcpus)
    if wait is not None and wait >= thresholds["cpu_wait_warning"]:
        severity = (
            SEVERITY_CRITICAL
            if wait >= thresholds["cpu_wait_critical"]
            else SEVERITY_WARNING
        )
        text = _("CPU wait %(n)s%%") % {"n": _number(wait, 0)}
        issues.append(_issue(ISSUE_CPU_WAIT, severity, text))
    swap = metrics.get("swap")
    if swap is not None and swap > thresholds["swap_warning"]:
        severity = (
            SEVERITY_CRITICAL
            if swap >= thresholds["swap_critical"]
            else SEVERITY_WARNING
        )
        text = _("swap %(n)s MiB/s") % {"n": _number(swap, 2)}
        issues.append(_issue(ISSUE_SWAP, severity, text))
    latency = write_latency(metrics, thresholds["disk_write_floor"])
    if latency is not None and latency >= thresholds["disk_latency_warning"]:
        severity = (
            SEVERITY_CRITICAL
            if latency >= thresholds["disk_latency_critical"]
            else SEVERITY_WARNING
        )
        text = _("disk latency %(n)s ms") % {"n": _number(latency, 0)}
        issues.append(_issue(ISSUE_DISK, severity, text))
    issues.sort(key=lambda i: -i["severity"])
    return issues


def advice_kind(issues):
    """The issue kind whose advice is shown for a row, or None."""
    kinds = {i["kind"] for i in issues}
    for kind in _ADVICE_ORDER:
        if kind in kinds:
            return kind
    return None


def attention_rows(servers, snapshot, thresholds, limit=TABLE_LIMIT):
    """
    Instances needing attention, worst first.

    Args:
        servers: Nova servers, or None when the Compute API failed (then
            only the instances reporting metrics are checked)
        snapshot: current metrics per instance UUID, or None when the
            metrics failed (then only the Nova state is checked)

    Returns:
        tuple: (at most ``limit`` rows ``{"id", "name", "status",
        "fault", "severity", "issues", "advice"}``, number of instances
        with issues)
    """
    snapshot = snapshot or {}
    if servers is None:
        candidates = [
            ({"id": i, "name": m.get("name") or i, "status": None}, None)
            for i, m in snapshot.items()
        ]
    else:
        candidates = [(s, s) for s in servers]
    rows = []
    for row, server in candidates:
        issues = symptoms(server, snapshot.get(row["id"]), thresholds)
        if not issues:
            continue
        rows.append(
            {
                "id": row["id"],
                "name": row.get("name") or row["id"],
                "status": row.get("status"),
                "fault": row.get("fault"),
                "severity": max(i["severity"] for i in issues),
                "issues": issues,
                "advice": advice_kind(issues),
            }
        )
    rows.sort(key=lambda r: (-r["severity"], r["name"], r["id"]))
    return rows[:limit], len(rows)
