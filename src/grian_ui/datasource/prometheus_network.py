#
# SPDX-License-Identifier: Apache-2.0

"""
Network health queries for the Prometheus datasource.

Kept apart from :mod:`grian_ui.datasource.prometheus`; its client calls
these functions. Every query returns one series per interface (or a
handful of series) whatever the number of routers or projects.
"""

import math
import re

from grian_ui import config
from grian_ui import network_health
from grian_ui import timeseries


IN_PACKETS = "ceilometer_network_incoming_packets"
IN_BYTES = "ceilometer_network_incoming_bytes"
OUT_BYTES = "ceilometer_network_outgoing_bytes"
#: Dropped and failed packets, both directions. Each metric is named:
#: Aetos rejects ``__name__`` regular expressions.
LOSS_METRICS = (
    "ceilometer_network_incoming_packets_drop",
    "ceilometer_network_outgoing_packets_drop",
    "ceilometer_network_incoming_packets_error",
    "ceilometer_network_outgoing_packets_error",
)
#: One series per interface: ``resource`` is
#: ``<instance>-<instance id>-<interface name>``.
INTERFACE_LABELS = "resource, project"
#: libvirt names an instance interface after the first 11 characters of
#: its Neutron port ID: ``tap<port key>`` (OVS, Linux bridge, OVN) or
#: ``vhu<port key>`` (vhost-user).
_PORT_KEY_RE = re.compile(r"(?:tap|vhu)([0-9a-f]{8}-[0-9a-f]{2})$")


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def interface_key(labels):
    """Port key of an interface series, else its resource ID."""
    resource = labels.get("resource") or ""
    match = _PORT_KEY_RE.search(resource) or _PORT_KEY_RE.search(
        labels.get("resource_name") or ""
    )
    return match.group(1) if match else resource


def _per_interface(metric, window, offset=None):
    # max: several publishers or sg-core replicas may report the same
    # interface; rate() handles counter resets (instance reboots)
    shift = f" offset {offset}" if offset else ""
    return f"max by ({INTERFACE_LABELS}) (rate({metric}[{window}]{shift}))"


def _tagged(query, label, value):
    return f'label_replace({query}, "{label}", "{value}", "", "")'


def interfaces_query(window):
    """
    One query: inbound packets/s now and a week before, packets lost/s.

    Each part is tagged with a ``field`` label (range functions drop the
    metric name). The loss metrics are tagged before the union so that an
    interface missing one of them keeps the others.
    """
    loss = " or ".join(
        _tagged(_per_interface(metric, window), "meter", str(i))
        for i, metric in enumerate(LOSS_METRICS)
    )
    return " or ".join(
        (
            _tagged(_per_interface(IN_PACKETS, window), "field", "in_pps"),
            _tagged(
                _per_interface(
                    IN_PACKETS, window, network_health.USUAL_OFFSET
                ),
                "field",
                "usual_pps",
            ),
            _tagged(
                f"sum by ({INTERFACE_LABELS}) ({loss})", "field", "lost_pps"
            ),
        )
    )


def traffic_query(window):
    """Cloud inbound and outbound bytes/s now (two series)."""
    return " or ".join(
        _tagged(f"sum({_per_interface(metric, window)})", "field", field)
        for metric, field in ((IN_BYTES, "in_bps"), (OUT_BYTES, "out_bps"))
    )


def get_interfaces(client):
    """
    Per-interface rates and cloud traffic (two queries).

    Args:
        client: a PrometheusTelemetryClient

    Returns:
        dict: see :mod:`grian_ui.network_health`
    """
    window = config.get_network_health_rate_window()
    interfaces = {}
    for metric in client.client.query(query=interfaces_query(window)) or []:
        labels = metric.labels
        field = labels.get("field")
        if field not in ("in_pps", "usual_pps", "lost_pps"):
            continue
        key = interface_key(labels)
        if not key:
            continue
        row = interfaces.setdefault(
            key,
            {
                "key": key,
                "project": labels.get("project"),
                "in_pps": None,
                "usual_pps": None,
                "lost_pps": None,
            },
        )
        row[field] = _number(metric.value)
    traffic = {"in_bps": None, "out_bps": None}
    for metric in client.client.query(query=traffic_query(window)) or []:
        field = metric.labels.get("field")
        if field in traffic:
            traffic[field] = _number(metric.value)
    return {"interfaces": list(interfaces.values()), "traffic": traffic}


def traffic_trend_query(range_key):
    _key, window, step = timeseries.get_range(range_key)
    rate_window = config.get_network_health_rate_window()
    # rate() over the larger of the step and the configured window, so
    # every point holds at least two samples
    if timeseries.duration_seconds(step) > timeseries.duration_seconds(
        rate_window
    ):
        rate_window = step
    inner = " or ".join(
        _tagged(
            f"sum({_per_interface(metric, rate_window)})", "direction", name
        )
        for metric, name in ((IN_BYTES, "in"), (OUT_BYTES, "out"))
    )
    return f"({inner})[{window}:{step}]"


def get_traffic_trend(client, range_key, at=None):
    """
    Cloud inbound and outbound bytes/s over the range (one query).

    Returns:
        dict: ``{"in": [[ts, bytes/s], ...], "out": [...]}``
    """
    trend = {"in": [], "out": []}
    for labels, points in client.query_matrix(
        traffic_trend_query(range_key), ndigits=1, time=at
    ):
        direction = labels.get("direction")
        if direction in trend:
            trend[direction] = points
    return trend
