#
# SPDX-License-Identifier: Apache-2.0

import sys
import types

from unittest import mock

from django.test import override_settings

from grian_ui import network_inventory
from grian_ui.datasource import fake_network
from grian_ui_tests.unit import base


def _resource(**attrs):
    return types.SimpleNamespace(**attrs)


def _port(port_id, owner, device_id="", subnets=(), host=None):
    return _resource(
        id=port_id,
        device_owner=owner,
        device_id=device_id,
        fixed_ips=[
            {"subnet_id": s, "ip_address": "10.0.0.1"} for s in subnets
        ],
        binding_host_id=host,
    )


class HelpersTests(base.TestCase):
    def test_epoch(self):
        self.assertEqual(network_inventory._epoch("1970-01-01 00:01:00"), 60.0)
        self.assertEqual(
            network_inventory._epoch("1970-01-01T00:01:00Z"), 60.0
        )
        self.assertIsNone(network_inventory._epoch(None))
        self.assertIsNone(network_inventory._epoch("yesterday"))

    def test_compact_agent_sdk_names(self):
        agent = network_inventory.compact_agent(
            _resource(
                id="a1",
                agent_type="OVN Controller Gateway agent",
                binary="ovn-controller",
                host="net-1",
                is_alive=False,
                is_admin_state_up=True,
                last_heartbeat_at="1970-01-01 00:02:00",
            )
        )
        self.assertEqual(
            agent,
            {
                "id": "a1",
                "type": "OVN Controller Gateway agent",
                "binary": "ovn-controller",
                "host": "net-1",
                "alive": False,
                "admin_up": True,
                "heartbeat": 120.0,
            },
        )

    def test_compact_agent_api_names(self):
        agent = network_inventory.compact_agent(
            _resource(
                id="a1",
                agent_type="L3 agent",
                host="net-1",
                alive=True,
                admin_state_up=False,
                heartbeat_timestamp=None,
            )
        )
        self.assertTrue(agent["alive"])
        self.assertFalse(agent["admin_up"])
        self.assertIsNone(agent["heartbeat"])
        self.assertIsNone(agent["binary"])

    def test_compact_router(self):
        router = network_inventory.compact_router(
            _resource(
                id="r1",
                name=None,
                tenant_id="p1",
                status="ACTIVE",
                is_admin_state_up=True,
            ),
            {"r1": "net-1"},
        )
        self.assertEqual(router["name"], "")
        self.assertEqual(router["project_id"], "p1")
        self.assertEqual(router["gateway_host"], "net-1")

    def test_compact_floating_ip(self):
        self.assertEqual(
            network_inventory.compact_floating_ip(
                _resource(status="DOWN", port_id=None)
            ),
            {"status": "DOWN", "associated": False},
        )


class NeutronNetworkInventoryTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.network = mock.MagicMock()
        self.neutron = mock.MagicMock()
        self.neutron.networkclient.return_value = self.network
        self.keystone = mock.MagicMock()
        api = types.ModuleType("openstack_dashboard.api")
        api.neutron = self.neutron
        api.keystone = self.keystone
        package = types.ModuleType("openstack_dashboard")
        package.api = api
        patcher = mock.patch.dict(
            sys.modules,
            {"openstack_dashboard": package, "openstack_dashboard.api": api},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request = mock.MagicMock()
        self.inventory = network_inventory.NeutronNetworkInventory(
            self.request
        )

    def test_get_inventory(self):
        self.network.agents.return_value = [
            _resource(id="a1", agent_type="t", host="h", is_alive=True)
        ]
        self.network.ports.return_value = [
            _port("g1", "network:router_gateway", "r1", host="net-1"),
            _port("g2", "network:router_gateway", "r2", host=""),
        ]
        self.network.routers.return_value = [
            _resource(id="r1", name="one", project_id="p", status="ACTIVE"),
            _resource(id="r2", name="two", project_id="p", status="ACTIVE"),
        ]
        self.network.ips.return_value = [
            _resource(status="ACTIVE", port_id="x")
        ]
        inventory = self.inventory.get_inventory()
        self.network.ports.assert_called_once_with(
            device_owner="network:router_gateway"
        )
        self.assertEqual(
            [r["gateway_host"] for r in inventory["routers"]],
            ["net-1", None],
        )
        self.assertEqual(len(inventory["agents"]), 1)
        self.assertEqual(
            inventory["floating_ips"],
            [{"status": "ACTIVE", "associated": True}],
        )
        # one client per inventory object
        self.neutron.networkclient.assert_called_once_with(self.request)

    def test_get_topology(self):
        self.network.ports.return_value = [
            _port("rp1", "network:router_interface", "r1", ["s1"]),
            _port(
                "rp2", "network:ha_router_replicated_interface", "r2", ["s2"]
            ),
            _port(
                "87745239-0013-4a94-9900-6252ab6f294f",
                "compute:nova",
                subnets=["s1", "s2"],
            ),
            _port("dhcp-port-0000", "network:dhcp", subnets=["s1"]),
            _port("11111111-2222", "compute:az1", subnets=["s9"]),
        ]
        topology = self.inventory.get_topology()
        # only the fields used are listed
        self.network.ports.assert_called_once_with(
            fields=("id", "device_owner", "device_id", "fixed_ips")
        )
        self.assertEqual(
            topology,
            {"behind": {"r1": ["87745239-00"], "r2": ["87745239-00"]}},
        )

    def test_project_names(self):
        self.keystone.tenant_list.return_value = (
            [_resource(id="p1", name="alpha")],
            False,
        )
        self.assertEqual(self.inventory.project_names(), {"p1": "alpha"})
        self.keystone.tenant_list.assert_called_once_with(
            self.request, admin=True
        )


class FakeNetworkInventoryTests(base.TestCase):
    def test_matches_fake_data(self):
        inventory = network_inventory.FakeNetworkInventory()
        self.assertEqual(
            inventory.get_inventory()["routers"][0]["name"], "web-router"
        )
        self.assertEqual(
            inventory.get_topology(), fake_network.fake_topology()
        )
        self.assertEqual(inventory.project_names(), fake_network.FAKE_PROJECTS)
        # callers may modify what they get
        inventory.project_names().clear()
        self.assertTrue(fake_network.FAKE_PROJECTS)


class GetInventoryTests(base.TestCase):
    def test_fake(self):
        self.assertIsInstance(
            network_inventory.get_inventory(None),
            network_inventory.FakeNetworkInventory,
        )

    @override_settings(GRIAN_PLUGIN={"datasource": "prometheus"})
    def test_prometheus(self):
        request = mock.MagicMock()
        inventory = network_inventory.get_inventory(request)
        self.assertIsInstance(
            inventory, network_inventory.NeutronNetworkInventory
        )
        self.assertIs(inventory.request, request)
