#
# SPDX-License-Identifier: Apache-2.0

"""
Network inventory from the OpenStack APIs (Neutron, Keystone).

Ceilometer has no Neutron service metric, and its interface metrics carry
neither the router nor the subnet of an interface. Agents, routers,
floating IPs and the ports that tie instances to routers are therefore
read from the Networking API through Horizon's API layer, with the
administrator's token, and returned as compact dicts (see
:mod:`grian_ui.network_health` for the formats).

The ``fake`` datasource uses :class:`FakeNetworkInventory`, whose ports
match the fake interface telemetry.
"""

import abc
import datetime

from grian_ui import config
from grian_ui import network_health


#: Port fields read by get_topology (the listing returns only these).
TOPOLOGY_PORT_FIELDS = ("id", "device_owner", "device_id", "fixed_ips")


def _epoch(value):
    """Neutron timestamp (``2026-11-19 14:27:49``, UTC) to epoch."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(
            str(value).replace("Z", "").replace("T", " ")
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _get(resource, *names):
    """First attribute (openstacksdk name, then API name) that is set."""
    for name in names:
        value = getattr(resource, name, None)
        if value is not None:
            return value
    return None


def _subnets(port):
    return sorted(
        {
            ip.get("subnet_id")
            for ip in (_get(port, "fixed_ips") or [])
            if isinstance(ip, dict) and ip.get("subnet_id")
        }
    )


def compact_agent(agent):
    return {
        "id": _get(agent, "id"),
        "type": _get(agent, "agent_type"),
        "binary": _get(agent, "binary"),
        "host": _get(agent, "host"),
        "alive": bool(_get(agent, "is_alive", "alive")),
        "admin_up": bool(_get(agent, "is_admin_state_up", "admin_state_up")),
        "heartbeat": _epoch(
            _get(agent, "last_heartbeat_at", "heartbeat_timestamp")
        ),
    }


def compact_router(router, gateway_hosts):
    router_id = _get(router, "id")
    return {
        "id": router_id,
        "name": _get(router, "name") or "",
        "project_id": _get(router, "project_id", "tenant_id"),
        "status": _get(router, "status"),
        "admin_up": bool(_get(router, "is_admin_state_up", "admin_state_up")),
        "gateway_host": gateway_hosts.get(router_id),
    }


def compact_floating_ip(fip):
    return {
        "status": _get(fip, "status"),
        "associated": bool(_get(fip, "port_id")),
    }


class NetworkInventory(abc.ABC):
    """Network inventory: agents, routers, floating IPs, topology."""

    @abc.abstractmethod
    def get_inventory(self) -> dict:
        """Agents, routers (with gateway host) and floating IPs."""

    @abc.abstractmethod
    def get_topology(self) -> dict:
        """``{"behind": {router_id: [port_key, ...]}}``."""

    @abc.abstractmethod
    def project_names(self) -> dict:
        """Project id -> name."""


class NeutronNetworkInventory(NetworkInventory):
    """Inventory read through Horizon's Networking and Identity APIs."""

    def __init__(self, request):
        self.request = request

    def _client(self):
        # imported lazily: unit tests run without horizon
        from openstack_dashboard.api import neutron

        if getattr(self, "_network", None) is None:
            self._network = neutron.networkclient(self.request)
        return self._network

    def get_inventory(self):
        """
        Four listings: agents, routers, router gateway ports, floating IPs.

        The gateway host of a router is the host its gateway port is bound
        to; with ML2/OVN, Neutron moves the binding to the chassis where
        the gateway is active.
        """
        network = self._client()
        agents = [compact_agent(a) for a in network.agents()]
        gateway_hosts = {}
        for port in network.ports(
            device_owner=network_health.ROUTER_GATEWAY_OWNER
        ):
            host = _get(port, "binding_host_id", "binding:host_id")
            if host:
                gateway_hosts[_get(port, "device_id")] = host
        routers = [compact_router(r, gateway_hosts) for r in network.routers()]
        floating_ips = [compact_floating_ip(f) for f in network.ips()]
        return {
            "agents": agents,
            "routers": routers,
            "floating_ips": floating_ips,
        }

    def get_topology(self):
        """
        Every port, once: router interfaces and instance ports.

        One paginated listing whatever the number of routers. Neutron has
        no prefix filter for ``compute:<zone>`` device owners, so ports are
        filtered here.
        """
        router_ports, instance_ports = [], []
        for port in self._client().ports(fields=TOPOLOGY_PORT_FIELDS):
            owner = _get(port, "device_owner") or ""
            if owner in network_health.ROUTER_INTERFACE_OWNERS:
                router_ports.append(
                    {
                        "router_id": _get(port, "device_id"),
                        "subnets": _subnets(port),
                    }
                )
            elif owner.startswith(network_health.COMPUTE_OWNER_PREFIX):
                instance_ports.append(
                    {
                        "key": network_health.port_key(_get(port, "id")),
                        "subnets": _subnets(port),
                    }
                )
        return network_health.build_topology(router_ports, instance_ports)

    def project_names(self):
        from openstack_dashboard.api import keystone

        projects, _more = keystone.tenant_list(self.request, admin=True)
        return {p.id: p.name for p in projects}


class FakeNetworkInventory(NetworkInventory):
    """Inventory matching the fake telemetry, for development and tests."""

    def get_inventory(self):
        from grian_ui.datasource import fake_network

        return fake_network.fake_inventory()

    def get_topology(self):
        from grian_ui.datasource import fake_network

        return fake_network.fake_topology()

    def project_names(self):
        from grian_ui.datasource import fake_network

        return dict(fake_network.FAKE_PROJECTS)


def get_inventory(request):
    """Network inventory for the configured datasource."""
    if config.get_datasource_type() == config.DATASOURCE_FAKE:
        return FakeNetworkInventory()
    return NeutronNetworkInventory(request)
