#
# SPDX-License-Identifier: Apache-2.0

"""
Project network queries for the Prometheus datasource.

Kept apart from :mod:`grian_ui.datasource.prometheus`; its client calls
these functions. Every selector names its metric (Aetos rejects
``{__name__=~...}`` for project users) and holds the current project's
matcher (:func:`grian_ui.project.project_matcher`): Aetos does not filter
the queries of users with an admin role.

The interface metrics have one series per instance network interface
(``resource_name`` is ``<instance name>:tap<port key>``), so they need no
``resource_name=~".+:instance-.+"`` filter: that filter is for the
instance metrics (``ceilometer_vcpus``, ``ceilometer_memory``...), which
also have a notification series per instance.
"""

import re

from grian_ui import project_network
from grian_ui import timeseries
from grian_ui.datasource.prometheus import number


IN_PACKETS = "ceilometer_network_incoming_packets"
IN_BYTES = "ceilometer_network_incoming_bytes"
OUT_BYTES = "ceilometer_network_outgoing_bytes"
#: Dropped and failed packets, both directions. Each metric is named.
LOSS_METRICS = (
    "ceilometer_network_incoming_packets_drop",
    "ceilometer_network_outgoing_packets_drop",
    "ceilometer_network_incoming_packets_error",
    "ceilometer_network_outgoing_packets_error",
)
#: Every metric the panel queries.
METRICS = (IN_PACKETS, IN_BYTES, OUT_BYTES) + LOSS_METRICS
#: One series per interface: ``resource`` is
#: ``instance-<instance id>-<instance id>-tap<port key>``.
INTERFACE_LABELS = "resource, resource_name"
#: libvirt names an instance interface after the first 11 characters of
#: its Neutron port ID: ``tap<port key>`` (OVS, Linux bridge, OVN) or
#: ``vhu<port key>`` (vhost-user).
_PORT_KEY_RE = re.compile(r"(?:tap|vhu)([0-9a-f]{8}-[0-9a-f]{2})$")
_FIELDS = (
    "in_pps",
    "usual_1d",
    "usual_6h",
    "in_bps",
    "out_bps",
    "lost_pps",
)


def interface_key(labels):
    """Port key of an interface series, else its resource ID."""
    resource = labels.get("resource") or ""
    match = _PORT_KEY_RE.search(resource) or _PORT_KEY_RE.search(
        labels.get("resource_name") or ""
    )
    return match.group(1) if match else resource


def instance_name(labels):
    """The instance name in ``resource_name`` (``<name>:tap…``)."""
    name, sep, _interface = (labels.get("resource_name") or "").rpartition(":")
    return name if sep else ""


def _selector(metric, matcher):
    return f"{metric}{{{matcher}}}"


def _per_interface(metric, matcher, window, offset=None):
    # max: several publishers or sg-core replicas may report the same
    # interface; rate() handles counter resets (instance reboots)
    shift = f" offset {offset}" if offset else ""
    return (
        f"max by ({INTERFACE_LABELS}) "
        f"(rate({_selector(metric, matcher)}[{window}]{shift}))"
    )


def _tagged(query, label, value):
    return f'label_replace({query}, "{label}", "{value}", "", "")'


def interfaces_query(matcher):
    """
    One query, one series per interface and field.

    ``in_pps``: inbound packets/s over QUIET_WINDOW; ``usual_1d``: the
    same, USUAL_OFFSET before; ``usual_6h``: its average over the last
    USUAL_FALLBACK (used with less than a day of history); ``in_bps`` /
    ``out_bps``: bytes/s and ``lost_pps``: dropped + failed packets/s
    over RATE_WINDOW. The loss metrics are tagged before the union so
    that an interface missing one of them keeps the others.
    """
    quiet = project_network.QUIET_WINDOW
    window = project_network.RATE_WINDOW
    loss = " or ".join(
        _tagged(_per_interface(metric, matcher, window), "meter", str(i))
        for i, metric in enumerate(LOSS_METRICS)
    )
    usual_6h = (
        f"max by ({INTERFACE_LABELS}) (avg_over_time("
        f"rate({_selector(IN_PACKETS, matcher)}[{quiet}])"
        f"[{project_network.USUAL_FALLBACK}:5m]))"
    )
    parts = (
        (_per_interface(IN_PACKETS, matcher, quiet), "in_pps"),
        (
            _per_interface(
                IN_PACKETS, matcher, quiet, project_network.USUAL_OFFSET
            ),
            "usual_1d",
        ),
        (usual_6h, "usual_6h"),
        (_per_interface(IN_BYTES, matcher, window), "in_bps"),
        (_per_interface(OUT_BYTES, matcher, window), "out_bps"),
        (f"sum by ({INTERFACE_LABELS}) ({loss})", "lost_pps"),
    )
    return " or ".join(_tagged(q, "field", f) for q, f in parts)


def parse_interfaces(results):
    """
    Interface rows from the interfaces query.

    ``usual_pps`` is the rate a day before when there is one, else the
    6-hour average (``usual_basis`` says which).
    """
    interfaces = {}
    for labels, value in results:
        field = labels.get("field")
        key = interface_key(labels)
        if field not in _FIELDS or not key:
            continue
        row = interfaces.setdefault(
            key,
            dict(
                {f: None for f in _FIELDS},
                key=key,
                name=instance_name(labels),
            ),
        )
        row[field] = number(value)
    rows = []
    for row in interfaces.values():
        usual_1d = row.pop("usual_1d")
        usual_6h = row.pop("usual_6h")
        if usual_1d is not None:
            row.update(usual_pps=usual_1d, usual_basis="1d")
        elif usual_6h is not None:
            row.update(usual_pps=usual_6h, usual_basis="6h")
        else:
            row.update(usual_pps=None, usual_basis=None)
        rows.append(row)
    rows.sort(key=lambda r: r["key"])
    return {"interfaces": rows}


def get_interfaces(client, matcher):
    """
    Per-interface rates of the project (one query).

    Args:
        client: a PrometheusTelemetryClient
        matcher: the project matcher, ``project="<id>"``

    Returns:
        dict: see :mod:`grian_ui.project_network`
    """
    results = client.client.query(query=interfaces_query(matcher)) or []
    return parse_interfaces((m.labels, m.value) for m in results)


def _trend_window(range_key):
    # rate() over the larger of the step and RATE_WINDOW, so every point
    # holds at least two samples
    _key, window, step = timeseries.get_range(range_key)
    rate_window = project_network.RATE_WINDOW
    if timeseries.duration_seconds(step) > timeseries.duration_seconds(
        rate_window
    ):
        rate_window = step
    return window, step, rate_window


def traffic_trend_query(matcher, range_key):
    """Project inbound and outbound bytes/s over the range."""
    window, step, rate_window = _trend_window(range_key)
    inner = " or ".join(
        _tagged(
            f"sum({_per_interface(metric, matcher, rate_window)})",
            "direction",
            name,
        )
        for metric, name in ((IN_BYTES, "in"), (OUT_BYTES, "out"))
    )
    return f"({inner})[{window}:{step}]"


def get_traffic_trend(client, matcher, range_key, at=None):
    """
    Project inbound and outbound bytes/s over the range (one query).

    Returns:
        dict: ``{"in": [[ts, bytes/s], ...], "out": [...]}``
    """
    trend = {"in": [], "out": []}
    for labels, points in client.query_matrix(
        traffic_trend_query(matcher, range_key), ndigits=1, time=at
    ):
        direction = labels.get("direction")
        if direction in trend:
            trend[direction] = points
    return trend


def interface_trend_query(matcher, range_key):
    """Inbound packets/s of every interface over the range."""
    window, step, rate_window = _trend_window(range_key)
    inner = _per_interface(IN_PACKETS, matcher, rate_window)
    return f"({inner})[{window}:{step}]"


def get_interface_trend(client, matcher, range_key, at=None):
    """
    Inbound packets/s per interface over the range (one query).

    Returns:
        dict: ``{port key: [[ts, packets/s], ...]}``
    """
    trend = {}
    for labels, points in client.query_matrix(
        interface_trend_query(matcher, range_key), ndigits=3, time=at
    ):
        key = interface_key(labels)
        if key:
            trend[key] = points
    return trend
