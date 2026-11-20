#
# SPDX-License-Identifier: Apache-2.0

import copy

from django.test import override_settings

from grian_ui import config
from grian_ui import project_network as pn
from grian_ui.datasource import fake_project_network as fake
from grian_ui_tests.unit import base


THRESHOLDS = dict(config.DEFAULT_PROJECT_NETWORK_THRESHOLDS)

ROUTER = "r1"
NET_A = "net-a"
NET_B = "net-b"


def _port(n, network=NET_A, status="ACTIVE", device=None):
    return {
        "id": f"0000000{n}-0000-4000-8000-00000000000{n}",
        "key": f"0000000{n}-00",
        "network_id": network,
        "device_id": device or f"vm-{n}",
        "status": status,
        "admin_up": status == "ACTIVE",
        "ip": f"10.0.0.{n}",
    }


def _interface(n, in_pps=5.0, usual=5.0, lost=0.0, basis="1d"):
    return {
        "key": f"0000000{n}-00",
        "name": f"metric-vm-{n}",
        "in_pps": in_pps,
        "usual_pps": usual,
        "usual_basis": basis,
        "in_bps": 100.0 * n,
        "out_bps": 50.0 * n,
        "lost_pps": lost,
    }


def _inventory(ports, routers=None, networks=None, fips=None):
    return {
        "routers": routers
        if routers is not None
        else [
            {
                "id": ROUTER,
                "name": "router1",
                "status": "ACTIVE",
                "admin_up": True,
                "gateway": "172.24.4.67",
            }
        ],
        "networks": networks
        if networks is not None
        else [
            {
                "id": NET_A,
                "name": "private",
                "status": "ACTIVE",
                "cidr": "10.0.0.0/26",
                "router_ids": [ROUTER],
                "other_project_ports": 3,
                "metadata_ports_down": 1,
            },
            {
                "id": NET_B,
                "name": "isolated",
                "status": "ACTIVE",
                "cidr": "10.0.9.0/24",
                "router_ids": [],
                "other_project_ports": 0,
                "metadata_ports_down": 1,
            },
        ],
        "ports": ports,
        "floating_ips": fips or [],
    }


def _instances(*numbers, status="ACTIVE"):
    return {
        "servers": {
            f"vm-{n}": {"name": f"vm-{n}", "status": status} for n in numbers
        }
    }


class ThresholdsTests(base.TestCase):
    def test_defaults_are_the_mockup_values(self):
        thresholds = config.get_project_network_thresholds()
        self.assertEqual(thresholds["quiet_min_pps"], 0.5)
        self.assertEqual(thresholds["quiet_percent"], 10)
        self.assertEqual(thresholds["packet_loss_warning"], 0)
        self.assertEqual(thresholds["packet_loss_critical"], 10)
        self.assertEqual(thresholds["quota_warning"], 75)
        self.assertEqual(thresholds["quota_critical"], 90)

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "project_network_thresholds": {"quiet_min_pps": 2},
        }
    )
    def test_override_through_get_thresholds(self):
        thresholds = config.get_project_network_thresholds()
        self.assertEqual(thresholds["quiet_min_pps"], 2)
        self.assertEqual(thresholds["quiet_percent"], 10)

    def test_invalid_overrides(self):
        for overrides in (
            {"unknown": 1},
            {"quiet_percent": 100},
            {"quiet_min_pps": -1},
            {"packet_loss_warning": 20},
            {"quota_warning": 95},
            "not a dict",
        ):
            with override_settings(
                GRIAN_PLUGIN={
                    "datasource": "fake",
                    "project_network_thresholds": overrides,
                }
            ):
                with self.assertRaises(
                    config.ConfigurationError, msg=overrides
                ):
                    config.get_project_network_thresholds()


class InterfaceTests(base.TestCase):
    def test_port_key(self):
        self.assertEqual(
            pn.port_key("01132844-f877-402d-8400-830891917614"), "01132844-f8"
        )
        self.assertEqual(pn.port_key(None), "")

    def test_quiet_needs_the_floor(self):
        # the demo project: about one packet every 20 s
        self.assertFalse(pn.is_judged(_interface(1, 0.0, 0.042), THRESHOLDS))
        self.assertFalse(pn.is_quiet(_interface(1, 0.0, 0.042), THRESHOLDS))
        self.assertIsNone(
            pn.percent_of_usual(_interface(1, 0.0, 0.042), THRESHOLDS)
        )
        self.assertTrue(pn.is_quiet(_interface(1, 0.04, 0.5), THRESHOLDS))
        self.assertFalse(pn.is_quiet(_interface(1, 0.05, 0.5), THRESHOLDS))
        self.assertEqual(
            pn.percent_of_usual(_interface(1, 2.5, 5.0), THRESHOLDS), 50
        )

    def test_quiet_needs_data(self):
        self.assertFalse(pn.is_quiet(_interface(1, None, 5.0), THRESHOLDS))
        self.assertFalse(pn.is_quiet(_interface(1, 0.0, None), THRESHOLDS))

    def test_loss_level(self):
        self.assertIsNone(pn.loss_level(None, THRESHOLDS))
        self.assertIsNone(pn.loss_level(0.0, THRESHOLDS))
        self.assertEqual(pn.loss_level(0.4, THRESHOLDS), pn.LEVEL_WARNING)
        self.assertEqual(pn.loss_level(10, THRESHOLDS), pn.LEVEL_CRITICAL)


class InterfaceRowsTests(base.TestCase):
    def test_join_and_states(self):
        inventory = _inventory(
            [_port(1), _port(2), _port(3, status="DOWN"), _port(4)]
        )
        telemetry = {
            "interfaces": [
                _interface(1, 0.1, 5.0),
                _interface(2, lost=0.4),
                _interface(3, 0.0, 5.0),
            ]
        }
        rows, not_running = pn.interface_rows(
            inventory, _instances(1, 2, 3, 4), telemetry, THRESHOLDS
        )
        states = {r["instance_name"]: r["state"] for r in rows}
        self.assertEqual(
            states,
            {
                "vm-1": pn.STATE_QUIET,
                "vm-2": pn.STATE_DROPS,
                # a down port explains the quiet interface
                "vm-3": pn.STATE_PORT_DOWN,
                "vm-4": pn.STATE_NO_DATA,
            },
        )
        self.assertEqual(not_running, 0)
        row = next(r for r in rows if r["instance_name"] == "vm-1")
        self.assertEqual(row["network_name"], "private")
        self.assertEqual(row["percent"], 2)
        self.assertEqual(row["severity"], 3)

    def test_ports_of_instances_not_running_are_skipped(self):
        inventory = _inventory([_port(1), _port(2, status="DOWN")])
        instances = _instances(1)
        instances["servers"]["vm-2"] = {"name": "vm-2", "status": "SHUTOFF"}
        rows, not_running = pn.interface_rows(
            inventory, instances, {"interfaces": []}, THRESHOLDS
        )
        self.assertEqual([r["instance_name"] for r in rows], ["vm-1"])
        self.assertEqual(not_running, 1)

    def test_without_instances_every_port_is_a_row(self):
        inventory = _inventory([_port(1), _port(2)])
        telemetry = {"interfaces": [_interface(1)]}
        rows, not_running = pn.interface_rows(
            inventory, None, telemetry, THRESHOLDS
        )
        # the metric's instance name, else the instance ID
        self.assertEqual(
            sorted(r["instance_name"] for r in rows), ["metric-vm-1", "vm-2"]
        )
        self.assertEqual(not_running, 0)

    def test_without_telemetry(self):
        rows, _n = pn.interface_rows(
            _inventory([_port(1)]), _instances(1), None, THRESHOLDS
        )
        self.assertEqual(rows[0]["state"], pn.STATE_NOT_CHECKED)

    def test_without_inventory_rows_come_from_telemetry(self):
        telemetry = {"interfaces": [_interface(1, lost=12)]}
        rows, _n = pn.interface_rows(None, None, telemetry, THRESHOLDS)
        self.assertEqual(rows[0]["instance_name"], "metric-vm-1")
        self.assertEqual(rows[0]["state"], pn.STATE_DROPS)
        self.assertEqual(rows[0]["severity"], 3)

    def test_select_interfaces(self):
        inventory = _inventory([_port(n) for n in range(1, 6)])
        telemetry = {
            "interfaces": [
                _interface(1),
                _interface(2, lost=0.5),
                _interface(3, 0.0, 5.0),
                _interface(4),
                _interface(5),
            ]
        }
        rows, _n = pn.interface_rows(
            inventory, _instances(1, 2, 3, 4, 5), telemetry, THRESHOLDS
        )
        selected = pn.select_interfaces(rows, limit=3)
        # problems first, then by inbound traffic
        self.assertEqual(
            [r["instance_name"] for r in selected], ["vm-3", "vm-2", "vm-5"]
        )


class TopologyTests(base.TestCase):
    def test_routers_then_networks(self):
        inventory = _inventory([_port(1), _port(2), _port(3, network=NET_B)])
        telemetry = {
            "interfaces": [
                _interface(1, 0.1, 5.0, lost=0.5),
                _interface(2, 4.0, 5.0),
                _interface(3),
            ]
        }
        rows, _n = pn.interface_rows(
            inventory, _instances(1, 2, 3), telemetry, THRESHOLDS
        )
        table = pn.topology(inventory, rows)
        self.assertEqual(
            [(t["type"], t.get("name")) for t in table],
            [
                ("router", "router1"),
                ("network", "private"),
                ("no_router", ""),
                ("network", "isolated"),
            ],
        )
        router = table[0]
        self.assertEqual(router["total"], 2)
        self.assertEqual(router["quiet"], 1)
        self.assertEqual(router["percent"], 41)
        self.assertEqual(router["lost_pps"], 0.5)
        self.assertTrue(router["active"])
        self.assertFalse(router["all_quiet"])
        self.assertEqual(router["keys"], ["00000001-00", "00000002-00"])
        self.assertEqual(table[1]["other_project_ports"], 3)
        self.assertEqual(table[2]["total"], 1)

    def test_low_traffic_has_no_percent(self):
        inventory = _inventory([_port(1), _port(2)])
        telemetry = {"interfaces": [_interface(1), _interface(2, 0.03, 0.04)]}
        rows, _n = pn.interface_rows(
            inventory, _instances(1, 2), telemetry, THRESHOLDS
        )
        router = pn.topology(inventory, rows)[0]
        self.assertIsNone(router["percent"])
        self.assertEqual(pn.topology(None, rows), [])

    def test_router_without_instances(self):
        inventory = _inventory([])
        router = pn.topology(inventory, [])[0]
        self.assertEqual(router["total"], 0)
        self.assertIsNone(router["percent"])
        self.assertIsNone(router["lost_pps"])


class VerdictTests(base.TestCase):
    def summarize(self, inventory, telemetry, instances=None, thresholds=None):
        if instances is None:
            instances = _instances(*range(1, 10))
        return pn.summarize(
            inventory, instances, telemetry, thresholds or THRESHOLDS
        )

    def test_router_wide_outage(self):
        inventory = _inventory([_port(1), _port(2), _port(3, network=NET_B)])
        telemetry = {
            "interfaces": [
                _interface(1, 0.01, 5.0),
                _interface(2, 0.0, 6.0),
                _interface(3),
            ]
        }
        summary = self.summarize(inventory, telemetry)
        verdict = summary["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_ROUTER_QUIET)
        self.assertEqual(verdict["level"], pn.LEVEL_CRITICAL)
        self.assertEqual(verdict["router"], "router1")
        self.assertEqual(verdict["total"], 2)
        self.assertEqual(verdict["keys"], ["00000001-00", "00000002-00"])
        self.assertTrue(verdict["router_active"])
        # the isolated network still has traffic
        self.assertTrue(verdict["others_fine"])
        self.assertEqual(summary["quiet"], 2)

    def test_low_traffic_instance_does_not_hide_an_outage(self):
        # vm-3 is idle (below quiet_min_pps): it is never "quiet", but the
        # two instances with enough traffic behind the router went quiet
        inventory = _inventory([_port(1), _port(2), _port(3)])
        telemetry = {
            "interfaces": [
                _interface(1, 0.0, 5.0),
                _interface(2, 0.0, 6.0),
                _interface(3, 0.05, 0.1),
            ]
        }
        summary = self.summarize(inventory, telemetry)
        verdict = summary["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_ROUTER_QUIET)
        self.assertEqual(verdict["total"], 2)
        self.assertEqual(verdict["not_judged"], 1)
        # when it went quiet is found from the judged interfaces only
        self.assertEqual(verdict["keys"], ["00000001-00", "00000002-00"])
        router = summary["topology"][0]
        self.assertTrue(router["all_quiet"])
        self.assertEqual((router["total"], router["judged"]), (3, 2))

    def _two_routers(self, ports):
        routers = [
            {"id": r, "name": f"router{r[1]}", "status": "ACTIVE"}
            for r in ("r1", "r2")
        ]
        networks = [
            {"id": n, "name": n, "status": "ACTIVE", "router_ids": [r]}
            for n, r in ((NET_A, "r1"), (NET_B, "r2"))
        ]
        return _inventory(ports, routers=routers, networks=networks)

    def test_other_router_all_quiet_is_not_fine(self):
        # router1: four quiet instances; router2: two quiet and one idle
        inventory = self._two_routers(
            [_port(n) for n in (1, 2, 3, 4)]
            + [_port(n, network=NET_B) for n in (5, 6, 7)]
        )
        telemetry = {
            "interfaces": [_interface(n, 0.0, 5.0) for n in range(1, 7)]
            + [_interface(7, 0.05, 0.1)]
        }
        verdict = self.summarize(inventory, telemetry)["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_ROUTER_QUIET)
        self.assertEqual(verdict["router"], "router1")
        self.assertFalse(verdict["others_fine"])

    def test_outage_router_is_the_one_with_most_judged(self):
        # router1 has more instances, but only two with enough traffic
        inventory = self._two_routers(
            [_port(n) for n in (1, 2, 3, 4, 5)]
            + [_port(n, network=NET_B) for n in (6, 7, 8)]
        )
        telemetry = {
            "interfaces": [_interface(n, 0.0, 5.0) for n in (1, 2, 6, 7, 8)]
            + [_interface(n, 0.05, 0.1) for n in (3, 4, 5)]
        }
        verdict = self.summarize(inventory, telemetry)["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_ROUTER_QUIET)
        self.assertEqual(verdict["router"], "router2")
        self.assertEqual(verdict["total"], 3)
        self.assertFalse(verdict["others_fine"])

    def test_one_judged_instance_is_not_an_outage(self):
        inventory = _inventory([_port(1), _port(2)])
        telemetry = {
            "interfaces": [_interface(1, 0.0, 5.0), _interface(2, 0.05, 0.1)]
        }
        verdict = self.summarize(inventory, telemetry)["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_INSTANCES_QUIET)

    def test_disabled_router_is_a_warning(self):
        inventory = _inventory([_port(1), _port(2)])
        inventory["routers"][0]["admin_up"] = False
        telemetry = {
            "interfaces": [_interface(1, 0.0, 5.0), _interface(2, 0.0, 5.0)]
        }
        verdict = self.summarize(inventory, telemetry)["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_ROUTER_QUIET)
        self.assertEqual(verdict["level"], pn.LEVEL_WARNING)
        self.assertFalse(verdict["router_active"])
        self.assertFalse(verdict["others_fine"])

    def test_one_instance_alone_is_not_an_outage(self):
        inventory = _inventory([_port(1)])
        telemetry = {"interfaces": [_interface(1, 0.0, 5.0)]}
        verdict = self.summarize(inventory, telemetry)["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_INSTANCES_QUIET)
        self.assertEqual(verdict["instances"], ["vm-1"])
        self.assertFalse(verdict["has_neighbours"])

    def test_one_instance_quiet_neighbours_fine(self):
        inventory = _inventory([_port(1), _port(2), _port(3)])
        telemetry = {
            "interfaces": [
                _interface(1),
                _interface(2, 0.0, 5.0),
                _interface(3),
            ]
        }
        verdict = self.summarize(inventory, telemetry)["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_INSTANCES_QUIET)
        self.assertEqual(verdict["level"], pn.LEVEL_WARNING)
        self.assertEqual(verdict["instances"], ["vm-2"])
        self.assertEqual(verdict["instances_total"], 1)
        self.assertEqual(verdict["network"], "private")
        self.assertTrue(verdict["has_neighbours"])

    def test_low_traffic_neighbours_are_not_fine(self):
        inventory = _inventory([_port(1), _port(2)])
        telemetry = {
            "interfaces": [_interface(1, 0.0, 5.0), _interface(2, 0.0, 0.04)]
        }
        verdict = self.summarize(inventory, telemetry)["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_INSTANCES_QUIET)
        self.assertFalse(verdict["has_neighbours"])

    def test_other_projects_ports_never_look_quiet(self):
        # Neutron lists them on the project's network, but they are not in
        # the inventory's ports (counted per network) and Aetos returns no
        # traffic for them: they neither count nor turn an outage partial
        inventory = _inventory([_port(1), _port(2)])
        telemetry = {
            "interfaces": [_interface(1, 0.0, 5.0), _interface(2, 0.0, 5.0)]
        }
        summary = self.summarize(inventory, telemetry)
        self.assertEqual(summary["verdict"]["key"], pn.VERDICT_ROUTER_QUIET)
        self.assertEqual(summary["verdict"]["total"], 2)
        self.assertEqual(summary["interfaces_total"], 2)
        self.assertEqual(summary["topology"][1]["other_project_ports"], 3)

    def test_resources_not_active(self):
        inventory = _inventory(
            [_port(1), _port(2, status="DOWN")],
            fips=[
                {"ip": "1.1.1.1", "status": "DOWN", "port_id": "p"},
                # DOWN by design
                {"ip": "2.2.2.2", "status": "DOWN", "port_id": None},
            ],
        )
        inventory["routers"][0]["status"] = "ERROR"
        telemetry = {"interfaces": [_interface(1, 0.01, 0.04)]}
        summary = self.summarize(inventory, telemetry)
        verdict = summary["verdict"]
        self.assertEqual(verdict["key"], pn.VERDICT_NOT_ACTIVE)
        self.assertEqual(
            verdict["resources"],
            [
                {"kind": "router", "name": "router1"},
                {"kind": "floating_ip", "name": "1.1.1.1"},
                {"kind": "port", "name": "vm-2"},
            ],
        )
        self.assertEqual(summary["not_active"], 3)

    def test_metadata_ports_are_ignored(self):
        summary = self.summarize(_inventory([_port(1)]), {"interfaces": []})
        self.assertEqual(summary["verdict"]["key"], pn.VERDICT_OK)
        self.assertEqual(summary["metadata_ports_down"], 2)

    def test_packet_loss(self):
        inventory = _inventory([_port(1), _port(2)])
        telemetry = {
            "interfaces": [_interface(1, lost=0.4), _interface(2, lost=12)]
        }
        summary = self.summarize(inventory, telemetry)
        self.assertEqual(summary["verdict"]["key"], pn.VERDICT_PACKET_LOSS)
        self.assertEqual(summary["verdict"]["interfaces_total"], 2)
        self.assertEqual(summary["lossy"], 2)
        self.assertEqual(summary["lossy_critical"], 1)
        self.assertAlmostEqual(summary["lost_pps"], 12.4)

    def test_ok_on_the_demo_project(self):
        summary = self.summarize(
            fake.fake_inventory(),
            {
                "interfaces": [
                    i
                    for i in fake.fake_interfaces()["interfaces"]
                    if i["name"] != "web-01"
                ]
            },
            fake.fake_instances(),
        )
        self.assertEqual(summary["verdict"]["key"], pn.VERDICT_OK)
        # web-01 reports nothing now
        self.assertEqual(summary["low_traffic"], 2)
        self.assertTrue(summary["all_low"])
        self.assertEqual(summary["not_running"], 1)
        self.assertEqual(summary["interfaces_total"], 3)

    def test_sources_missing(self):
        summary = self.summarize(None, {"interfaces": [_interface(1)]})
        self.assertFalse(summary["inventory_checked"])
        self.assertTrue(summary["traffic_checked"])
        self.assertEqual(summary["topology"], [])
        summary = self.summarize(_inventory([_port(1)]), None)
        self.assertTrue(summary["inventory_checked"])
        self.assertFalse(summary["traffic_checked"])
        self.assertIsNone(summary["in_bps"])
        self.assertEqual(summary["verdict"]["key"], pn.VERDICT_OK)

    def test_traffic_totals(self):
        summary = self.summarize(
            _inventory([_port(1)]),
            {"interfaces": [_interface(1), _interface(2)]},
        )
        self.assertEqual(summary["in_bps"], 300.0)
        self.assertEqual(summary["out_bps"], 150.0)

    def test_fake_project(self):
        summary = self.summarize(
            fake.fake_inventory(),
            fake.fake_interfaces(),
            fake.fake_instances(),
        )
        self.assertEqual(summary["verdict"]["key"], pn.VERDICT_PACKET_LOSS)
        self.assertEqual(summary["judged"], 1)


class QuietSinceTests(base.TestCase):
    def test_start_of_the_quiet_stretch(self):
        series = {
            "a": [[0, 5.0], [300, 5.0], [600, 0.1], [900, 0.0]],
            "b": [[0, 5.0], [300, 4.0], [600, 0.0], [900, None]],
        }
        interfaces = {"a": {"usual_pps": 5.0}, "b": {"usual_pps": 5.0}}
        self.assertEqual(
            pn.quiet_since(series, ["a", "b"], interfaces, THRESHOLDS), 600
        )

    def test_points_with_missing_series_do_not_count(self):
        # b has no value at 300: a's 0.0 alone would look quiet, which
        # would put the start at 300 instead of 600
        series = {
            "a": [[0, 5.0], [300, 0.0], [600, 0.0], [900, 0.0]],
            "b": [[0, 5.0], [300, None], [600, 0.0], [900, 0.0]],
        }
        interfaces = {"a": {"usual_pps": 5.0}, "b": {"usual_pps": 5.0}}
        self.assertEqual(
            pn.quiet_since(series, ["a", "b"], interfaces, THRESHOLDS), 600
        )
        # a series missing entirely: no point is complete
        self.assertIsNone(
            pn.quiet_since(
                {"a": series["a"]}, ["a", "b"], interfaces, THRESHOLDS
            )
        )

    def test_incomplete_points_with_traffic_are_not_quiet(self):
        # b has no value at 600, but a alone is above the limit there: the
        # quiet stretch starts at 900, not 300
        series = {
            "a": [[0, 5.0], [300, 0.0], [600, 5.0], [900, 0.0], [1200, 0.0]],
            "b": [[0, 5.0], [300, 0.0], [600, None], [900, 0.0], [1200, 0.0]],
        }
        interfaces = {"a": {"usual_pps": 5.0}, "b": {"usual_pps": 5.0}}
        self.assertEqual(
            pn.quiet_since(series, ["a", "b"], interfaces, THRESHOLDS), 900
        )

    def test_quiet_over_the_whole_range_or_no_data(self):
        interfaces = {"a": {"usual_pps": 5.0}}
        self.assertIsNone(
            pn.quiet_since({"a": [[0, 0.0]]}, ["a"], interfaces, THRESHOLDS)
        )
        self.assertIsNone(pn.quiet_since({}, ["a"], interfaces, THRESHOLDS))
        self.assertIsNone(pn.quiet_since({}, ["a"], {}, THRESHOLDS))


class FloatingIPAndQuotaTests(base.TestCase):
    def test_floating_ips(self):
        inventory = fake.fake_inventory()
        inventory["floating_ips"].append(
            {
                "ip": "172.24.4.9",
                "status": "ERROR",
                "port_id": "x",
                "fixed_ip": "10.0.0.99",
            }
        )
        summary = pn.summarize(
            inventory, fake.fake_instances(), None, THRESHOLDS
        )
        rows, total = pn.floating_ip_rows(inventory, summary["rows"])
        self.assertEqual(total, 3)
        # the problem first; targets by instance name, else fixed IP
        self.assertEqual(
            [(r["ip"], r["target"], r["problem"]) for r in rows],
            [
                ("172.24.4.9", "10.0.0.99", True),
                ("172.24.4.120", "web-01", False),
                ("172.24.4.188", "", False),
            ],
        )
        rows, total = pn.floating_ip_rows(inventory, [], limit=1)
        self.assertEqual((len(rows), total), (1, 3))
        self.assertEqual(pn.floating_ip_rows(None, []), ([], 0))

    def test_quota(self):
        inventory = {
            "quota": {
                "ports": {"used": 45, "limit": 50},
                "routers": {"used": 8, "limit": 10},
                "floating_ips": {"used": 2, "limit": -1},
                "security_groups": {"used": 1, "limit": 0},
            }
        }
        rows = pn.quota_rows(inventory, THRESHOLDS)
        self.assertEqual(
            [(r["key"], r["percent"], r["level"]) for r in rows],
            [
                ("ports", 90, pn.LEVEL_CRITICAL),
                ("routers", 80, pn.LEVEL_WARNING),
                ("floating_ips", None, pn.LEVEL_OK),
                ("security_groups", 100, pn.LEVEL_CRITICAL),
            ],
        )
        self.assertIsNone(rows[2]["limit"])
        self.assertEqual(pn.quota_rows(None, THRESHOLDS), [])


class FakeDataTests(base.TestCase):
    def test_fake_data_matches(self):
        inventory = fake.fake_inventory()
        keys = {p["key"] for p in inventory["ports"]}
        for interface in fake.fake_interfaces()["interfaces"]:
            self.assertIn(interface["key"], keys)
        trend = fake.fake_interface_trend("6h", at=7200)
        self.assertEqual(
            set(trend),
            {i["key"] for i in fake.fake_interfaces()["interfaces"]},
        )
        traffic = fake.fake_traffic_trend("1h", at=7200)
        self.assertEqual(len(traffic["in"]), 60)

    def test_inputs_are_not_modified(self):
        inventory = fake.fake_inventory()
        telemetry = fake.fake_interfaces()
        before = copy.deepcopy((inventory, telemetry))
        pn.summarize(inventory, fake.fake_instances(), telemetry, THRESHOLDS)
        self.assertEqual((inventory, telemetry), before)
