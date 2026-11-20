#
# SPDX-License-Identifier: Apache-2.0

"""
Project network inventory from the OpenStack APIs (Neutron, Nova).

Routers, networks, ports, floating IPs and (separately) the network
quota of the current project are read through Horizon's API layer with
the user's token, as the rest of Horizon does, and returned as compact
dicts (see :mod:`grian_ui.project_network` for the formats). The
instance names and states come from the Compute API.

Every listing is filtered to the project: the result is shared by its
members, and an admin token would otherwise list the whole cloud. Neutron
lists every port on the networks a project owns, including the ports of
other projects' instances attached to them. Only the project's own
instance ports are returned; the others are counted per network.

The ``fake`` datasource uses :class:`FakeProjectNetworkInventory`, whose
ports match the fake interface telemetry.
"""

import abc

from grian_ui import config
from grian_ui import project
from grian_ui import project_network


def _get(resource, *names):
    """First attribute (openstacksdk name, then API name) that is set."""
    for name in names:
        value = getattr(resource, name, None)
        if value is not None:
            return value
    return None


def _fixed_ips(port):
    return [
        ip
        for ip in (_get(port, "fixed_ips") or [])
        if isinstance(ip, dict) and ip.get("ip_address")
    ]


def _first_ip(addresses):
    """The first IPv4 address, else the first address, else ""."""
    addresses = [a for a in addresses if a]
    for address in addresses:
        if ":" not in address:
            return address
    return addresses[0] if addresses else ""


def compact_router(router):
    gateway = _get(router, "external_gateway_info") or {}
    return {
        "id": _get(router, "id"),
        "name": _get(router, "name") or "",
        "status": _get(router, "status"),
        "admin_up": bool(_get(router, "is_admin_state_up", "admin_state_up")),
        "gateway": _first_ip(
            [
                ip.get("ip_address")
                for ip in (gateway.get("external_fixed_ips") or [])
                if isinstance(ip, dict)
            ]
        ),
    }


def compact_port(port):
    port_id = _get(port, "id")
    return {
        "id": port_id,
        "key": project_network.port_key(port_id),
        "network_id": _get(port, "network_id"),
        "device_id": _get(port, "device_id"),
        "status": _get(port, "status"),
        "admin_up": bool(_get(port, "is_admin_state_up", "admin_state_up")),
        "ip": _first_ip([ip["ip_address"] for ip in _fixed_ips(port)]),
    }


def compact_floating_ip(fip):
    return {
        "ip": _get(fip, "floating_ip_address", "ip") or "",
        "status": _get(fip, "status"),
        "port_id": _get(fip, "port_id"),
        "fixed_ip": _get(fip, "fixed_ip_address") or "",
    }


def compact_quota(details):
    """Neutron quota details (``/quotas/<id>/details``) to used / limit."""
    quota = {}
    for key, name in (
        ("ports", "port"),
        ("routers", "router"),
        ("floating_ips", "floatingip"),
        ("security_groups", "security_group"),
    ):
        item = (details or {}).get(name)
        if isinstance(item, dict):
            quota[key] = {
                "used": item.get("used") or 0,
                "limit": item.get("limit"),
            }
    return quota


def build_inventory(project_id, routers, networks, subnets, ports, fips):
    """
    Join the listings into the inventory (see grian_ui.project_network).

    Args:
        project_id: the current project
        routers, floating IPs: the project's own (compacted)
        networks, subnets, ports: the project's ones, the ports on its
            networks and the shared networks its ports use (API objects)
    """
    router_ids = {r["id"] for r in routers}
    owned = {
        _get(n, "id")
        for n in networks
        if _get(n, "project_id", "tenant_id") == project_id
    }
    cidrs = {}
    for subnet in subnets:
        cidrs.setdefault(_get(subnet, "network_id"), []).append(
            _get(subnet, "cidr")
        )
    own_ports, attached, others, metadata = [], {}, {}, {}
    for port in ports:
        owner = _get(port, "device_owner") or ""
        network_id = _get(port, "network_id")
        mine = _get(port, "project_id", "tenant_id") == project_id
        if owner in project_network.ROUTER_INTERFACE_OWNERS:
            router_id = _get(port, "device_id")
            if router_id in router_ids:
                attached.setdefault(network_id, set()).add(router_id)
        elif owner == project_network.METADATA_OWNER:
            if _get(port, "status") == "DOWN":
                metadata[network_id] = metadata.get(network_id, 0) + 1
        elif owner.startswith(project_network.COMPUTE_OWNER_PREFIX):
            if mine:
                own_ports.append(compact_port(port))
            elif network_id in owned:
                # only on the project's own networks: never what an admin
                # token shows of shared networks
                others[network_id] = others.get(network_id, 0) + 1
    used = {p["network_id"] for p in own_ports} | set(attached)
    shown = []
    for network in networks:
        network_id = _get(network, "id")
        if network_id not in owned and network_id not in used:
            # shared or external networks the project does not use
            continue
        shown.append(
            {
                "id": network_id,
                "name": _get(network, "name") or "",
                "status": _get(network, "status"),
                "cidr": _first_ip(sorted(cidrs.get(network_id, []))),
                "router_ids": sorted(attached.get(network_id, ())),
                "other_project_ports": others.get(network_id, 0),
                "metadata_ports_down": metadata.get(network_id, 0),
            }
        )
    return {
        "routers": routers,
        "networks": shown,
        "ports": own_ports,
        "floating_ips": fips,
    }


class ProjectNetworkInventory(abc.ABC):
    """The current project's network resources and instances."""

    @abc.abstractmethod
    def get_inventory(self) -> dict:
        """Routers, networks, own instance ports, floating IPs."""

    @abc.abstractmethod
    def get_quota(self) -> dict:
        """``{"quota": {...}}``: the network quota, used and limit."""

    @abc.abstractmethod
    def get_instances(self) -> dict:
        """``{"servers": {id: {"name", "status"}}}``."""


class NeutronProjectNetworkInventory(ProjectNetworkInventory):
    """Inventory read through Horizon's Networking and Compute APIs."""

    def __init__(self, request):
        self.request = request

    def get_inventory(self):
        """
        Routers, networks, subnets, ports and floating IPs of the project.

        The result is shared by every member of the project, so every
        listing is filtered to the project, also for an admin (who would
        otherwise see the whole cloud): the project's ports; the ports on
        the networks it owns (other projects' instances there are
        counted); the networks it owns plus the shared networks its ports
        use; the subnets of those networks. Seven calls at most.
        """
        # imported lazily: unit tests run without horizon
        from openstack_dashboard.api import neutron

        project_id = project.project_id(self.request)
        network = neutron.networkclient(self.request)
        routers = [
            compact_router(r) for r in network.routers(project_id=project_id)
        ]
        networks = list(network.networks(project_id=project_id))
        own_ids = sorted({_get(n, "id") for n in networks})
        ports = {
            _get(p, "id"): p for p in network.ports(project_id=project_id)
        }
        if own_ids:
            # never unfiltered: an empty list would mean every port
            for port in network.ports(network_id=own_ids):
                ports.setdefault(_get(port, "id"), port)
        shared_ids = sorted(
            {_get(p, "network_id") for p in ports.values()} - set(own_ids)
        )
        if shared_ids:
            networks += list(network.networks(id=shared_ids))
        network_ids = sorted({_get(n, "id") for n in networks})
        subnets = (
            list(network.subnets(network_id=network_ids))
            if network_ids
            else []
        )
        return build_inventory(
            project_id,
            routers,
            networks,
            subnets,
            list(ports.values()),
            [
                compact_floating_ip(f)
                for f in network.ips(project_id=project_id)
            ],
        )

    def get_quota(self):
        """The project's network quota details (one call)."""
        from openstack_dashboard.api import neutron

        project_id = project.project_id(self.request)
        return {
            "quota": compact_quota(
                neutron.tenant_quota_detail_get(self.request, project_id)
            )
        }

    def get_instances(self):
        from openstack_dashboard.api import nova

        servers, _more = nova.server_list(self.request)
        return {
            "servers": {
                s.id: {"name": s.name or "", "status": s.status}
                for s in servers
            }
        }


class FakeProjectNetworkInventory(ProjectNetworkInventory):
    """Inventory matching the fake telemetry, for development and tests."""

    def get_inventory(self):
        from grian_ui.datasource import fake_project_network

        return fake_project_network.fake_inventory()

    def get_quota(self):
        from grian_ui.datasource import fake_project_network

        return fake_project_network.fake_quota()

    def get_instances(self):
        from grian_ui.datasource import fake_project_network

        return fake_project_network.fake_instances()


def get_inventory(request):
    """Project network inventory for the configured datasource."""
    if config.get_datasource_type() == config.DATASOURCE_FAKE:
        return FakeProjectNetworkInventory()
    return NeutronProjectNetworkInventory(request)
