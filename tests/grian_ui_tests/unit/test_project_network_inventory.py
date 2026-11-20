#
# SPDX-License-Identifier: Apache-2.0

import sys
import types

from unittest import mock

from django.test import override_settings

from grian_ui import project_network_inventory as inv
from grian_ui.datasource import fake_project_network
from grian_ui_tests.unit import base


PROJECT = "b8753cd7d01b4455b774b46991f36f83"
OTHER = "7bb2692e0d994decacab9a372f166676"


def _resource(**attrs):
    return types.SimpleNamespace(**attrs)


def _port(
    port_id,
    owner,
    network,
    project=PROJECT,
    device="",
    status="ACTIVE",
    ips=("10.0.0.5",),
):
    return _resource(
        id=port_id,
        device_owner=owner,
        device_id=device,
        network_id=network,
        project_id=project,
        status=status,
        is_admin_state_up=status == "ACTIVE",
        fixed_ips=[{"subnet_id": "s", "ip_address": ip} for ip in ips],
    )


def _listings():
    """The demo project as Neutron lists it to a project member."""
    routers = [
        inv.compact_router(
            _resource(
                id="r1",
                name="router1",
                status="ACTIVE",
                is_admin_state_up=True,
                external_gateway_info={
                    "external_fixed_ips": [
                        {"ip_address": "2001:db8::2bd"},
                        {"ip_address": "172.24.4.67"},
                    ]
                },
            )
        )
    ]
    networks = [
        _resource(
            id="private", name="private", status="ACTIVE", project_id=PROJECT
        ),
        _resource(
            id="heat", name="heat-net", status="ACTIVE", project_id=PROJECT
        ),
        _resource(
            id="shared", name="shared", status="ACTIVE", project_id=OTHER
        ),
        _resource(
            id="public", name="public", status="ACTIVE", project_id=OTHER
        ),
        _resource(
            id="used-shared", name="used", status="ACTIVE", project_id=OTHER
        ),
    ]
    subnets = [
        _resource(network_id="private", cidr="fdc4:9af6:720::/64"),
        _resource(network_id="private", cidr="10.0.0.0/26"),
        _resource(network_id="heat", cidr="10.0.5.0/24"),
    ]
    ports = [
        _port(
            "01132844-f877-402d-8400-830891917614",
            "compute:nova",
            "private",
            device="vm-1",
            ips=("fdc4::1aa3", "10.0.0.54"),
        ),
        _port(
            "b4493520-e425-465d-83cb-73a6e680d510",
            "compute:nova",
            "private",
            device="vm-2",
        ),
        # other projects' instances on the project's network
        _port("07f6ff0c", "compute:nova", "private", project=OTHER),
        _port("87745239", "compute:nova", "private", project=OTHER),
        _port("c4697196", "compute:nova", "private", project=OTHER),
        # OVN metadata ports: DOWN by design
        _port("42455d41", "network:distributed", "private", status="DOWN"),
        _port("8d8e2082", "network:distributed", "heat", status="DOWN"),
        _port("b9048caa", "network:router_interface", "private", device="r1"),
        _port("5339d6ef", "network:router_interface", "heat", device="r1"),
        # an interface of another project's router
        _port(
            "x",
            "network:router_interface",
            "shared",
            project=OTHER,
            device="r9",
        ),
        # the project's instance on a shared network
        _port("aaaabbbb-cccc", "compute:nova", "used-shared", device="vm-3"),
    ]
    fips = [
        inv.compact_floating_ip(
            _resource(
                floating_ip_address="172.24.4.120",
                status="ACTIVE",
                port_id="01132844-f877",
                fixed_ip_address="10.0.0.54",
            )
        )
    ]
    return routers, networks, subnets, ports, fips


def _cloud_listings():
    """
    The demo listings plus what only an admin token lists: another
    project's instance on the shared network the project uses, and
    another project's private network with its subnet and instance.
    """
    routers, networks, subnets, ports, fips = _listings()
    networks = networks + [
        _resource(
            id="secret", name="secret-net", status="ACTIVE", project_id=OTHER
        )
    ]
    subnets = subnets + [
        _resource(network_id="secret", cidr="192.168.77.0/24"),
        _resource(network_id="used-shared", cidr="10.9.0.0/24"),
    ]
    ports = ports + [
        _port("dddd", "compute:nova", "used-shared", project=OTHER),
        _port("eeee", "compute:nova", "secret", project=OTHER),
        _port("ffff", "network:distributed", "secret", project=OTHER),
    ]
    return routers, networks, subnets, ports, fips


def _matches(resource, filters):
    for name, wanted in filters.items():
        value = getattr(resource, name, None)
        if isinstance(wanted, list):
            if not wanted:
                # the SDK drops an empty list: no filter at all
                continue
            if value not in wanted:
                return False
        elif value != wanted:
            return False
    return True


class _FakeNetworkClient:
    """Lists like Neutron for an admin token: filters, else everything."""

    def __init__(self, routers, networks, subnets, ports, fips):
        self.listings = {
            "routers": routers,
            "networks": networks,
            "subnets": subnets,
            "ports": ports,
            "ips": fips,
        }
        self.calls = []

    def __getattr__(self, name):
        if name not in self.listings:
            raise AttributeError(name)

        def listing(**filters):
            self.calls.append((name, filters))
            return iter(r for r in self.listings[name] if _matches(r, filters))

        return listing


class CompactTests(base.TestCase):
    def test_router_gateway_prefers_ipv4(self):
        routers = _listings()[0]
        self.assertEqual(
            routers,
            [
                {
                    "id": "r1",
                    "name": "router1",
                    "status": "ACTIVE",
                    "admin_up": True,
                    "gateway": "172.24.4.67",
                }
            ],
        )
        router = inv.compact_router(
            _resource(id="r", status="ACTIVE", admin_state_up=False)
        )
        self.assertEqual(router["gateway"], "")
        self.assertFalse(router["admin_up"])
        self.assertEqual(router["name"], "")

    def test_floating_ip(self):
        self.assertEqual(
            inv.compact_floating_ip(_resource(ip="1.2.3.4", status="DOWN")),
            {
                "ip": "1.2.3.4",
                "status": "DOWN",
                "port_id": None,
                "fixed_ip": "",
            },
        )

    def test_quota(self):
        details = {
            "port": {"used": 7, "limit": 500, "reserved": 0},
            "router": {"used": 1, "limit": 10, "reserved": 0},
            "floatingip": {"used": 0, "limit": 50, "reserved": 0},
            "security_group": {"used": 1, "limit": 10, "reserved": 0},
            "network": {"used": 2, "limit": 100, "reserved": 0},
        }
        self.assertEqual(
            inv.compact_quota(details),
            {
                "ports": {"used": 7, "limit": 500},
                "routers": {"used": 1, "limit": 10},
                "floating_ips": {"used": 0, "limit": 50},
                "security_groups": {"used": 1, "limit": 10},
            },
        )
        self.assertEqual(inv.compact_quota(None), {})


class BuildInventoryTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.inventory = inv.build_inventory(PROJECT, *_listings())

    def test_only_the_projects_own_instance_ports(self):
        ports = self.inventory["ports"]
        self.assertEqual(
            [(p["key"], p["device_id"], p["ip"]) for p in ports],
            [
                ("01132844-f8", "vm-1", "10.0.0.54"),
                ("b4493520-e4", "vm-2", "10.0.0.5"),
                ("aaaabbbb-cc", "vm-3", "10.0.0.5"),
            ],
        )

    def test_networks(self):
        networks = {n["id"]: n for n in self.inventory["networks"]}
        # own networks, and the shared network the project uses
        self.assertEqual(set(networks), {"private", "heat", "used-shared"})
        private = networks["private"]
        self.assertEqual(private["other_project_ports"], 3)
        self.assertEqual(private["metadata_ports_down"], 1)
        self.assertEqual(private["router_ids"], ["r1"])
        self.assertEqual(private["cidr"], "10.0.0.0/26")
        self.assertEqual(networks["heat"]["router_ids"], ["r1"])
        self.assertEqual(networks["heat"]["other_project_ports"], 0)
        # another project's router is not one of ours
        self.assertEqual(networks["used-shared"]["router_ids"], [])
        self.assertEqual(networks["used-shared"]["cidr"], "")

    def test_nothing_of_other_projects_leaks(self):
        # even when given what an admin token lists
        inventory = inv.build_inventory(PROJECT, *_cloud_listings())
        networks = {n["id"]: n for n in inventory["networks"]}
        self.assertEqual(set(networks), {"private", "heat", "used-shared"})
        self.assertEqual(networks["used-shared"]["other_project_ports"], 0)
        self.assertEqual(networks["private"]["other_project_ports"], 3)
        self.assertNotIn("secret", repr(inventory))
        self.assertNotIn("192.168.77", repr(inventory))
        self.assertEqual(len(inventory["ports"]), 3)


class NeutronInventoryTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.network = mock.MagicMock()
        self.neutron = mock.MagicMock()
        self.neutron.networkclient.return_value = self.network
        self.nova = mock.MagicMock()
        api = types.ModuleType("openstack_dashboard.api")
        api.neutron = self.neutron
        api.nova = self.nova
        package = types.ModuleType("openstack_dashboard")
        package.api = api
        patcher = mock.patch.dict(
            sys.modules,
            {"openstack_dashboard": package, "openstack_dashboard.api": api},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request = mock.MagicMock()
        self.request.user.project_id = PROJECT
        self.inventory = inv.NeutronProjectNetworkInventory(self.request)

    def use_cloud(self):
        routers, networks, subnets, ports, _fips = _cloud_listings()
        router = _resource(
            id="r1", name="router1", status="ACTIVE", project_id=PROJECT
        )
        fip = _resource(
            floating_ip_address="172.24.4.120",
            status="ACTIVE",
            project_id=PROJECT,
        )
        other_fip = _resource(
            floating_ip_address="172.24.4.9", status="ACTIVE", project_id=OTHER
        )
        client = _FakeNetworkClient(
            [router], networks, subnets, ports, [fip, other_fip]
        )
        self.neutron.networkclient.return_value = client
        return client

    def test_get_inventory(self):
        client = self.use_cloud()
        inventory = self.inventory.get_inventory()
        # with the user token, every listing filtered to the project
        self.neutron.networkclient.assert_called_once_with(self.request)
        for name, filters in client.calls:
            self.assertTrue(filters, name)
        self.assertEqual(
            client.calls,
            [
                ("routers", {"project_id": PROJECT}),
                ("networks", {"project_id": PROJECT}),
                ("ports", {"project_id": PROJECT}),
                # the other projects' ports only on networks it owns
                ("ports", {"network_id": ["heat", "private"]}),
                # the shared network its own port uses
                ("networks", {"id": ["used-shared"]}),
                (
                    "subnets",
                    {"network_id": ["heat", "private", "used-shared"]},
                ),
                ("ips", {"project_id": PROJECT}),
            ],
        )
        self.neutron.tenant_quota_detail_get.assert_not_called()
        self.assertEqual(len(inventory["ports"]), 3)
        self.assertEqual(inventory["routers"][0]["name"], "router1")
        self.assertEqual(
            [f["ip"] for f in inventory["floating_ips"]], ["172.24.4.120"]
        )
        self.assertNotIn("quota", inventory)

    def test_nothing_of_other_projects_leaks(self):
        # an admin populates the cache shared by every member
        self.use_cloud()
        inventory = self.inventory.get_inventory()
        networks = {n["id"]: n for n in inventory["networks"]}
        self.assertEqual(set(networks), {"private", "heat", "used-shared"})
        self.assertEqual(networks["used-shared"]["other_project_ports"], 0)
        self.assertEqual(networks["used-shared"]["cidr"], "10.9.0.0/24")
        self.assertEqual(networks["private"]["other_project_ports"], 3)
        self.assertEqual(networks["private"]["metadata_ports_down"], 1)
        self.assertNotIn("secret", repr(inventory))
        self.assertNotIn("192.168.77", repr(inventory))
        self.assertNotIn("172.24.4.9'", repr(inventory))

    def test_no_networks_never_lists_unfiltered(self):
        # another project's port: an unfiltered listing would return it
        others = [_port("07f6ff0c", "compute:nova", "net-x", project=OTHER)]
        client = _FakeNetworkClient([], [], [], others, [])
        self.neutron.networkclient.return_value = client
        inventory = self.inventory.get_inventory()
        self.assertEqual(inventory["networks"], [])
        self.assertEqual(inventory["ports"], [])
        for name, filters in client.calls:
            self.assertTrue(filters, name)
            for key, value in filters.items():
                self.assertTrue(value, f"{name}({key}={value!r})")

    def test_get_quota(self):
        self.neutron.tenant_quota_detail_get.return_value = {
            "port": {"used": 7, "limit": 500}
        }
        self.assertEqual(
            self.inventory.get_quota(),
            {"quota": {"ports": {"used": 7, "limit": 500}}},
        )
        self.neutron.tenant_quota_detail_get.assert_called_once_with(
            self.request, PROJECT
        )
        self.neutron.networkclient.assert_not_called()

    def test_invalid_project(self):
        self.request.user.project_id = 'x"}'
        with self.assertRaises(ValueError):
            self.inventory.get_inventory()
        self.neutron.networkclient.assert_not_called()

    def test_get_instances(self):
        self.nova.server_list.return_value = (
            [
                _resource(id="vm-1", name="web", status="ACTIVE"),
                _resource(id="vm-2", name=None, status="SHUTOFF"),
            ],
            False,
        )
        self.assertEqual(
            self.inventory.get_instances(),
            {
                "servers": {
                    "vm-1": {"name": "web", "status": "ACTIVE"},
                    "vm-2": {"name": "", "status": "SHUTOFF"},
                }
            },
        )
        self.nova.server_list.assert_called_once_with(self.request)


class GetInventoryTests(base.TestCase):
    def test_fake(self):
        inventory = inv.get_inventory(mock.MagicMock())
        self.assertIsInstance(inventory, inv.FakeProjectNetworkInventory)
        self.assertEqual(
            inventory.get_inventory(), fake_project_network.fake_inventory()
        )
        self.assertEqual(
            inventory.get_instances(), fake_project_network.fake_instances()
        )
        self.assertEqual(
            inventory.get_quota(), fake_project_network.fake_quota()
        )

    @override_settings(GRIAN_PLUGIN={"datasource": "prometheus"})
    def test_neutron(self):
        request = mock.MagicMock()
        inventory = inv.get_inventory(request)
        self.assertIsInstance(inventory, inv.NeutronProjectNetworkInventory)
        self.assertIs(inventory.request, request)
