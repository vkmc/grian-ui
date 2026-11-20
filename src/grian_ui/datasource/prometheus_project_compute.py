#
# SPDX-License-Identifier: Apache-2.0

"""
Prometheus queries of the Project Compute dashboard.

Used by :class:`grian_ui.datasource.prometheus.PrometheusTelemetryClient`;
every function takes that client and the current project's matcher
(:func:`grian_ui.project.project_matcher`), which goes into every selector:
Aetos only filters by project for users without an admin role. Every
selector also names its metric (Aetos rejects ``{__name__=~...}`` for
project users) and keeps only the polled samples
(``resource_name="<name>:instance-<uuid>"``): Nova notifications publish
``vcpus`` and ``memory`` again with a plain name, which would count every
instance twice over a range.

The number of queries never depends on the number of instances: one for
the current values, one for the 7-day statistics, one for the forecast,
one per chart, one for the sparklines of the (at most 25) listed instances
and one for an instance's detail charts. Each returns one value or series
per instance (per disk for the disk meters).

Ceilometer meters used:

* ``ceilometer_cpu`` and ``ceilometer_cpu_delay`` (ns, cumulative):
  ``rate() / 1e9`` is cores in use, and cores waiting for a host CPU;
* ``ceilometer_vcpus``, ``ceilometer_memory`` (flavor, MiB) and
  ``ceilometer_memory_usage`` (MiB used by the guest);
* ``ceilometer_memory_swap_in`` and ``ceilometer_memory_swap_out`` (MiB,
  cumulative);
* ``ceilometer_disk_device_usage`` and ``ceilometer_disk_device_capacity``
  (bytes, per disk);
* ``ceilometer_disk_device_write_bytes``,
  ``ceilometer_disk_device_write_requests`` and
  ``ceilometer_disk_device_write_latency`` (cumulative, per disk; the
  latency in ns).
"""

import re

from grian_ui import project_compute as pc
from grian_ui.datasource import prometheus


#: Only the polled samples of the instance meters.
POLLED = 'resource_name=~".+:instance-.+"'
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_UUID_RE = re.compile(_UUID)
#: ``resource`` of an instance (its UUID) or of a disk (``<uuid>-vda``).
_RESOURCE_RE = re.compile(f"({_UUID})(?:-(.+))?")
#: The project matcher must be exactly ``project="<id>"``.
_MATCHER_RE = re.compile(r'project="[A-Za-z0-9_-]{1,64}"')

#: Fields of the current values, per instance.
INSTANCE_FIELDS = (
    "vcpus",
    "memory_alloc",
    "cpu",
    "cpu_wait",
    "memory",
    "swap",
)
#: Fields of the current values, per disk.
DISK_FIELDS = ("used", "capacity", "write", "latency")
#: Fields of the 7-day statistics, per instance.
RIGHTSIZING_FIELDS = ("cpu_avg", "cpu_p95", "memory_avg", "swap", "hours")
#: Metric whose series give an instance's name and allocation.
_ALLOCATION = {
    pc.QUOTA_CORES: "ceilometer_vcpus",
    pc.QUOTA_RAM: "ceilometer_memory",
}


def is_instance_id(value):
    """True if ``value`` is an instance UUID (safe in a query)."""
    return bool(_UUID_RE.fullmatch(value or ""))


def _check(matcher):
    """Refuse anything but a project matcher (it scopes every query)."""
    if not _MATCHER_RE.fullmatch(matcher or ""):
        raise ValueError(f"invalid project matcher {matcher!r}")
    return matcher


def selector(metric, matcher, *extra):
    """``metric{polled,project,...}``: the only way queries select data."""
    parts = [POLLED, _check(matcher)] + [p for p in extra if p]
    return f"{metric}{{{','.join(parts)}}}"


def _instance_matcher(instance_ids):
    """``resource=~"a|b"`` for the valid instance IDs, or None."""
    valid = sorted({i for i in instance_ids if is_instance_id(i)})
    if not valid:
        return None
    return f'resource=~"{"|".join(valid)}"'


def _tagged(expr, field):
    return f'label_replace({expr}, "field", "{field}", "", "")'


def _gauge(metric, matcher, window, by="resource", extra=None):
    """Last value of a gauge per ``by`` (duplicated series reduced)."""
    return (
        f"max by ({by}) (last_over_time("
        f"{selector(metric, matcher, extra)}[{window}]))"
    )


def _rate(metric, matcher, window, extra=None):
    """rate() of a counter per resource (duplicated series reduced)."""
    return (
        f"max by (resource) (rate({selector(metric, matcher, extra)}"
        f"[{window}]))"
    )


#: Metrics whose series name an instance (any of them is enough).
_NAME_METRICS = (
    "ceilometer_vcpus",
    "ceilometer_memory",
    "ceilometer_memory_usage",
)


def name_expr(matcher, window=pc.CURRENT_WINDOW):
    """
    The most recent vCPU or memory series of each instance.

    A renamed instance has a series per name within ``window``; this keeps
    the one with the latest sample, whose ``resource_name`` is the current
    name (the value is that sample's timestamp). The memory meters name an
    instance that has no vCPU samples in the window.
    """
    latest = " or ".join(
        f"max_over_time(timestamp({selector(m, matcher)})[{window}:1m])"
        for m in _NAME_METRICS
    )
    return f"topk by (resource) (1, {latest})"


def snapshot_query(matcher, window=pc.CURRENT_WINDOW):
    """
    The current values of every instance and disk, in one query.

    Values are reduced per resource (a renamed instance has one series per
    name for a while); the ``name`` field gives the current name.
    """
    w = window
    exprs = {
        "name": name_expr(matcher, w),
        "vcpus": _gauge("ceilometer_vcpus", matcher, w),
        "memory_alloc": _gauge("ceilometer_memory", matcher, w),
        "cpu": f"{_rate('ceilometer_cpu', matcher, w)} / 1e9",
        "cpu_wait": f"{_rate('ceilometer_cpu_delay', matcher, w)} / 1e9",
        "memory": _gauge("ceilometer_memory_usage", matcher, w),
        # separate sums: the two meters differ in more labels than the name
        "swap": (
            f"{_rate('ceilometer_memory_swap_in', matcher, w)} + "
            f"{_rate('ceilometer_memory_swap_out', matcher, w)}"
        ),
        "disk:used": _gauge("ceilometer_disk_device_usage", matcher, w),
        "disk:capacity": _gauge("ceilometer_disk_device_capacity", matcher, w),
        "disk:write": _rate("ceilometer_disk_device_write_bytes", matcher, w),
        # ns per request -> ms; a disk without writes gives 0/0 (dropped)
        "disk:latency": (
            f"{_rate('ceilometer_disk_device_write_latency', matcher, w)} / "
            f"{_rate('ceilometer_disk_device_write_requests', matcher, w)}"
            " / 1e6"
        ),
    }
    return " or ".join(_tagged(expr, field) for field, expr in exprs.items())


def instance_name(resource_name):
    """``grian-vm`` from ``grian-vm:instance-<id>`` (None if no name)."""
    name, sep, _rest = (resource_name or "").rpartition(":instance-")
    return name if sep and name else None


def get_snapshot(client, matcher):
    """
    Current values of every running instance of the project (one query).

    Returns:
        dict: ``{"instances": {uuid: {"name", <INSTANCE_FIELDS>,
        "disks": {device: {<DISK_FIELDS>}}}}}`` (see
        :mod:`grian_ui.project_compute`)
    """
    instances = {}
    query = snapshot_query(matcher)
    for metric in client.client.query(query=query) or []:
        labels = metric.labels
        match = _RESOURCE_RE.fullmatch(labels.get("resource") or "")
        field = labels.get("field") or ""
        if not match:
            continue
        instance_id, device = match.group(1), match.group(2)
        value = prometheus.number(metric.value)
        row = instances.setdefault(
            instance_id,
            dict(dict.fromkeys(INSTANCE_FIELDS), name=None, disks={}),
        )
        if field.startswith("disk:"):
            name = field[len("disk:") :]
            if device and name in DISK_FIELDS:
                disk = row["disks"].setdefault(
                    device, dict.fromkeys(DISK_FIELDS)
                )
                disk[name] = value
            continue
        if device:
            continue
        if field == "name":
            row["name"] = instance_name(labels.get("resource_name"))
        elif field in INSTANCE_FIELDS:
            row[field] = value
    return {"instances": instances}


def rightsizing_query(matcher, window=pc.RIGHTSIZING_WINDOW):
    """
    7-day statistics of every instance, in one query.

    CPU: average and 95th percentile of the 10-minute rate. Memory: average
    of what the guest uses. Swap: MiB swapped in and out. Hours: hourly
    points with an allocation (how much history there is).
    """
    cw = pc.CURRENT_WINDOW
    cpu = f"{_rate('ceilometer_cpu', matcher, cw)} / 1e9"
    memory = _gauge("ceilometer_memory_usage", matcher, cw)
    swap = " + ".join(
        f"max by (resource) (increase({selector(metric, matcher)}[{window}]))"
        for metric in (
            "ceilometer_memory_swap_in",
            "ceilometer_memory_swap_out",
        )
    )
    exprs = {
        "cpu_avg": f"avg_over_time(({cpu})[{window}:{cw}])",
        "cpu_p95": f"quantile_over_time(0.95, ({cpu})[{window}:{cw}])",
        "memory_avg": f"avg_over_time(({memory})[{window}:{cw}])",
        "swap": swap,
        "hours": (
            f"count_over_time(({_gauge('ceilometer_vcpus', matcher, cw)})"
            f"[{window}:1h])"
        ),
    }
    return " or ".join(_tagged(expr, field) for field, expr in exprs.items())


def get_rightsizing(client, matcher):
    """
    7-day statistics of every instance that ran in the last 7 days.

    Returns:
        dict: ``{"instances": {uuid: {<RIGHTSIZING_FIELDS>}}}``
    """
    instances = {}
    for metric in client.client.query(query=rightsizing_query(matcher)) or []:
        resource = metric.labels.get("resource")
        field = metric.labels.get("field")
        if not is_instance_id(resource) or field not in RIGHTSIZING_FIELDS:
            continue
        row = instances.setdefault(resource, dict.fromkeys(RIGHTSIZING_FIELDS))
        row[field] = prometheus.number(metric.value)
    return {"instances": instances}


def total_expr(resource, matcher, window=pc.CURRENT_WINDOW):
    """What the project's running instances hold of a quota, now."""
    if resource == pc.QUOTA_INSTANCES:
        return f"count({_gauge('ceilometer_vcpus', matcher, window)})"
    return f"sum({_gauge(_ALLOCATION[resource], matcher, window)})"


def forecast_query(matcher, days):
    """
    Growth per second of each total over ``days`` (least squares, the
    slope ``predict_linear`` uses), and the hours with data.
    """
    lookback = f"{int(days)}d"
    parts = [
        _tagged(f"deriv(({total_expr(r, matcher)})[{lookback}:1h])", r)
        for r in pc.QUOTAS
    ]
    parts.append(
        _tagged(
            f"count_over_time(({total_expr(pc.QUOTA_INSTANCES, matcher)})"
            f"[{lookback}:1h])",
            "hours",
        )
    )
    return " or ".join(parts)


def get_forecast(client, matcher, days):
    """
    Growth of what running instances hold, over the last ``days``.

    Returns:
        dict: ``{"slopes": {quota: units per second or None}, "hours":
        hours with data or None, "days": days}``
    """
    result = {"slopes": dict.fromkeys(pc.QUOTAS), "hours": None, "days": days}
    for metric in (
        client.client.query(query=forecast_query(matcher, days)) or []
    ):
        field = metric.labels.get("field")
        value = prometheus.number(metric.value)
        if field in pc.QUOTAS:
            result["slopes"][field] = value
        elif field == "hours":
            result["hours"] = value
    return result


def trend_query(matcher, resource, window, step):
    """What running instances held of a quota, over ``window``."""
    return f"({total_expr(resource, matcher)})[{window}:{step}]"


def get_trend(client, matcher, resource, range_key, at=None):
    """
    What running instances held of ``resource`` over a chart range.

    Args:
        at: evaluation time (epoch), the end of the chart grid

    Returns:
        dict: ``{"points": [[ts, value], ...]}``
    """
    _key, window, step = pc.trend_range(range_key)
    points = []
    for _labels, series in client.query_matrix(
        trend_query(matcher, resource, window, step), ndigits=3, time=at
    ):
        points = series
    return {"points": points}


def cpu_series_query(matcher, instance_ids):
    """CPU in use (cores) of some instances over 7 days, or None."""
    instances = _instance_matcher(instance_ids)
    if instances is None:
        return None
    step = pc.SPARK_STEP
    cpu = _rate("ceilometer_cpu", matcher, step, instances)
    return f"({cpu} / 1e9)[{pc.RIGHTSIZING_WINDOW}:{step}]"


def get_cpu_series(client, matcher, instance_ids, at=None):
    """
    The 7-day CPU sparklines of the listed instances (one query).

    Returns:
        dict: ``{"series": {uuid: [[ts, cores], ...]}}``
    """
    query = cpu_series_query(matcher, instance_ids)
    result = {"series": {}}
    if query is None:
        return result
    for labels, points in client.query_matrix(query, ndigits=3, time=at):
        resource = labels.get("resource")
        if is_instance_id(resource):
            result["series"][resource] = points
    return result


def instance_trend_query(matcher, instance_id):
    """CPU (cores) and memory (MiB) of one instance over 7 days, or None."""
    instance = _instance_matcher([instance_id])
    if instance is None:
        return None
    step = pc.DETAIL_STEP
    cpu = f"{_rate('ceilometer_cpu', matcher, step, instance)} / 1e9"
    memory = _gauge("ceilometer_memory_usage", matcher, step, extra=instance)
    window = pc.RIGHTSIZING_WINDOW
    return (
        f"({_tagged(cpu, 'cpu')} or {_tagged(memory, 'memory')})"
        f"[{window}:{step}]"
    )


def get_instance_trend(client, matcher, instance_id, at=None):
    """
    CPU and memory of one instance over 7 days (one query).

    Returns:
        dict: ``{"cpu": [[ts, cores], ...], "memory": [[ts, MiB], ...]}``
    """
    query = instance_trend_query(matcher, instance_id)
    result = {"cpu": [], "memory": []}
    if query is None:
        return result
    for labels, points in client.query_matrix(query, ndigits=3, time=at):
        field = labels.get("field")
        if field in result:
            result[field] = points
    return result
