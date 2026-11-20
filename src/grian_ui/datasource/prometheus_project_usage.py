#
# SPDX-License-Identifier: Apache-2.0

"""
Prometheus queries of the project Usage report.

Used by :class:`grian_ui.datasource.prometheus.PrometheusTelemetryClient`.
A report takes two queries whatever the number of instances and the length
of the period: one instant query for the per-instance totals and the
volume GB-hours, and one subquery for the hourly project totals of the
daily chart.

Every selector names its metric (Aetos refuses ``{__name__=~...}`` for
project users) and holds the current project
(:func:`grian_ui.project.project_matcher`: Aetos does not filter queries of
users with an admin role). Ceilometer meters used:

* ``ceilometer_vcpus`` and ``ceilometer_memory`` (flavor size, MiB): held;
* ``ceilometer_cpu`` (ns, cumulative) and ``ceilometer_memory_usage``
  (MiB, gauge): used;
* ``ceilometer_network_{incoming,outgoing}_bytes`` (cumulative, per vNIC);
* ``ceilometer_volume_size`` and ``ceilometer_volume_snapshot_size`` (GiB).

The instance meters are also published from Nova notifications with a
plain ``resource_name``; only the polled samples
(``resource_name="<name>:instance-<id>"``) are used, so instances are not
counted twice. Network samples are polled only (``resource_name`` is
``<name>:<vNIC>``). Each series is reduced to one value per resource
(``max by (resource)``) before summing, so duplicated series are not
counted twice either.

"Held" amounts are sums over a subquery grid of STEP_SECONDS: an instance
counts as running for a step when it has a sample in that step. That holds
whatever the polling interval only because sg-core exposes each polled
value until the next poll and Prometheus scrapes it every 15 s, so a
running instance has samples in every step. Samples sparser than the grid
(polled samples stored as they arrive, with polling slower than 5
minutes) would leave steps empty and under-count the held hours. Used CPU
time and traffic are ``increase()`` of the counters, which handles
counter resets (instance reboots).

An instance renamed during the period has one series per name: its name
and its last size (vCPUs, memory) come from the series with the latest
sample, so an instance renamed and resized shows its latest flavor.
"""

import datetime

from grian_ui import project
from grian_ui import project_usage as pu
from grian_ui.datasource import prometheus


#: Only the polled samples of the instance meters.
POLLED = 'resource_name=~".+:instance-.+"'
#: Only the polled samples of the vNIC meters (``<name>:<vNIC>``).
POLLED_VNIC = 'resource_name=~".+:.+"'
#: Instance UUID in a ``resource`` label: the instance itself or a vNIC
#: (``<libvirt name>-<uuid>-<vNIC>``); the greedy prefix picks the last
#: UUID followed by a dash, the instance's (as in Top Consumers).
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
INSTANCE_FROM_RESOURCE = f"(.*-)?({_UUID})(-.+)?"
#: Label carrying the instance UUID in per-instance results.
INSTANCE_LABEL = "instance_id"
#: Subquery step of the "held" sums.
STEP = f"{pu.STEP_SECONDS // 60}m"
#: Resolution of the daily chart data (summed into days by the view).
HOUR = 3600

#: Gauges summed over the subquery grid: field -> metric.
_HELD = {
    "vcpu_steps": "ceilometer_vcpus",
    "memory_steps": "ceilometer_memory",
    "memory_usage_steps": "ceilometer_memory_usage",
}
#: Counters: field -> (metric, filter of the polled samples).
_COUNTERS = {
    "cpu_ns": ("ceilometer_cpu", POLLED),
    "net_in": ("ceilometer_network_incoming_bytes", POLLED_VNIC),
    "net_out": ("ceilometer_network_outgoing_bytes", POLLED_VNIC),
}
#: Last flavor size of each instance: field -> metric.
_LAST = {
    "last_vcpus": "ceilometer_vcpus",
    "last_memory": "ceilometer_memory",
}
#: Project-wide storage gauges: field -> metric.
_STORAGE = {
    "volume_steps": "ceilometer_volume_size",
    "snapshot_steps": "ceilometer_volume_snapshot_size",
}


def _selector(metric, matcher, polled=None):
    parts = [matcher] + ([polled] if polled else [])
    return f"{metric}{{{','.join(parts)}}}"


def _subquery_range(seconds):
    """
    Subquery range covering ``seconds`` with exactly one point per step.

    One second short: Prometheus 2 also returns a point at the start of a
    range when it falls on a step boundary, which would count that step
    twice in consecutive windows.
    """
    return f"{max(1, int(seconds) - 1)}s"


def _per_resource_held(metric, matcher, window, aggregate="sum_over_time"):
    inner = f"max by (resource) ({_selector(metric, matcher, POLLED)})"
    return f"{aggregate}(({inner})[{_subquery_range(window)}:{STEP}])"


def _per_resource_increase(field, matcher, window):
    metric, polled = _COUNTERS[field]
    return (
        f"max by (resource) (increase("
        f"{_selector(metric, matcher, polled)}[{int(window)}s]))"
    )


def _per_instance(expr, by=INSTANCE_LABEL):
    return (
        f"sum by ({by}) (label_replace({expr}, \"{INSTANCE_LABEL}\", "
        f'"$2", "resource", "{INSTANCE_FROM_RESOURCE}"))'
    )


def _tagged(expr, field):
    return f'label_replace({expr}, "field", "{field}", "", "")'


def totals_query(matcher, window):
    """
    Per-instance totals and storage GB-hours over the last ``window``
    seconds (one instant query, one series per instance and field).
    """
    exprs = [
        _tagged(
            _per_instance(
                _per_resource_held(
                    "ceilometer_vcpus", matcher, window, "count_over_time"
                )
            ),
            "running_steps",
        )
    ]
    for field, metric in _HELD.items():
        exprs.append(
            _tagged(
                _per_instance(_per_resource_held(metric, matcher, window)),
                field,
            )
        )
    for field in _COUNTERS:
        exprs.append(
            _tagged(
                _per_instance(_per_resource_increase(field, matcher, window)),
                field,
            )
        )
    # names (resource_name, one series per name with the time of its last
    # sample) and the last flavor size, for instances deleted since (the
    # Compute API no longer lists them)
    exprs.append(
        _tagged(
            _per_instance(
                f"max by (resource, resource_name) (max_over_time(timestamp("
                f"{_selector('ceilometer_vcpus', matcher, POLLED)}"
                f")[{_subquery_range(window)}:{STEP}]))",
                by=f"{INSTANCE_LABEL}, resource_name",
            ),
            "name_seen",
        )
    )
    # the last size of each name series (paired with name_seen by the
    # parser)
    for field, metric in _LAST.items():
        exprs.append(
            _tagged(
                _per_instance(
                    f"max by (resource, resource_name) (last_over_time("
                    f"{_selector(metric, matcher, POLLED)}"
                    f"[{int(window)}s]))",
                    by=f"{INSTANCE_LABEL}, resource_name",
                ),
                field,
            )
        )
    for field, metric in _STORAGE.items():
        exprs.append(
            _tagged(
                f"sum_over_time((sum(max by (resource) ("
                f"{_selector(metric, matcher)})))"
                f"[{_subquery_range(window)}:{STEP}])",
                field,
            )
        )
    return " or ".join(exprs)


def hourly_query(matcher, window):
    """
    Project totals per hour over the last ``window`` seconds (a subquery:
    one series per field, one point per hour, ending at each hour).
    """
    exprs = []
    for field, metric in _HELD.items():
        inner = f"max by (resource) ({_selector(metric, matcher, POLLED)})"
        exprs.append(
            _tagged(
                f"sum(sum_over_time(({inner})"
                f"[{_subquery_range(HOUR)}:{STEP}]))",
                field,
            )
        )
    for field in _COUNTERS:
        exprs.append(
            _tagged(
                f"sum({_per_resource_increase(field, matcher, HOUR)})", field
            )
        )
    return f"({' or '.join(exprs)})[{_subquery_range(window)}:1h]"


def _epoch(moment):
    return moment.timestamp()


def _ceil_hour(moment):
    start = moment.replace(minute=0, second=0, microsecond=0)
    return start if start == moment else start + datetime.timedelta(hours=1)


def _steps_to_hours(value):
    return value * pu.STEP_SECONDS / 3600.0


def _latest_name_and_size(fields):
    """
    The name and last sizes of an instance from its series (one per name):
    those of the series with the latest sample.
    """
    seen = fields.get("seen", {})

    def latest(resource_name):
        # ties: the larger name, so the result does not depend on the
        # series order
        return (seen.get(resource_name, float("-inf")), resource_name)

    name = None
    if seen:
        name = max(seen, key=latest).rsplit(":instance-", 1)[0] or None
    sizes = {}
    for field in _LAST:
        values = fields.get(field)
        if values:
            # ties (no name series): the larger size
            sizes[field] = max(
                values.items(), key=lambda item: (*latest(item[0]), item[1])
            )[1]
    return name, sizes


def parse_totals(results):
    """
    The ``instances``, ``volume_gb_hours`` and ``snapshot_gb_hours`` of a
    report from the totals query results (``[(labels, value), ...]``).
    """
    raw = {}
    storage = {}
    for labels, sample in results or []:
        labels = labels or {}
        field = labels.get("field")
        value = prometheus.number(sample)
        if value is None:
            continue
        if field in _STORAGE:
            storage[field] = storage.get(field, 0.0) + value
            continue
        uuid = labels.get(INSTANCE_LABEL)
        if not uuid:
            continue
        fields = raw.setdefault(uuid, {})
        resource_name = labels.get("resource_name") or ""
        if field == "name_seen":
            # one series per name, with the time of its last sample
            fields.setdefault("seen", {})[resource_name] = value
        elif field in _LAST:
            # one series per name: its last size
            fields.setdefault(field, {})[resource_name] = value
        else:
            # per-instance sums already (vNICs summed in the query)
            fields[field] = fields.get(field, 0.0) + value
    instances = {}
    for uuid, f in raw.items():
        name, sizes = _latest_name_and_size(f)
        instances[uuid] = {
            "name": name,
            "vcpus": sizes.get("last_vcpus"),
            "memory_mib": sizes.get("last_memory"),
            "hours": _steps_to_hours(f.get("running_steps", 0.0)),
            "vcpu_hours": _steps_to_hours(f.get("vcpu_steps", 0.0)),
            "cpu_hours": f.get("cpu_ns", 0.0) / 1e9 / 3600.0,
            "ram_gb_hours_held": _steps_to_hours(f.get("memory_steps", 0.0))
            / 1024.0,
            "ram_gb_hours_used": _steps_to_hours(
                f.get("memory_usage_steps", 0.0)
            )
            / 1024.0,
            "net_in": f.get("net_in", 0.0),
            "net_out": f.get("net_out", 0.0),
        }

    def gb_hours(field):
        if field not in storage:
            return None
        return _steps_to_hours(storage[field])

    return {
        "instances": instances,
        "volume_gb_hours": gb_hours("volume_steps"),
        "snapshot_gb_hours": gb_hours("snapshot_steps"),
    }


def parse_hourly(series, start):
    """
    The ``hourly`` part of a report from the hourly query results.

    Each point ends its hour; it is keyed by the hour's start, and hours
    starting before ``start`` (epoch) are dropped.
    """
    by_field = {}
    for labels, points in series or []:
        field = (labels or {}).get("field")
        if field:
            by_field[field] = points
    convert = {
        "vcpu_hours": ("vcpu_steps", _steps_to_hours),
        "cpu_hours": ("cpu_ns", lambda v: v / 1e9 / 3600.0),
        "ram_gb_hours_held": (
            "memory_steps",
            lambda v: _steps_to_hours(v) / 1024.0,
        ),
        "ram_gb_hours_used": (
            "memory_usage_steps",
            lambda v: _steps_to_hours(v) / 1024.0,
        ),
    }
    hourly = {}
    for field, (source, scale) in convert.items():
        hourly[field] = [
            [t - HOUR, scale(v)]
            for t, v in by_field.get(source, [])
            if t - HOUR >= start
        ]
    traffic = {}
    for source in ("net_in", "net_out"):
        for t, v in by_field.get(source, []):
            if t - HOUR >= start:
                traffic[t - HOUR] = traffic.get(t - HOUR, 0.0) + v
    hourly["net_bytes"] = [[t, v] for t, v in sorted(traffic.items())]
    return hourly


def query_vector(client, query, time):
    """
    Run an instant query evaluated at ``time`` (epoch seconds).

    ``PrometheusAPIClient.query`` has no evaluation time, so the raw
    response is decoded here, as ``query_matrix`` does.

    Returns:
        list: ``[(labels, value), ...]``
    """
    # _get is the method PrometheusAPIClient.query itself uses
    decoded = client.client._get(
        "query", {"query": query, "time": f"{time:.3f}"}
    )
    data = decoded.get("data", {})
    if data.get("resultType") != "vector":
        raise ValueError(
            f"expected an instant vector, got {data.get('resultType')}"
        )
    return [
        (result.get("metric", {}), (result.get("value") or [None, None])[1])
        for result in data.get("result", [])
    ]


def get_usage(client, request, start, end):
    """
    The usage report of the request's project between ``start`` and
    ``end`` (aware datetimes; see :mod:`grian_ui.project_usage`).
    """
    matcher = project.project_matcher(request)
    window = _epoch(end) - _epoch(start)
    results = query_vector(client, totals_query(matcher, window), _epoch(end))
    report = parse_totals(results)
    # the chart grid ends at the end of the current hour, so the last
    # (partial) hour is included
    chart_end = _ceil_hour(end)
    series = client.query_matrix(
        hourly_query(matcher, _epoch(chart_end) - _epoch(start)),
        time=_epoch(chart_end),
    )
    report["hourly"] = parse_hourly(series, _epoch(start))
    report["start"] = _epoch(start)
    report["end"] = _epoch(end)
    return report
