#
# SPDX-License-Identifier: Apache-2.0

"""
Top consumers evaluation.

Pure functions shared by the datasources and the Top Consumers views. They
turn the per-project snapshot (one row per project with running instances)
and the bounded per-instance lists into summary totals, "top N" table rows
with their share of the total, and stacked chart series, so the amount of
data rendered in the browser does not grow with the cloud.

Snapshot format (returned by ``DatasourceClient.get_top_consumers_projects``)::

    {
        "projects": [  # projects with instances that reported recently
            {
                "id": "7bb2...",
                "instances": 2,  # running instances reporting
                "cpu": 3.55,  # cores in use
                "vcpus": 5.0,  # vCPUs of those instances
                "memory": 87.3,  # MiB in use
                "memory_alloc": 512,  # MiB of flavor RAM
                "disk_read": 0.0,  # bytes/s
                "disk_write": 1e6,  # bytes/s
                "net_in": 10.2,  # bytes/s
                "net_out": 3.1,  # bytes/s
            },
        ],
    }

Any value but ``id`` may be None when its meter has no recent sample.

Instance rows (``DatasourceClient.get_top_consumers_instances``)::

    {
        "id": uuid,
        "name": str or None,
        "project_id": str,
        "value": float,
        "vcpus": float or None,
        "memory_alloc": float or None,
    }
"""

import decimal
import math

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from grian_ui import timeseries


RESOURCE_CPU = "cpu"
RESOURCE_MEMORY = "memory"
RESOURCE_DISK = "disk"
RESOURCE_NETWORK = "network"
RESOURCES = (RESOURCE_CPU, RESOURCE_MEMORY, RESOURCE_DISK, RESOURCE_NETWORK)
RESOURCE_LABELS = {
    RESOURCE_CPU: gettext_lazy("CPU"),
    RESOURCE_MEMORY: gettext_lazy("Memory"),
    RESOURCE_DISK: gettext_lazy("Disk I/O"),
    RESOURCE_NETWORK: gettext_lazy("Network"),
}
#: Unit of each resource's values (used by the chart tick formatter).
RESOURCE_UNITS = {
    RESOURCE_CPU: "cores",
    RESOURCE_MEMORY: "mib",
    RESOURCE_DISK: "bytes_per_second",
    RESOURCE_NETWORK: "bytes_per_second",
}
#: Snapshot fields summed into each resource.
RESOURCE_FIELDS = {
    RESOURCE_CPU: ("cpu",),
    RESOURCE_MEMORY: ("memory",),
    RESOURCE_DISK: ("disk_read", "disk_write"),
    RESOURCE_NETWORK: ("net_in", "net_out"),
}
#: Allocation field compared with the use, for the resources that have one.
ALLOCATION_FIELDS = {
    RESOURCE_CPU: "vcpus",
    RESOURCE_MEMORY: "memory_alloc",
}
SNAPSHOT_FIELDS = (
    "instances",
    "cpu",
    "vcpus",
    "memory",
    "memory_alloc",
    "disk_read",
    "disk_write",
    "net_in",
    "net_out",
)

BY_PROJECT = "project"
BY_INSTANCE = "instance"
BY_CHOICES = (BY_PROJECT, BY_INSTANCE)

#: Window of the current values: rate() of the counters and the last
#: sample of the gauges. Must hold at least two polls of every meter
#: (Ceilometer polls every 300-600 s by default), and matches the staleness
#: window used elsewhere, so instances that stopped running drop out.
CURRENT_WINDOW = "10m"
#: Ranges of the over-time chart and the table sparklines: the shared
#: trend ranges (grian_ui.timeseries.RANGES, 60-170 points each).
TREND_RANGES = tuple(timeseries.RANGES)
DEFAULT_TREND_RANGE = "24h"

#: Rows in the table.
TOP_LIMIT = 10
#: Series in the stacked chart (plus "Other").
CHART_SERIES = 5
#: Idle project names listed in the table footer.
IDLE_LIMIT = 10
#: Key of the "Other" series and of the total in trend results.
OTHER_KEY = "_other"
TOTAL_KEY = "_total"

LEVEL_BUSY = "busy"
LEVEL_IDLE = "idle"


def resource_value(row, resource):
    """Value of ``resource`` in a snapshot row (None when not reported)."""
    values = [row.get(f) for f in RESOURCE_FIELDS[resource]]
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def totals(snapshot):
    """
    Cloud-wide totals of the snapshot.

    Returns:
        dict: each SNAPSHOT_FIELDS key and each resource, summed over the
        projects (None when no project reports it), plus ``projects``
    """
    projects = snapshot.get("projects", [])
    result = {"projects": len(projects)}
    for field in SNAPSHOT_FIELDS:
        known = [p[field] for p in projects if p.get(field) is not None]
        result[field] = sum(known) if known else None
    for resource in RESOURCES:
        known = [
            v
            for v in (resource_value(p, resource) for p in projects)
            if v is not None
        ]
        result[resource] = sum(known) if known else None
    return result


def ratio_percent(used, allocated):
    """``used`` as a percentage of ``allocated``, or None."""
    if used is None or not allocated:
        return None
    return 100.0 * used / allocated


def percent_shown(value):
    """
    ``value`` rounded as the page shows it (half up, like Django's
    ``floatformat:0``), or None.
    """
    if value is None:
        return None
    return int(
        decimal.Decimal(repr(value)).quantize(
            decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP
        )
    )


def usage_level(ratio, thresholds):
    """
    ``busy``, ``idle`` or ``""`` for a used ÷ allocated percentage.

    Decided on the value as shown (whole percent), so a ratio shown as
    "80%" is busy at an 80% threshold and one shown as "10%" is not idle
    at a 10% threshold.
    """
    shown = percent_shown(ratio)
    if shown is None:
        return ""
    if shown >= thresholds["usage_busy"]:
        return LEVEL_BUSY
    if shown < thresholds["usage_idle"]:
        return LEVEL_IDLE
    return ""


def trend_window(range_key):
    """
    ``(range, step, window)`` of a trend range.

    ``window`` is the rate() window (and gauge lookback) at each step: the
    step, but never shorter than CURRENT_WINDOW, so every point holds at
    least two polls of each meter.
    """
    _key, window, step = timeseries.get_range(range_key)
    rate_window = max(step, CURRENT_WINDOW, key=timeseries.duration_seconds)
    return window, step, rate_window


def _share(value, total):
    if not value or not total:
        return 0.0
    return 100.0 * value / total


def _decorate(row, resource, value, allocated, total, thresholds):
    """Add the value, share and used ÷ allocated fields to ``row``."""
    ratio = ratio_percent(value, allocated)
    row.update(
        value=value,
        share=_share(value, total),
        allocated=allocated,
        ratio=ratio,
        ratio_shown=percent_shown(ratio),
        level=usage_level(ratio, thresholds),
        value_text=format_value(resource, value),
        allocated_text=format_allocated(resource, allocated),
    )
    return row


def top_projects(snapshot, resource, names, thresholds, limit=TOP_LIMIT):
    """
    The projects using the most of ``resource``, biggest first.

    Projects without a value for the resource count as 0 (they have running
    instances, just no sample of that meter). Ties are broken by name so
    the order is stable across refreshes.

    Returns:
        tuple: (at most ``limit`` rows, number of projects, total value)
    """
    projects = snapshot.get("projects", [])
    total = totals(snapshot)[resource]
    allocation = ALLOCATION_FIELDS.get(resource)
    rows = []
    for project in projects:
        name = names.get(project["id"])
        row = {
            "key": project["id"],
            "id": project["id"],
            "name": name,
            "label": name or project["id"],
            "instances": project.get("instances"),
        }
        _decorate(
            row,
            resource,
            resource_value(project, resource),
            project.get(allocation) if allocation else None,
            total,
            thresholds,
        )
        rows.append(row)
    rows.sort(key=lambda r: (-(r["value"] or 0), r["label"].lower()))
    return rows[:limit], len(rows), total


def instance_rows(instances, resource, total, names, thresholds):
    """
    Table rows for the instances a datasource returned (already ranked).

    Args:
        instances: instance rows (see the module docstring)
        total: total of ``resource`` the shares are relative to
        names: project id -> name
    """
    allocation = ALLOCATION_FIELDS.get(resource)
    rows = []
    for instance in instances:
        project_id = instance.get("project_id")
        row = {
            "key": instance["id"],
            "id": instance["id"],
            "name": instance.get("name"),
            "label": instance.get("name") or instance["id"],
            "project_id": project_id,
            "project_name": names.get(project_id),
            "project_label": names.get(project_id) or project_id,
        }
        _decorate(
            row,
            resource,
            instance.get("value"),
            instance.get(allocation) if allocation else None,
            total,
            thresholds,
        )
        rows.append(row)
    rows.sort(key=lambda r: (-(r["value"] or 0), r["label"].lower()))
    return rows


def find_project(snapshot, project_id):
    """The snapshot row of ``project_id``, or None."""
    for project in snapshot.get("projects", []):
        if project["id"] == project_id:
            return project
    return None


def idle_projects(snapshot, names, limit=IDLE_LIMIT):
    """
    Known projects with nothing running, by name.

    Returns:
        tuple: (at most ``limit`` names, number of idle projects)
    """
    active = {p["id"] for p in snapshot.get("projects", [])}
    idle = sorted(
        (name for pid, name in names.items() if pid not in active),
        key=str.lower,
    )
    return idle[:limit], len(idle)


def stack_with_other(series, total, keys):
    """
    Series of ``keys`` in order, plus "Other" (the total minus them).

    Args:
        series: key -> ``[value_or_None, ...]`` on a common grid
        total: ``[value_or_None, ...]``, the cloud (or project) total
        keys: the series to draw, biggest first

    Returns:
        list: ``[(key, values), ...]``; a missing key is all None. "Other"
        is left out when it is never above a rounding error. At a step
        where the total is missing, "Other" is missing too; a key missing
        at a step counts as 0 there.
    """
    length = len(total)
    result = [(key, list(series.get(key) or [None] * length)) for key in keys]
    other = []
    for i, value in enumerate(total):
        if value is None:
            other.append(None)
            continue
        drawn = sum(values[i] or 0 for _key, values in result)
        rest = value - drawn
        # rounding: the parts and the total are separate evaluations
        other.append(max(0.0, rest) if rest > abs(value) * 1e-6 else 0.0)
    if any(v for v in other):
        result.append((OTHER_KEY, other))
    return result


def trend_summary(values):
    """Latest, lowest and highest value of a trend (for screen readers)."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    return {"latest": known[-1], "min": min(known), "max": max(known)}


# Formatting ----------------------------------------------------------------

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def _number(value, digits):
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_bytes(value):
    """``1.5 GiB``: binary units, 3 significant digits at most."""
    if value is None or not math.isfinite(value):
        return "–"
    unit = 0
    while abs(value) >= 1024 and unit < len(_BYTE_UNITS) - 1:
        value /= 1024.0
        unit += 1
    digits = 0 if unit == 0 or abs(value) >= 100 else (1 if value >= 10 else 2)
    return f"{_number(value, digits)} {_BYTE_UNITS[unit]}"


def format_value(resource, value):
    """A resource value with its unit, e.g. ``3.55 cores``, ``1.2 MiB/s``."""
    if value is None:
        return "–"
    if resource == RESOURCE_CPU:
        return _("%(n)s cores") % {"n": _number(value, 2 if value < 10 else 1)}
    if resource == RESOURCE_MEMORY:
        return format_bytes(value * 1024 * 1024)
    return f"{format_bytes(value)}/s"


def format_allocated(resource, value):
    """An allocation (``5 vCPUs``, ``512 MiB``), or a dash."""
    if value is None:
        return "–"
    if resource == RESOURCE_CPU:
        return _("%(n)s vCPUs") % {"n": _number(value, 0)}
    if resource == RESOURCE_MEMORY:
        return format_bytes(value * 1024 * 1024)
    return "–"
