#
# SPDX-License-Identifier: Apache-2.0

"""
Compute API (Nova) data of the Project Compute dashboard.

What holds quota starts from Nova: instances in ERROR or SHUTOFF report no
metrics. The servers, the flavors and the absolute limits of the current
project are read through Horizon's API layer (``openstack_dashboard.api``)
with the user's token, as Project > Compute > Instances and Project >
Overview do, and returned as compact dicts (see
:mod:`grian_ui.project_compute` for the format).

The ``fake`` datasource uses :class:`FakeComputeInventory`, whose servers
match the fake telemetry.
"""

import abc
import datetime
import logging

from grian_ui import config


LOG = logging.getLogger(__name__)

#: Longest fault message kept per server.
FAULT_MAX_LENGTH = 300
#: Absolute limits kept (see grian_ui.project_compute.LIMIT_KEYS).
LIMIT_NAMES = (
    "maxTotalInstances",
    "totalInstancesUsed",
    "maxTotalCores",
    "totalCoresUsed",
    "maxTotalRAMSize",
    "totalRAMUsed",
)


def _epoch(value):
    """Nova timestamp (``2026-11-18T09:58:12Z``, naive means UTC) to epoch."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _field(obj, name, default=None):
    """An attribute of a flavor object or a key of a flavor dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def compact_server(server, flavors):
    """
    Keep the fields the dashboard needs from a Horizon Server.

    Args:
        flavors: flavor id -> flavor (objects or dicts with ``name``,
            ``vcpus``, ``ram``, ``disk``)
    """
    # Horizon's wrapper only exposes some attributes; "updated" is on the
    # novaclient resource it wraps
    raw = getattr(server, "_apiresource", server)
    fault = getattr(server, "fault", None)
    message = None
    if isinstance(fault, dict) and fault.get("message"):
        message = str(fault["message"]).strip()[:FAULT_MAX_LENGTH]
    flavor_ref = getattr(server, "flavor", None) or {}
    # microversion 2.47+ embeds the flavor (with original_name); older ones
    # only give its id
    flavor = flavors.get(_field(flavor_ref, "id")) or flavor_ref
    return {
        "id": server.id,
        "name": getattr(server, "name", None),
        "status": getattr(server, "status", None),
        "flavor": _field(flavor, "name") or _field(flavor, "original_name"),
        "vcpus": _field(flavor, "vcpus"),
        "ram": _field(flavor, "ram"),
        "disk": _field(flavor, "disk"),
        "fault": message,
        "updated": _epoch(getattr(raw, "updated", None)),
    }


def compact_limits(limits):
    """The absolute limits the dashboard uses; None for unlimited."""
    result = {}
    for name in LIMIT_NAMES:
        value = limits.get(name)
        if isinstance(value, int | float) and value != float("inf"):
            result[name] = value
        else:
            result[name] = None
    return result


class ComputeInventory(abc.ABC):
    """The current project's servers and compute limits."""

    @abc.abstractmethod
    def read(self) -> dict:
        """
        Servers and limits of the current project.

        Returns:
            dict: ``{"servers": [...], "limits": {...}}``
        """


class NovaComputeInventory(ComputeInventory):
    """Read through Horizon's Nova API wrapper, with the user's token."""

    def __init__(self, request):
        self.request = request

    def read(self):
        # imported lazily: unit tests run without horizon
        from openstack_dashboard.api import nova

        # the current project's servers, as Project > Compute > Instances
        # lists them (same client and filter); limit=-1 makes novaclient
        # follow every page (osapi_max_limit servers per request, 1000 by
        # default)
        client = nova.get_novaclient_with_locked_status(self.request)
        servers = client.servers.list(
            detailed=True,
            search_opts={"project_id": self.request.user.project_id},
            limit=-1,
        )
        flavors = {f.id: f for f in nova.flavor_list(self.request)}
        missing = {
            _field(getattr(s, "flavor", None) or {}, "id") for s in servers
        } - set(flavors)
        for flavor_id in sorted(f for f in missing if f):
            # private or deleted flavors are not in the list
            try:
                flavors[flavor_id] = nova.flavor_get(self.request, flavor_id)
            except Exception as exc:
                LOG.info("Flavor %s is not readable: %s", flavor_id, exc)
        # reserved=True: the counts of Project > Overview
        limits = nova.tenant_absolute_limits(self.request, reserved=True)
        return {
            "servers": [compact_server(s, flavors) for s in servers],
            "limits": compact_limits(limits),
        }


class FakeComputeInventory(ComputeInventory):
    """Servers and limits matching the fake telemetry."""

    def read(self):
        from grian_ui.datasource import fake_project_compute

        return fake_project_compute.get_inventory()


def get_inventory(request):
    """Inventory for the configured datasource."""
    if config.get_datasource_type() == config.DATASOURCE_FAKE:
        return FakeComputeInventory()
    return NovaComputeInventory(request)
