#
# SPDX-License-Identifier: Apache-2.0

"""
Prometheus queries of the project Instance metrics page.

One subquery per chart group (CPU, memory, disk, network) and range: each
series of the group is tagged with a ``series`` label and the series are
joined with ``or``, so a group costs one request to Aetos.

Every selector names its metric (Aetos rejects ``{__name__=~...}`` for
project users), carries the current project (Aetos does not filter for
users with an admin role) and selects this instance's series only:

* instance meters (``cpu``, ``memory``, ...): ``resource="<uuid>"``, and
  the polled samples only (``resource_name="<name>:<libvirt name>"``):
  Nova notifications publish the same meters with a plain name, which
  would count the instance twice over a range;
* disk meters: ``resource="<uuid>-<device>"``, polled samples only;
* network meters: ``resource="<libvirt name>-<uuid>-<vNIC>"``. They are
  polled only, and their ``resource_name`` is ``"<name>:<vNIC>"``, so the
  ``:instance-`` filter would drop them.

Gauges take the last sample over the rate window, counters a rate() over
it, so every point uses every sample since the previous one.
"""

import re

from grian_ui import project_instance_metrics as pim
from grian_ui import timeseries


#: Polled samples of the instance and disk meters.
POLLED = 'resource_name=~".+:instance-.+"'
#: What grian_ui.project.project_matcher() returns.
_PROJECT_MATCHER = re.compile(r'project="[A-Za-z0-9_-]{1,64}"')
#: Values are rounded for the browser (bytes/s, vCPUs, ms, MiB).
NDIGITS = 4


def _selectors(project_matcher, instance_id):
    """Label matchers of the instance, its disks and its vNICs."""
    instance_id = pim.validate_instance_id(instance_id)
    if not isinstance(project_matcher, str) or not _PROJECT_MATCHER.fullmatch(
        project_matcher
    ):
        raise ValueError(f"invalid project matcher {project_matcher!r}")
    proj = project_matcher
    return {
        "instance": f'{proj},resource="{instance_id}",{POLLED}',
        "disk": f'{proj},resource=~"{instance_id}-.+",{POLLED}',
        "vnic": f'{proj},resource=~".+-{instance_id}-.+"',
    }


def _rate(metric, sel, w):
    return f"sum(rate({metric}{{{sel}}}[{w}]))"


def _last(metric, sel, w):
    return f"max(last_over_time({metric}{{{sel}}}[{w}]))"


def _expressions(group, sel, w):
    """``[(series, PromQL expression)]`` of one group."""
    inst, disk, vnic = sel["instance"], sel["disk"], sel["vnic"]
    if group == pim.GROUP_CPU:
        return [
            # ns of CPU time per second -> vCPUs busy (or waiting)
            ("cpu", f"{_rate('ceilometer_cpu', inst, w)} / 1e9"),
            ("cpu_wait", f"{_rate('ceilometer_cpu_delay', inst, w)} / 1e9"),
            ("vcpus", _last("ceilometer_vcpus", inst, w)),
        ]
    if group == pim.GROUP_MEMORY:
        swap_in = _rate("ceilometer_memory_swap_in", inst, w)
        swap_out = _rate("ceilometer_memory_swap_out", inst, w)
        return [
            # MiB
            ("memory", _last("ceilometer_memory_usage", inst, w)),
            ("memory_alloc", _last("ceilometer_memory", inst, w)),
            # MiB/s swapped in + out; a plain "+" would drop the points
            # where only one of the two series is reported
            (
                "swap",
                f"({swap_in} or {swap_out} * 0) + "
                f"({swap_out} or {swap_in} * 0)",
            ),
        ]
    if group == pim.GROUP_DISK:
        return [
            ("disk_read", _rate("ceilometer_disk_device_read_bytes", disk, w)),
            (
                "disk_write",
                _rate("ceilometer_disk_device_write_bytes", disk, w),
            ),
            # ns per write request -> ms; without writes 0/0 is NaN,
            # which query_matrix drops
            (
                "disk_write_latency",
                f"{_rate('ceilometer_disk_device_write_latency', disk, w)} / "
                f"{_rate('ceilometer_disk_device_write_requests', disk, w)} "
                "/ 1e6",
            ),
        ]
    if group == pim.GROUP_NETWORK:
        return [
            ("net_in", _rate("ceilometer_network_incoming_bytes", vnic, w)),
            ("net_out", _rate("ceilometer_network_outgoing_bytes", vnic, w)),
        ]
    raise ValueError(f"unknown group {group!r}")


def group_query(
    project_matcher, group, instance_id, range_key, min_rate_window
):
    """
    The subquery of one group over ``range_key``.

    Raises ValueError for an invalid project matcher, instance ID or group
    (never queried).
    """
    _key, window, step = timeseries.get_range(range_key)
    w = pim.rate_window(range_key, min_rate_window)
    sel = _selectors(project_matcher, instance_id)
    parts = [
        f'label_replace({expr}, "series", "{name}", "", "")'
        for name, expr in _expressions(group, sel, w)
    ]
    return f"({' or '.join(parts)})[{window}:{step}]"


def get_instance_metrics(
    client,
    project_matcher,
    group,
    instance_id,
    range_key,
    min_rate_window,
    at=None,
):
    """
    The series of one group, as returned by Prometheus.

    Args:
        client: PrometheusTelemetryClient
        project_matcher: grian_ui.project.project_matcher(request), put
            in every selector
        group: one of project_instance_metrics.GROUPS
        instance_id: server UUID (validated here too)
        range_key: one of timeseries.RANGES
        min_rate_window: shortest rate() window, e.g. ``"10m"``
        at: evaluation time (epoch seconds); the views build the chart
            grid from it

    Returns:
        dict: ``{series: [[timestamp, value], ...]}`` with every series of
        the group (empty lists for the missing ones)

    Raises:
        ValueError: invalid project matcher, group or instance ID; query
            errors propagate
    """
    query = group_query(
        project_matcher, group, instance_id, range_key, min_rate_window
    )
    result = {name: [] for name in pim.SERIES[group]}
    for labels, points in client.query_matrix(query, ndigits=NDIGITS, time=at):
        name = labels.get("series")
        if name in result:
            result[name] = points
    return result
