#
# SPDX-License-Identifier: Apache-2.0

"""
Instance metrics page logic (Project -> Telemetry -> Instance metrics).

Pure functions shared by the datasources and the views: input validation,
the ranges offered, the instance summary built from the Compute API, and
the tiles and charts built from the time series. No request handling.

The page shows one instance of the current project. Its metrics come in
four groups (one query each): CPU, memory, disk and network. Every series
is a list of ``[timestamp, value]`` points aligned to the range's grid
(``None`` for a missing step).
"""

import math
import re

from datetime import datetime

from django.utils.translation import gettext_lazy as _

from grian_ui import timeseries


# Nova server UUIDs as the Compute API returns them: lowercase hex with
# dashes. Strict, so the value is safe in PromQL strings and regexes.
_UUID_PATTERN = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
)

GROUP_CPU = "cpu"
GROUP_MEMORY = "memory"
GROUP_DISK = "disk"
GROUP_NETWORK = "network"
#: Chart groups, in page order. Each is one query and one partial.
GROUPS = (GROUP_CPU, GROUP_MEMORY, GROUP_DISK, GROUP_NETWORK)

#: Titles of the groups (shown when a group cannot be drawn).
GROUP_LABELS = {
    GROUP_CPU: _("CPU"),
    GROUP_MEMORY: _("Memory"),
    GROUP_DISK: _("Disk"),
    GROUP_NETWORK: _("Network"),
}

#: Series of each group (the ``series`` label of its query).
SERIES = {
    GROUP_CPU: ("cpu", "cpu_wait", "vcpus"),
    GROUP_MEMORY: ("memory", "memory_alloc", "swap"),
    GROUP_DISK: ("disk_read", "disk_write", "disk_write_latency"),
    GROUP_NETWORK: ("net_in", "net_out"),
}

#: Selectable ranges (the shared Telemetry ranges), the default one, and
#: the range the "current value" tiles are read from.
RANGES = tuple(timeseries.RANGES)
DEFAULT_RANGE = "24h"
TILE_RANGE = "1h"

STATE_ACTIVE = "active"
STATE_ERROR = "error"
STATE_SHUTOFF = "shutoff"
STATE_OTHER = "other"

#: Statuses without metrics that are not ERROR: the instance is not
#: running, so the polling agent has nothing to read.
_STOPPED_STATUSES = ("SHUTOFF", "SUSPENDED", "SHELVED", "SHELVED_OFFLOADED")


def validate_instance_id(instance_id):
    """
    Return ``instance_id`` if it is a server UUID, safe for PromQL and Nova.

    Raises ValueError for anything else (empty, uppercase, extra text).
    """
    if not instance_id or not isinstance(instance_id, str):
        raise ValueError("instance_id is required")
    if not _UUID_PATTERN.fullmatch(instance_id):
        raise ValueError(f"invalid instance ID: {instance_id!r}")
    return instance_id


def is_instance_id(value):
    """True if ``value`` is a server UUID (see validate_instance_id)."""
    try:
        validate_instance_id(value)
    except ValueError:
        return False
    return True


def instance_belongs_to_project(server, project_id):
    """
    True if ``server`` (a dict) belongs to ``project_id``.

    The Compute API returns other projects' instances to administrators,
    whatever project their token is scoped to: the page must check.
    """
    if not server or not project_id:
        return False
    server_project = server.get("project_id") or server.get("tenant_id")
    return server_project == project_id


def available_ranges(retention_days):
    """The ranges that fit in the metric retention (at least TILE_RANGE)."""
    limit = retention_days * 86400
    ranges = [
        key
        for key in RANGES
        if timeseries.duration_seconds(timeseries.RANGES[key][0]) <= limit
    ]
    return ranges or [TILE_RANGE]


def default_range(ranges):
    """DEFAULT_RANGE, or the longest offered range if it is not offered."""
    return DEFAULT_RANGE if DEFAULT_RANGE in ranges else ranges[-1]


def rate_window(range_key, minimum):
    """
    Window of the rate() calls for ``range_key``: its step, or ``minimum``.

    A window shorter than the step would skip samples between points; one
    shorter than two polling intervals has gaps.
    """
    _key, _window, step = timeseries.get_range(range_key)
    step_s = timeseries.duration_seconds(step)
    if step_s >= timeseries.duration_seconds(minimum):
        return step
    return minimum


def latest(values):
    """The last value that is not None, or None."""
    for value in reversed(values or []):
        if value is not None:
            return value
    return None


def summary(values):
    """``{"latest", "min", "max"}`` of the known values, or None."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    return {"latest": latest(values), "min": min(known), "max": max(known)}


def percent(part, whole):
    """``part`` as a percentage of ``whole``, or None."""
    if part is None or not whole:
        return None
    return 100.0 * part / whole


def wait_percent(wait_cores, vcpus, default_vcpus=None):
    """
    CPU wait as a share of the vCPU time, per point.

    Args:
        wait_cores: vCPU-seconds waited per second, per point
        vcpus: vCPU count per point (follows a resize; None if unknown)
        default_vcpus: vCPU count for points without one (the flavor's)
    """
    result = []
    for i, wait in enumerate(wait_cores):
        count = vcpus[i] if i < len(vcpus) else None
        result.append(percent(wait, count or default_vcpus))
    return result


def latency_with_writes(latency_ms, write_bytes, min_write_mib):
    """
    Latency per write only where the instance wrote at least the floor.

    With a handful of writes the time per write jumps around and means
    nothing (a few slow writes read as tens of ms), so those points become
    gaps.
    """
    floor = min_write_mib * 1048576
    result = []
    for i, latency in enumerate(latency_ms):
        written = write_bytes[i] if i < len(write_bytes) else None
        if latency is None or written is None or written < floor:
            result.append(None)
        else:
            result.append(latency)
    return result


# Formatting -------------------------------------------------------------


def format_bytes(value):
    """Binary units, like the admin dashboards: ``"1.5 MiB"``."""
    if value is None:
        return "–"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    i = 0
    while abs(value) >= 1024 and i < len(units) - 1:
        value /= 1024.0
        i += 1
    if i == 0 or abs(value) >= 100:
        return f"{value:.0f} {units[i]}"
    digits = 1 if abs(value) >= 10 else 2
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return f"{text} {units[i]}"


def format_rate(value):
    """Bytes per second: ``"850 MiB/s"``."""
    return "–" if value is None else f"{format_bytes(value)}/s"


def format_mib(value):
    """A size in MiB: ``"512 MiB"``, ``"4 GiB"``."""
    return "–" if value is None else format_bytes(value * 1048576)


def format_cores(value):
    """vCPUs busy: two decimals below 10."""
    if value is None:
        return "–"
    return f"{value:.2f}" if value < 10 else f"{value:.1f}"


def format_percent(value):
    """``"12%"``, ``"0.4%"`` below 1, ``"–"`` without a value."""
    if value is None:
        return "–"
    return f"{value:.1f}%" if value < 1 else f"{value:.0f}%"


def format_value(unit, value):
    """A chart value in its unit (see charts())."""
    if unit == "cores":
        return "–" if value is None else f"{format_cores(value)} vCPU"
    if unit == "percent":
        return format_percent(value)
    if unit == "mib":
        return format_mib(value)
    if unit == "bytes_rate":
        return format_rate(value)
    if unit == "ms":
        return "–" if value is None else f"{value:.1f} ms"
    return "–" if value is None else f"{value:g}"


# The instance -----------------------------------------------------------


def state_of(status):
    """Which explanation applies: active, error, shutoff or other."""
    status = (status or "").upper()
    if status == "ACTIVE":
        return STATE_ACTIVE
    if status == "ERROR":
        return STATE_ERROR
    if status in _STOPPED_STATUSES:
        return STATE_SHUTOFF
    return STATE_OTHER


def parse_time(value):
    """Epoch seconds of a Compute API timestamp, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # the Compute API always says UTC; never read it as local time
        return None
    return parsed.timestamp()


def addresses_of(addresses):
    """
    ``[{"network", "ips"}]`` from the Compute API ``addresses`` field.

    Fixed addresses first, then floating ones, IPv4 before IPv6.
    """
    result = []
    for network in sorted(addresses or {}):
        entries = addresses[network] or []
        ordered = sorted(
            entries,
            key=lambda a: (
                a.get("OS-EXT-IPS:type") == "floating",
                a.get("version") != 4,
            ),
        )
        ips = [a["addr"] for a in ordered if a.get("addr")]
        result.append({"network": network, "ips": ips})
    return result


def flavor_of(flavor, details=None):
    """
    ``{"name", "vcpus", "ram_mib", "disk_gb"}`` of a server's flavor.

    Args:
        flavor: the server's ``flavor`` field. With Compute API
            microversion 2.47 or later it embeds the flavor
            (``original_name``, ``vcpus``, ``ram``, ``disk``); before, only
            its ``id``
        details: the flavor read from the Compute API (``name``,
            ``vcpus``, ``ram``, ``disk``), when not embedded; None if it
            could not be read (e.g. deleted)
    """
    flavor = flavor or {}
    source = flavor if "vcpus" in flavor else details or {}
    return {
        "name": flavor.get("original_name")
        or source.get("name")
        or flavor.get("id"),
        "vcpus": source.get("vcpus"),
        "ram_mib": source.get("ram"),
        "disk_gb": source.get("disk"),
    }


def coverage(created_at, range_key, now):
    """
    How much of the range the instance existed for.

    Returns:
        dict: ``{"created_at", "hours"}`` when the instance was created
        inside the range (so the charts start late), else None
    """
    if created_at is None:
        return None
    _key, window, _step = timeseries.get_range(range_key)
    if now - created_at >= timeseries.duration_seconds(window):
        return None
    return {
        "created_at": int(created_at),
        "hours": max(0, int((now - created_at) // 3600)),
    }


# Tiles and charts -------------------------------------------------------


def _values(series, name):
    return [v for _t, v in series.get(name) or []]


def level_of(value, warning, critical):
    """``"danger"``, ``"warning"`` or ``""`` against the thresholds."""
    if value is None:
        return ""
    if value >= critical:
        return "danger"
    if value >= warning:
        return "warning"
    return ""


def tiles(groups, flavor, thresholds):
    """
    The current values (latest point of each series).

    Args:
        groups: ``{group: {series: [[t, v], ...]}}`` read over TILE_RANGE;
            missing groups are skipped
        flavor: see flavor_of; gives the vCPUs and RAM when the metrics
            have none
        thresholds: see config.get_project_instance_metrics_thresholds

    Returns:
        list: ``[{"key", "label", "value", "unit", "sub", "level"}]``,
        level being ``""``, ``"warning"`` or ``"danger"``
    """
    result = []
    cpu = groups.get(GROUP_CPU)
    if cpu is not None:
        result.extend(_cpu_tiles(cpu, flavor, thresholds))
    memory = groups.get(GROUP_MEMORY)
    if memory is not None:
        result.append(_memory_tile(memory, flavor))
    disk = groups.get(GROUP_DISK)
    if disk is not None:
        result.append(
            {
                "key": "disk",
                "label": _("Disk"),
                "value": format_rate(latest(_values(disk, "disk_write"))),
                "unit": "",
                "sub": _("written | %(read)s read")
                % {"read": format_rate(latest(_values(disk, "disk_read")))},
                "level": "",
            }
        )
    network = groups.get(GROUP_NETWORK)
    if network is not None:
        result.append(
            {
                "key": "network",
                "label": _("Network"),
                "value": format_rate(latest(_values(network, "net_in"))),
                "unit": "",
                "sub": _("in | %(out)s out")
                % {"out": format_rate(latest(_values(network, "net_out")))},
                "level": "",
            }
        )
    return result


def _cpu_tiles(cpu, flavor, thresholds):
    vcpus = latest(_values(cpu, "vcpus")) or flavor.get("vcpus")
    used = latest(_values(cpu, "cpu"))
    share = percent(used, vcpus)
    busy = share is not None and share >= thresholds["cpu_busy"]
    wait = latest(
        wait_percent(
            _values(cpu, "cpu_wait"),
            _values(cpu, "vcpus"),
            flavor.get("vcpus"),
        )
    )
    return [
        {
            "key": "cpu",
            "label": _("CPU"),
            "value": format_cores(used),
            "unit": _("of %(n)s vCPU") % {"n": f"{vcpus:g}"} if vcpus else "",
            "sub": (
                _("%(p)s of the flavor") % {"p": format_percent(share)}
                if share is not None
                else _("no vCPU count reported")
            ),
            "level": "danger" if busy else "",
        },
        {
            "key": "cpu_wait",
            "label": _("CPU wait"),
            "value": format_percent(wait),
            "unit": "",
            "sub": _("time waiting for a host CPU"),
            "level": level_of(
                wait,
                thresholds["cpu_wait_warning"],
                thresholds["cpu_wait_critical"],
            ),
        },
    ]


def _memory_tile(memory, flavor):
    used = latest(_values(memory, "memory"))
    alloc = latest(_values(memory, "memory_alloc")) or flavor.get("ram_mib")
    swap = latest(_values(memory, "swap"))
    share = percent(used, alloc)
    if swap is None:
        swap_text = _("swap not reported")
    elif swap:
        swap_text = _("swapping %(rate)s") % {
            "rate": format_rate(swap * 1048576)
        }
    else:
        swap_text = _("no swap")
    if share is not None:
        sub = _("of %(alloc)s (%(p)s) | %(swap)s") % {
            "alloc": format_mib(alloc),
            "p": format_percent(share),
            "swap": swap_text,
        }
    else:
        sub = swap_text
    return {
        "key": "memory",
        "label": _("Memory"),
        "value": format_mib(used),
        "unit": "",
        "sub": sub,
        "level": "warning" if swap else "",
    }


def _dataset(label, values, color, fill=False, dash=False):
    return {
        "label": str(label),
        "values": values,
        "color": color,
        "fill": fill,
        "dash": dash,
    }


def _flat(value, count):
    return [value] * count if value is not None else [None] * count


def _chart(key, label, unit, datasets, note, **extra):
    main = [d for d in datasets if not d["dash"]]
    summaries = []
    for dataset in main:
        found = summary(dataset["values"])
        if found is not None:
            summaries.append(
                {
                    "label": dataset["label"],
                    **{k: format_value(unit, v) for k, v in found.items()},
                }
            )
    chart = {
        "key": key,
        "label": str(label),
        "unit": unit,
        "datasets": datasets,
        "note": str(note),
        "has_data": bool(summaries),
        "summary": summaries,
        "max": None,
    }
    chart.update(extra)
    return chart


def _count(series):
    return max((len(points) for points in series.values()), default=0)


def charts(group, series, flavor, thresholds):
    """
    The charts of one group.

    Args:
        group: one of GROUPS
        series: ``{name: [[t, v], ...]}`` on the range grid
        flavor: see flavor_of (reference lines when metrics have none)
        thresholds: see config.get_project_instance_metrics_thresholds

    Returns:
        list: ``[{"key", "label", "unit", "datasets", "note", "has_data",
        "summary", "max", ...}]``; datasets are ``{"label", "values",
        "color", "fill", "dash"}`` (dashed: reference lines); units are
        "cores", "percent", "mib", "bytes_rate" or "ms"
    """
    if group == GROUP_CPU:
        return _cpu_charts(series, flavor, thresholds)
    if group == GROUP_MEMORY:
        return _memory_charts(series, flavor)
    if group == GROUP_DISK:
        return _disk_charts(series, thresholds)
    if group == GROUP_NETWORK:
        return _network_charts(series)
    raise ValueError(f"unknown group {group!r}")


def _cpu_charts(series, flavor, thresholds):
    n = _count(series)
    vcpus = _values(series, "vcpus")
    if not any(v is not None for v in vcpus):
        vcpus = _flat(flavor.get("vcpus"), n)
    count = latest(vcpus)
    wait = wait_percent(
        _values(series, "cpu_wait"), vcpus, flavor.get("vcpus")
    )
    vcpu_label = (
        _("vCPUs in flavor (%(n)s)") % {"n": f"{count:g}"}
        if count
        else _("vCPUs in flavor")
    )
    used = _chart(
        "cpu",
        _("CPU used (vCPUs)"),
        "cores",
        [
            _dataset(_("Used"), _values(series, "cpu"), "blue", fill=True),
            _dataset(vcpu_label, vcpus, "grey", dash=True),
        ],
        _(
            "If the line sits at the flavor limit, the instance needs more "
            "vCPUs."
        ),
        max=round(count * 1.15, 2) if count else None,
    )
    warning = thresholds["cpu_wait_warning"]
    critical = thresholds["cpu_wait_critical"]
    known = [v for v in wait if v is not None]
    top = max([critical + 5] + [math.ceil(v / 5) * 5 + 5 for v in known])
    waited = _chart(
        "cpu_wait",
        _("CPU wait (% of vCPU time)"),
        "percent",
        [
            _dataset(_("Wait"), wait, "blue", fill=True),
            _dataset(
                _("Warning %(v)s%%") % {"v": f"{warning:g}"},
                _flat(warning, n),
                "warning",
                dash=True,
            ),
            _dataset(
                _("Critical %(v)s%%") % {"v": f"{critical:g}"},
                _flat(critical, n),
                "critical",
                dash=True,
            ),
        ],
        _(
            "Time the guest was ready to run but the host had no free CPU. "
            "Caused by the host, not by your workload."
        ),
        max=top,
    )
    return [used, waited]


def _memory_charts(series, flavor):
    n = _count(series)
    alloc = _values(series, "memory_alloc")
    if not any(v is not None for v in alloc):
        alloc = _flat(flavor.get("ram_mib"), n)
    size = latest(alloc)
    swapped = [v for v in _values(series, "swap") if v]
    alloc_label = (
        _("Flavor RAM (%(size)s)") % {"size": format_mib(size)}
        if size
        else _("Flavor RAM")
    )
    note = str(
        _(
            "As reported by the guest balloon driver. Close to the limit, "
            "or any swap, means a bigger flavor is needed."
        )
    )
    if swapped:
        note = (
            str(
                _("The instance swapped in this range (up to %(rate)s).")
                % {"rate": format_rate(max(swapped) * 1048576)}
            )
            + " "
            + note
        )
    return [
        _chart(
            "memory",
            _("Memory used"),
            "mib",
            [
                _dataset(
                    _("Used"), _values(series, "memory"), "blue", fill=True
                ),
                _dataset(alloc_label, alloc, "grey", dash=True),
            ],
            note,
            max=round(size * 1.1, 1) if size else None,
            swapped=bool(swapped),
        )
    ]


def _disk_charts(series, thresholds):
    n = _count(series)
    write = _values(series, "disk_write")
    floor = thresholds["latency_min_write_mib"]
    measured = _values(series, "disk_write_latency")
    latency = latency_with_writes(measured, write, floor)
    throughput = _chart(
        "disk",
        _("Disk throughput"),
        "bytes_rate",
        [
            _dataset(_("Read"), _values(series, "disk_read"), "blue"),
            _dataset(_("Write"), write, "magenta"),
        ],
        _("All disks of the instance."),
    )
    warning = thresholds["disk_latency_warning"]
    critical = thresholds["disk_latency_critical"]
    lat = _chart(
        "disk_latency",
        _("Disk latency per write request (ms)"),
        "ms",
        [
            _dataset(_("Latency"), latency, "blue"),
            _dataset(
                _("Warning %(v)s ms") % {"v": f"{warning:g}"},
                _flat(warning, n),
                "warning",
                dash=True,
            ),
            _dataset(
                _("Critical %(v)s ms") % {"v": f"{critical:g}"},
                _flat(critical, n),
                "critical",
                dash=True,
            ),
        ],
        _(
            "Shown only while the instance writes at least %(floor)s: with "
            "very few writes the value jumps around and isn't meaningful. "
            "Gaps mean no (or too few) writes in that interval."
        )
        % {"floor": format_rate(floor * 1048576)},
        # measured, but never with enough writes to be shown
        too_few_writes=(
            any(v is not None for v in measured)
            and not any(v is not None for v in latency)
        ),
    )
    return [throughput, lat]


def _network_charts(series):
    return [
        _chart(
            "network",
            _("Network traffic"),
            "bytes_rate",
            [
                _dataset(
                    _("Inbound"), _values(series, "net_in"), "blue", fill=True
                ),
                _dataset(_("Outbound"), _values(series, "net_out"), "magenta"),
            ],
            _(
                "All interfaces. See the Network page for drops, errors and "
                "whether neighbours are affected."
            ),
        )
    ]
