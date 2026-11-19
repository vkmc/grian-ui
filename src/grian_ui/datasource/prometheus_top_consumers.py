#
# SPDX-License-Identifier: Apache-2.0

"""
Prometheus queries of the Top Consumers dashboard.

Used by :class:`grian_ui.datasource.prometheus.PrometheusTelemetryClient`;
every function takes that client. The number of queries never depends on
the size of the cloud: one for the per-project snapshot, two for a top
instance list, one per chart.

Ceilometer meters used (see :mod:`grian_ui.top_consumers`):

* ``ceilometer_cpu`` (ns, cumulative): ``rate() / 1e9`` is cores in use;
* ``ceilometer_memory_usage`` (MiB, gauge);
* ``ceilometer_disk_device_{read,write}_bytes`` (cumulative, per disk);
* ``ceilometer_network_{incoming,outgoing}_bytes`` (cumulative, per vNIC);
* ``ceilometer_vcpus`` and ``ceilometer_memory`` (flavor allocation).

The instance-level meters are also published from Nova notifications,
with a plain ``resource_name``; only the polled samples
(``resource_name="<name>:instance-<id>"``) are used, so instances are not
counted twice. Network samples are polled only, and their ``resource_name``
is ``<name>:<vNIC>``.
"""

import re

from grian_ui import top_consumers as tc
from grian_ui.datasource import prometheus


#: Only the polled samples of the instance meters.
POLLED = 'resource_name=~".+:instance-.+"'
#: Instance UUID in a ``resource`` label: the instance itself, a disk
#: (``<uuid>-vda``) or a vNIC (``<libvirt name>-<uuid>-<vNIC>``). The
#: greedy prefix picks the last UUID followed by a dash, the instance's.
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_INSTANCE_FROM_RESOURCE = f"(.*-)?({_UUID})(-.+)?"
#: Label carrying the instance UUID in per-instance results.
INSTANCE_LABEL = "instance_id"

_UUID_RE = re.compile(_UUID)
#: Keystone project IDs accepted in queries (UUID hex by default; LDAP and
#: other backends may use other ID forms of the same characters).
_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: Counter meters summed into each resource, with the label added to keep
#: their series apart before the sum (they may share every other label).
_COUNTERS = {
    tc.RESOURCE_CPU: (("ceilometer_cpu", POLLED, "cpu"),),
    tc.RESOURCE_DISK: (
        ("ceilometer_disk_device_read_bytes", POLLED, "read"),
        ("ceilometer_disk_device_write_bytes", POLLED, "write"),
    ),
    tc.RESOURCE_NETWORK: (
        ("ceilometer_network_incoming_bytes", "", "in"),
        ("ceilometer_network_outgoing_bytes", "", "out"),
    ),
}
_GAUGES = {
    tc.RESOURCE_MEMORY: "ceilometer_memory_usage",
    "vcpus": "ceilometer_vcpus",
    "memory_alloc": "ceilometer_memory",
}
#: Scale of each resource (CPU time in ns per second -> cores).
_SCALE = {tc.RESOURCE_CPU: 1e9}


def is_instance_id(value):
    """True if ``value`` is an instance UUID (safe in a query)."""
    return bool(_UUID_RE.fullmatch(value or ""))


def is_project_id(value):
    """True if ``value`` looks like a project ID (safe in a query)."""
    return bool(_PROJECT_ID_RE.fullmatch(value or ""))


def _matchers(*parts):
    """``{a,b}`` from the non-empty parts, or ``""``."""
    parts = [p for p in parts if p]
    return "{" + ",".join(parts) + "}" if parts else ""


def _project_matcher(project_ids):
    """``project=~"a|b"`` for the valid IDs, None if there are none."""
    valid = [p for p in project_ids if is_project_id(p)]
    if not valid:
        return None
    return f'project=~"{"|".join(valid)}"'


def _instance_matcher(instance_ids):
    """
    Matcher of the series of the given instances, or None.

    Matches the instance, disk and vNIC forms of ``resource``.
    """
    valid = [i for i in instance_ids if is_instance_id(i)]
    if not valid:
        return None
    return f'resource=~"(.*-)?({"|".join(valid)})(-.+)?"'


def resource_expr(resource, group, window, selector=""):
    """
    PromQL of ``resource`` summed by the ``group`` labels.

    Every series is reduced to one value per meter and resource first (a
    disk, a vNIC, an instance), so duplicated series (several notification
    agents or sg-core replicas) are not counted twice.

    Args:
        group: comma-separated labels, e.g. ``"project"``, or ``""`` for
            the total
        window: rate() window of counters, lookback of gauges
        selector: extra label matcher, e.g. ``project="..."``
    """
    by = f"sum by ({group})" if group else "sum"
    if resource in _GAUGES:
        metric = _GAUGES[resource]
        inner = (
            f"max by (project, resource) (last_over_time("
            f"{metric}{_matchers(POLLED, selector)}[{window}]))"
        )
        inner = (
            _with_instance_label(inner) if INSTANCE_LABEL in group else inner
        )
        return f"{by} ({inner})"
    parts = []
    for metric, polled, field in _COUNTERS[resource]:
        parts.append(
            f'label_replace(max by (project, resource) (rate('
            f"{metric}{_matchers(polled, selector)}[{window}])), "
            f'"meter", "{field}", "", "")'
        )
    inner = " or ".join(parts)
    if INSTANCE_LABEL in group:
        inner = _with_instance_label(inner)
    expr = f"{by} ({inner})"
    scale = _SCALE.get(resource)
    return f"{expr} / {scale:g}" if scale else expr


def _with_instance_label(expr):
    return (
        f'label_replace({expr}, "{INSTANCE_LABEL}", "$2", "resource", '
        f'"{_INSTANCE_FROM_RESOURCE}")'
    )


def _tagged(expr, field):
    return f'label_replace({expr}, "field", "{field}", "", "")'


def get_projects(client):
    """
    Per-project snapshot (one query, one series per project and field).

    See :mod:`grian_ui.top_consumers` for the format.
    """
    window = tc.CURRENT_WINDOW
    exprs = [
        _tagged(
            f"count by (project) (max by (project, resource) (last_over_time("
            f"ceilometer_vcpus{{{POLLED}}}[{window}])))",
            "instances",
        ),
        _tagged(resource_expr(tc.RESOURCE_CPU, "project", window), "cpu"),
        _tagged(resource_expr("vcpus", "project", window), "vcpus"),
        _tagged(
            resource_expr(tc.RESOURCE_MEMORY, "project", window), "memory"
        ),
        _tagged(
            resource_expr("memory_alloc", "project", window), "memory_alloc"
        ),
    ]
    # disk and network: one field per direction
    for resource, fields in (
        (tc.RESOURCE_DISK, ("disk_read", "disk_write")),
        (tc.RESOURCE_NETWORK, ("net_in", "net_out")),
    ):
        for (metric, polled, _meter), field in zip(
            _COUNTERS[resource], fields
        ):
            exprs.append(
                _tagged(
                    f"sum by (project) (max by (project, resource) (rate("
                    f"{metric}{_matchers(polled)}[{window}])))",
                    field,
                )
            )
    projects = {}
    for metric in client.client.query(query=" or ".join(exprs)) or []:
        project_id = metric.labels.get("project")
        field = metric.labels.get("field")
        if not project_id or field not in tc.SNAPSHOT_FIELDS:
            continue
        row = projects.setdefault(
            project_id,
            dict({f: None for f in tc.SNAPSHOT_FIELDS}, id=project_id),
        )
        row[field] = prometheus.number(metric.value)
    for row in projects.values():
        if row["instances"] is not None:
            row["instances"] = int(row["instances"])
    return {"projects": list(projects.values())}


def get_instances(client, resource, project_id=None, limit=tc.TOP_LIMIT):
    """
    The ``limit`` instances using the most of ``resource``, with their
    names and allocation (two queries).

    Args:
        project_id: only this project's instances
    """
    selector = ""
    if project_id is not None:
        selector = _project_matcher([project_id])
        if selector is None:
            return []
    group = f"project, {INSTANCE_LABEL}"
    query = (
        f"topk({int(limit)}, "
        f"{resource_expr(resource, group, tc.CURRENT_WINDOW, selector)})"
    )
    rows = {}
    for metric in client.client.query(query=query) or []:
        instance_id = metric.labels.get(INSTANCE_LABEL)
        if not is_instance_id(instance_id):
            continue
        rows[instance_id] = {
            "id": instance_id,
            "name": None,
            "project_id": metric.labels.get("project"),
            "value": prometheus.number(metric.value),
            "vcpus": None,
            "memory_alloc": None,
        }
    if not rows:
        return []

    # names and allocation of the listed instances only
    matcher = _instance_matcher(rows)
    window = tc.CURRENT_WINDOW
    query = " or ".join(
        _tagged(
            f"max by (resource, resource_name) (last_over_time("
            f"{metric}{_matchers(POLLED, matcher)}[{window}]))",
            field,
        )
        for field, metric in (
            ("vcpus", _GAUGES["vcpus"]),
            ("memory_alloc", _GAUGES["memory_alloc"]),
        )
    )
    for metric in client.client.query(query=query) or []:
        row = rows.get(metric.labels.get("resource"))
        if row is None:
            continue
        field = metric.labels.get("field")
        if field in ("vcpus", "memory_alloc"):
            row[field] = prometheus.number(metric.value)
        name = instance_name(metric.labels.get("resource_name"))
        if name:
            row["name"] = name
    return list(rows.values())


def instance_name(resource_name):
    """``grian-vm`` from ``grian-vm:instance-<id>`` (None if no name)."""
    name, sep, _rest = (resource_name or "").rpartition(":")
    return name if sep and name else None


def get_trend(
    client,
    resource,
    by,
    keys,
    project_id=None,
    range_key=tc.DEFAULT_TREND_RANGE,
    at=None,
):
    """
    ``resource`` over ``range_key`` for the given projects or instances,
    plus the total (one query). Rates use tc.trend_window's window at
    each step.

    Args:
        by: BY_PROJECT or BY_INSTANCE
        keys: project or instance IDs (invalid ones are dropped)
        project_id: totals and instances of this project only
        at: evaluation time (epoch), the end of the chart grid

    Returns:
        dict: ``{"series": {key: [[ts, v], ...]}, "total": [[ts, v], ...]}``
    """
    window, step, rate_window = tc.trend_window(range_key)
    scope = ""
    if project_id is not None:
        scope = _project_matcher([project_id])
        if scope is None:
            return {"series": {}, "total": []}
    if by == tc.BY_INSTANCE:
        label, matcher = INSTANCE_LABEL, _instance_matcher(keys)
    else:
        label, matcher = "project", _project_matcher(keys)
    parts = []
    if matcher:
        selector = ",".join(p for p in (scope, matcher) if p)
        group = f"project, {label}" if label != "project" else "project"
        parts.append(resource_expr(resource, group, rate_window, selector))
    parts.append(
        f'label_replace({resource_expr(resource, "", rate_window, scope)}, '
        f'"{label}", "{tc.TOTAL_KEY}", "", "")'
    )
    query = f"({' or '.join(parts)})[{window}:{step}]"
    result = {"series": {}, "total": []}
    for labels, points in client.query_matrix(query, time=at):
        key = labels.get(label)
        if key == tc.TOTAL_KEY:
            result["total"] = points
        elif key:
            result["series"][key] = points
    return result
