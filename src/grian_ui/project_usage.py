#
# SPDX-License-Identifier: Apache-2.0

"""
Project usage report.

Pure functions shared by the datasources and the Project -> Telemetry ->
Usage views. A report covers one calendar month (UTC) of the current
project: what its instances held (flavor size x time running) and what
they used (CPU time, memory, network), plus volume and snapshot GB-hours.

The metric store keeps a limited history (Prometheus: 15 days by default,
``GRIAN_PLUGIN['metrics_retention_days']``), so a month is often only
partly available. :func:`period_info` computes the range a report really
covers: from the later of the start of the month and the first full day
still kept, to the end of the month or now. Days outside that range are
reported as "not kept", never as zero usage, and months entirely outside
it cannot be selected.

Report format (returned by ``DatasourceClient.get_project_usage``)::

    {
        "instances": {
            "<uuid>": {
                "name": "web-1" or None,  # from resource_name
                "vcpus": 4.0 or None,  # last value in the period
                "memory_mib": 4096.0 or None,  # last value in the period
                "hours": 12.5,  # hours reporting metrics
                "vcpu_hours": 50.0,  # vCPUs x hours
                "cpu_hours": 31.2,  # CPU time used
                "ram_gb_hours_held": 50.0,  # flavor RAM (GiB) x hours
                "ram_gb_hours_used": 10.1,  # memory used (GiB) x hours
                "net_in": 123.0,  # bytes received
                "net_out": 456.0,  # bytes sent
            },
        },
        "volume_gb_hours": 24.0,  # volume size (GiB) x hours, or None
        "snapshot_gb_hours": 0.0,  # snapshot size (GiB) x hours, or None
        "hourly": {  # project totals per hour, keyed by the hour's start
            "vcpu_hours": [[epoch, value], ...],
            "cpu_hours": [...],
            "ram_gb_hours_held": [...],
            "ram_gb_hours_used": [...],
            "net_bytes": [...],
        },
    }

Servers (from the Compute API, see the views)::

    {"id": uuid, "name": str, "status": "ACTIVE", "flavor": "m1.small"}
"""

import datetime
import decimal
import math

from django.utils.dates import MONTHS
from django.utils.translation import gettext as _


UTC = datetime.timezone.utc

#: Resolution of the "held" sums (subquery step), in seconds: an instance
#: counts as running for a step when it has a sample in the step.
STEP_SECONDS = 300
#: Most months listed in the period selector (the current one included).
MAX_MONTHS = 12

#: Hourly fields of the report.
HOURLY_FIELDS = (
    "vcpu_hours",
    "cpu_hours",
    "ram_gb_hours_held",
    "ram_gb_hours_used",
    "net_bytes",
)
#: Daily chart metrics: key -> (held field or None, used field).
CHART_METRICS = {
    "cpu": ("vcpu_hours", "cpu_hours"),
    "mem": ("ram_gb_hours_held", "ram_gb_hours_used"),
    "net": (None, "net_bytes"),
}
#: Columns of the CSV export, in order.
CSV_COLUMNS = (
    "period_start",
    "period_end",
    "instance",
    "instance_id",
    "flavor",
    "hours_running",
    "vcpu_hours_held",
    "cpu_hours_used",
    "ram_gb_hours_held",
    "ram_gb_hours_used",
    "network_in_bytes",
    "network_out_bytes",
)

#: Day states of the daily chart.
DAY_KEPT = "kept"
DAY_NOT_KEPT = "not_kept"

#: Per-instance fields summed in the table footer.
SUM_FIELDS = (
    "hours",
    "vcpu_hours",
    "cpu_hours",
    "ram_gb_hours_held",
    "ram_gb_hours_used",
    "net_in",
    "net_out",
)


# Periods ---------------------------------------------------------------


def utcnow():
    """The current time, timezone-aware (UTC)."""
    return datetime.datetime.now(UTC)


def _month_start(year, month):
    return datetime.datetime(year, month, 1, tzinfo=UTC)


def _add_months(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _ceil_day(moment):
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start if start == moment else start + datetime.timedelta(days=1)


def period_key(year, month):
    return f"{year:04d}-{month:02d}"


def parse_period(key, now):
    """
    ``(year, month)`` of a ``YYYY-MM`` key, or None.

    Months after the current one (at ``now``) are refused.
    """
    if not isinstance(key, str) or len(key) != 7 or key[4] != "-":
        return None
    year, month = key[:4], key[5:]
    if not (year.isascii() and year.isdigit()):
        return None
    if not (month.isascii() and month.isdigit()):
        return None
    year, month = int(year), int(month)
    if not (1 <= month <= 12 and year >= 1970):
        return None
    if (year, month) > (now.year, now.month):
        return None
    return year, month


def month_label(year, month):
    """``November 2026`` (localised month name)."""
    return _("%(month)s %(year)s") % {
        "month": MONTHS[month],
        "year": year,
    }


def first_kept(now, retention_days):
    """First full UTC day still in the metric store."""
    return _ceil_day(now - datetime.timedelta(days=retention_days))


def period_info(year, month, now, retention_days):
    """
    The range a report of ``year``/``month`` covers.

    Args:
        now: aware datetime (UTC)
        retention_days: days of history the metric store keeps

    Returns:
        dict: ``key``, ``label``, ``is_current``, ``month_start``,
        ``month_end`` (start of the next month), ``start`` and ``end``
        (the covered range; ``end`` is now for the current month),
        ``available`` (any day of the month kept), ``complete`` (every
        day of the month so far is kept), ``incomplete`` (days of
        the month were deleted), ``days_total`` (days of the month so far),
        ``days_covered``, ``first_kept``, ``retention_days``,
        ``locked_reason`` (why the month cannot be selected, or None)
    """
    month_start = _month_start(year, month)
    month_end = _month_start(*_add_months(year, month, 1))
    is_current = month_start <= now < month_end
    end = min(month_end, now)
    kept_from = first_kept(now, retention_days)
    start = max(month_start, kept_from)
    available = start < end
    locked_reason = None
    if not available:
        locked_reason = _(
            "Not available: older than the %(days)s days of metrics "
            "this cloud keeps"
        ) % {"days": retention_days}
    label = month_label(year, month)
    if is_current:
        label = _("%(month)s (to date)") % {"month": label}
    return {
        "key": period_key(year, month),
        "year": year,
        "month": month,
        "label": label,
        "is_current": is_current,
        "month_start": month_start,
        "month_end": month_end,
        "start": start,
        "end": end,
        "available": available,
        "complete": available and start == month_start,
        "incomplete": available and start > month_start,
        "days_total": max(0, (_ceil_day(end) - month_start).days),
        "days_covered": (_ceil_day(end) - start).days if available else 0,
        "first_kept": kept_from,
        "retention_days": retention_days,
        "locked_reason": locked_reason,
    }


def list_periods(now, retention_days, limit=MAX_MONTHS):
    """
    Months of the period selector, newest first.

    Every month with kept days, then the first month without any (shown
    locked), at most ``limit`` months.
    """
    periods = []
    year, month = now.year, now.month
    while len(periods) < limit:
        info = period_info(year, month, now, retention_days)
        periods.append(info)
        if not info["available"]:
            break
        year, month = _add_months(year, month, -1)
    return periods


def format_day(day):
    """``11/4``."""
    return f"{day.month}/{day.day}"


def format_moment(moment):
    """``11/4 00:00``."""
    return f"{moment.month}/{moment.day} {moment:%H:%M}"


def format_range(start, end):
    """``11/4 00:00 – 11/18 20:20``; a midnight end is ``11/30 24:00``."""
    if (end.hour, end.minute, end.second) == (0, 0, 0):
        before = end - datetime.timedelta(days=1)
        last = f"{format_day(before)} 24:00"
    else:
        last = format_moment(end)
    return f"{format_moment(start)} – {last}"


def day_states(period):
    """
    ``[(date, state), ...]`` for each day of the month so far.

    States: DAY_KEPT, DAY_NOT_KEPT (deleted from the metric store).
    """
    days = []
    day = period["month_start"]
    while day < period["end"]:
        if day < period["start"]:
            state = DAY_NOT_KEPT
        else:
            state = DAY_KEPT
        days.append((day.date(), state))
        day += datetime.timedelta(days=1)
    return days


def missing_bands(states, retention_days):
    """
    Runs of days without data, for the grey chart bands.

    Returns:
        list: ``{"from": index, "to": index, "label": str}`` (inclusive)
    """
    label = _("not kept: older than %(days)s days") % {"days": retention_days}
    bands = []
    for index, (_day, state) in enumerate(states):
        if state == DAY_KEPT:
            continue
        last = bands[-1] if bands else None
        if last and last["to"] == index - 1:
            last["to"] = index
        else:
            bands.append({"from": index, "to": index, "label": label})
    return bands


# Rows ------------------------------------------------------------------


def _num(value):
    """A finite float, or 0.0."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def ratio_percent(used, held):
    """``used`` ÷ ``held`` in percent, or None without anything held."""
    if not held or held <= 0:
        return None
    return 100.0 * used / held


def flavor_from_metrics(vcpus, memory_mib):
    """``4 vCPU, 4.0 GiB`` from the instance meters, or None."""
    vcpus, memory_mib = _num(vcpus), _num(memory_mib)
    parts = []
    if vcpus > 0:
        parts.append(_("%(n)s vCPU") % {"n": _number(vcpus, 0)})
    if memory_mib > 0:
        parts.append(format_bytes(memory_mib * 1048576))
    return ", ".join(parts) or None


def instance_rows(report, servers, thresholds):
    """
    The by-instance table: every instance with usage in the period.

    Names, flavors and states come from the Compute API (``servers``, or
    None when it could not be read). Instances deleted since keep the name
    from their metrics and the size from ``vcpus`` and ``memory``.

    Returns:
        list: rows sorted by vCPU-hours held, biggest first
    """
    by_id = {s["id"]: s for s in servers or []}
    low = thresholds["cpu_use_low"]
    rows = []
    for uuid, usage in (report.get("instances") or {}).items():
        values = {field: _num(usage.get(field)) for field in SUM_FIELDS}
        if not any(values.values()):
            continue
        server = by_id.get(uuid) or {}
        ratio = ratio_percent(values["cpu_hours"], values["vcpu_hours"])
        rows.append(
            dict(
                values,
                id=uuid,
                name=server.get("name") or usage.get("name") or uuid,
                flavor=server.get("flavor")
                or flavor_from_metrics(
                    usage.get("vcpus"), usage.get("memory_mib")
                ),
                status=server.get("status"),
                # None: the Compute API could not be read
                deleted=None if servers is None else not server,
                cpu_ratio=ratio,
                # judged on the percentage as shown (a whole number)
                cpu_low=ratio is not None
                and float(format_percent(ratio).replace(",", "")) < low,
                ram_ratio=ratio_percent(
                    values["ram_gb_hours_used"], values["ram_gb_hours_held"]
                ),
            )
        )
    rows.sort(key=lambda r: (-r["vcpu_hours"], str(r["name"]), r["id"]))
    return rows


def not_listed(servers, rows):
    """
    Instances of the project without usage in the period, by name.

    They report no metrics (ERROR, SHUTOFF, SHELVED...) but still hold
    quota.
    """
    listed = {r["id"] for r in rows}
    found = [
        {
            "id": s["id"],
            "name": s.get("name") or s["id"],
            "status": s.get("status") or "",
        }
        for s in servers or []
        if s["id"] not in listed
    ]
    found.sort(key=lambda s: (str(s["name"]), s["id"]))
    return found


def totals(rows, report):
    """Sums of the table, plus the volume and snapshot GB-hours."""
    result = {field: sum(r[field] for r in rows) for field in SUM_FIELDS}
    result["instances"] = len(rows)
    result["cpu_ratio"] = ratio_percent(
        result["cpu_hours"], result["vcpu_hours"]
    )
    result["ram_ratio"] = ratio_percent(
        result["ram_gb_hours_used"], result["ram_gb_hours_held"]
    )
    result["net_total"] = result["net_in"] + result["net_out"]
    volume = report.get("volume_gb_hours")
    snapshot = report.get("snapshot_gb_hours")
    result["volume_gb_hours"] = _num(volume)
    result["snapshot_gb_hours"] = _num(snapshot)
    result["storage_gb_hours"] = (
        result["volume_gb_hours"] + result["snapshot_gb_hours"]
    )
    return result


# Daily chart -----------------------------------------------------------


def first_data(report):
    """Start (epoch) of the first hour with usage, or None."""
    first = None
    for points in (report.get("hourly") or {}).values():
        for timestamp, value in points:
            if _num(value) > 0 and (first is None or timestamp < first):
                first = timestamp
    return first


def daily(report, period):
    """
    The daily chart: one value per day of the month so far.

    Days not kept are None and drawn as grey bands; kept days without usage are 0.

    Returns:
        dict: ``labels``, ``bands``, and per chart metric
        ``{"held": [...] or None, "used": [...]}``
    """
    states = day_states(period)
    index = {day: i for i, (day, _state) in enumerate(states)}
    hourly = report.get("hourly") or {}
    sums = {}
    for field in HOURLY_FIELDS:
        sums[field] = [0.0] * len(states)
        for timestamp, value in hourly.get(field, []):
            day = datetime.datetime.fromtimestamp(timestamp, UTC).date()
            i = index.get(day)
            if i is not None:
                sums[field][i] += _num(value)
    kept = [state == DAY_KEPT for _day, state in states]

    def values(field):
        if field is None:
            return None
        digits = 0 if field == "net_bytes" else 2
        return [
            round(v, digits) if ok else None
            for v, ok in zip(sums[field], kept)
        ]

    result = {
        "labels": [format_day(day) for day, _state in states],
        "bands": missing_bands(states, period["retention_days"]),
    }
    for metric, (held, used) in CHART_METRICS.items():
        result[metric] = {"held": values(held), "used": values(used)}
    return result


# CSV -------------------------------------------------------------------


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _csv_text(value):
    """Neutralise spreadsheet formulas in user-chosen names."""
    text = str(value or "")
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def csv_rows(rows, period):
    """CSV records (header first), in machine-readable units."""
    start, end = _iso(period["start"]), _iso(period["end"])
    records = [list(CSV_COLUMNS)]
    for row in rows:
        records.append(
            [
                start,
                end,
                _csv_text(row["name"]),
                row["id"],
                _csv_text(row["flavor"]),
                f"{row['hours']:.2f}",
                f"{row['vcpu_hours']:.2f}",
                f"{row['cpu_hours']:.2f}",
                f"{row['ram_gb_hours_held']:.2f}",
                f"{row['ram_gb_hours_used']:.2f}",
                str(int(round(row["net_in"]))),
                str(int(round(row["net_out"]))),
            ]
        )
    return records


def csv_filename(project_name, period):
    """``usage-<project>-<first day>_to_<last day>.csv`` (safe chars)."""
    safe = "".join(
        c if c.isascii() and (c.isalnum() or c in "-_.") else "_"
        for c in str(project_name or "project")
    )[:64]
    end = period["end"]
    if (end.hour, end.minute, end.second) == (0, 0, 0):
        end -= datetime.timedelta(days=1)
    return f"usage-{safe}-{period['start']:%Y-%m-%d}_to_{end:%Y-%m-%d}.csv"


# Formatting ------------------------------------------------------------


def _number(value, digits):
    """Round half up (as people do), with thousands separators."""
    quantum = decimal.Decimal(1).scaleb(-digits)
    rounded = decimal.Decimal(repr(float(value))).quantize(
        quantum, rounding=decimal.ROUND_HALF_UP
    )
    return f"{rounded:,f}"


def format_amount(value):
    """``39.0``, ``1,234``: one decimal under 100, whole numbers above."""
    if value is None:
        return "–"
    value = float(value)
    return _number(value, 0) if abs(value) >= 100 else _number(value, 1)


def format_bytes(value):
    """Binary units, as the other Telemetry dashboards: ``1.4 GiB``."""
    if value is None:
        return "–"
    value = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    index = 0
    while abs(value) >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    digits = 0 if index == 0 or abs(value) >= 100 else 1
    text = _number(value, digits)
    if text.endswith(".0"):
        text = text[:-2]
    return f"{text} {units[index]}"


def format_percent(value):
    """``83`` (whole percent), or None."""
    return None if value is None else _number(value, 0)
