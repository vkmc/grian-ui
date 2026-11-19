#
# SPDX-License-Identifier: Apache-2.0

from django.test import override_settings

from grian_ui import config
from grian_ui import network_health as nh
from grian_ui.datasource import fake_network
from grian_ui_tests.unit import base


TH = dict(config.DEFAULT_NETWORK_HEALTH_THRESHOLDS)
GW = "OVN Controller Gateway agent"


def _agent(host, alive=True, admin_up=True, kind="OVN Controller agent"):
    return {
        "id": f"{kind}@{host}",
        "type": kind,
        "binary": "ovn-controller",
        "host": host,
        "alive": alive,
        "admin_up": admin_up,
        "heartbeat": None,
    }


def _router(rid, project="p1", gateway="net-1", status="ACTIVE", **kw):
    return dict(
        id=rid,
        name=f"router-{rid}",
        project_id=project,
        status=status,
        admin_up=True,
        gateway_host=gateway,
        **kw,
    )


def _iface(key, now, usual, lost=0.0, project="p1"):
    return {
        "key": key,
        "project": project,
        "in_pps": now,
        "usual_pps": usual,
        "lost_pps": lost,
    }


def _inventory(agents=None, routers=None, floating_ips=None):
    return {
        "agents": agents if agents is not None else [_agent("net-1")],
        "routers": routers or [],
        "floating_ips": floating_ips or [],
    }


class ConfigTests(base.TestCase):
    def test_defaults(self):
        self.assertEqual(config.get_network_health_thresholds(), TH)
        self.assertEqual(config.get_network_health_rate_window(), "10m")

    @override_settings(
        GRIAN_PLUGIN={"network_health_thresholds": {"quiet_percent": 25}}
    )
    def test_threshold_override(self):
        self.assertEqual(
            config.get_network_health_thresholds()["quiet_percent"], 25
        )

    def test_invalid_thresholds(self):
        for overrides in (
            {"quiet_percent": 100},
            {"quiet_percent": -1},
            {"packet_loss": "1"},
            {"unknown": 1},
        ):
            with override_settings(
                GRIAN_PLUGIN={"network_health_thresholds": overrides}
            ):
                with self.assertRaises(config.ConfigurationError):
                    config.get_network_health_thresholds()

    def test_rate_window(self):
        for value in ("5m", "1h", "6h", "360m"):
            with override_settings(
                GRIAN_PLUGIN={"network_health_rate_window": value}
            ):
                self.assertEqual(
                    config.get_network_health_rate_window(), value
                )
        for value in ("0m", "7h", "10s", "10m\n", 10, None, "m", "1d"):
            with override_settings(
                GRIAN_PLUGIN={"network_health_rate_window": value}
            ):
                with self.assertRaises(config.ConfigurationError):
                    config.get_network_health_rate_window()


class TopologyTests(base.TestCase):
    def test_port_key(self):
        self.assertEqual(
            nh.port_key("87745239-0013-4a94-9900-6252ab6f294f"), "87745239-00"
        )
        self.assertEqual(nh.port_key(None), "")

    def test_build_topology(self):
        topology = nh.build_topology(
            [
                {"router_id": "r1", "subnets": ["s1"]},
                {"router_id": "r1", "subnets": ["s2"]},
                {"router_id": "r2", "subnets": ["s3"]},
            ],
            [
                # on two subnets of the same router: counted once
                {"key": "a", "subnets": ["s1", "s2"]},
                {"key": "b", "subnets": ["s2"]},
                {"key": "c", "subnets": ["s9"]},
                {"key": "d", "subnets": []},
            ],
        )
        self.assertEqual(topology, {"behind": {"r1": ["a", "b"]}})


class EvaluateRouterTests(base.TestCase):
    def evaluate(self, ifaces, keys=None, router=None, dead=()):
        interfaces = {i["key"]: i for i in ifaces}
        if keys is None:
            keys = list(interfaces)
        return nh.evaluate_router(
            router or _router("r1"), keys, interfaces, TH, dead
        )

    def test_normal(self):
        row = self.evaluate([_iface("a", 9, 10), _iface("b", 11, 10)])
        self.assertEqual(row["state"], nh.ROUTER_OK)
        self.assertEqual((row["judged"], row["quiet"]), (2, 0))
        self.assertEqual(row["percent"], 100)
        self.assertEqual(row["instances"], 2)

    def test_quiet_needs_every_judged_interface(self):
        row = self.evaluate([_iface("a", 0.5, 10), _iface("b", 9, 10)])
        self.assertEqual(row["state"], nh.ROUTER_OK)
        self.assertEqual(row["quiet"], 1)
        row = self.evaluate([_iface("a", 0.5, 10), _iface("b", 0, 10)])
        self.assertEqual(row["state"], nh.ROUTER_QUIET)
        self.assertEqual(row["percent"], 2)

    def test_threshold_is_strict(self):
        # exactly 10% of usual is not quiet
        row = self.evaluate([_iface("a", 1, 10)])
        self.assertEqual(row["state"], nh.ROUTER_OK)

    def test_low_traffic_interfaces_are_not_judged(self):
        row = self.evaluate([_iface("a", 0, 0.4), _iface("b", 9, 10)])
        self.assertEqual((row["reporting"], row["judged"]), (2, 1))
        self.assertEqual(row["state"], nh.ROUTER_OK)
        row = self.evaluate([_iface("a", 0, 0.4)])
        self.assertEqual(row["state"], nh.ROUTER_LOW_TRAFFIC)
        self.assertIsNone(row["percent"])

    def test_usual_missing(self):
        row = self.evaluate([_iface("a", 0, None)])
        self.assertEqual(row["state"], nh.ROUTER_LOW_TRAFFIC)

    def test_interfaces_not_reporting_now_are_never_quiet(self):
        # an instance whose polling stopped is not a quiet interface
        row = self.evaluate([_iface("a", None, 10)])
        self.assertEqual(row["state"], nh.ROUTER_NO_DATA)
        row = self.evaluate([], keys=["a"])
        self.assertEqual(row["state"], nh.ROUTER_NO_DATA)
        self.assertEqual(row["instances"], 1)

    def test_empty_router(self):
        self.assertEqual(self.evaluate([], keys=[])["state"], nh.ROUTER_EMPTY)

    def test_unknown_without_topology_or_telemetry(self):
        row = nh.evaluate_router(_router("r1"), None, {}, TH)
        self.assertEqual(row["state"], nh.ROUTER_UNKNOWN)
        self.assertIsNone(row["instances"])
        row = nh.evaluate_router(_router("r1"), ["a"], None, TH)
        self.assertEqual(row["state"], nh.ROUTER_UNKNOWN)
        # nothing behind it: known to be empty even without telemetry
        row = nh.evaluate_router(_router("r1"), [], None, TH)
        self.assertEqual(row["state"], nh.ROUTER_EMPTY)

    def test_gateway_down(self):
        row = self.evaluate([_iface("a", 9, 10)], dead={"net-1"})
        self.assertTrue(row["gateway_down"])
        self.assertEqual(row["state"], nh.ROUTER_GATEWAY_DOWN)
        # quiet wins
        row = self.evaluate([_iface("a", 0, 10)], dead={"net-1"})
        self.assertEqual(row["state"], nh.ROUTER_QUIET)

    def test_not_active(self):
        row = self.evaluate([], keys=[], router=_router("r1", status="ERROR"))
        self.assertEqual(row["state"], nh.ROUTER_ERROR)
        # ALLOCATING is transient, not a problem
        row = self.evaluate(
            [], keys=[], router=_router("r1", status="ALLOCATING")
        )
        self.assertEqual(row["state"], nh.ROUTER_EMPTY)
        # an administratively disabled router is DOWN by design
        router = _router("r1", status="DOWN")
        router["admin_up"] = False
        row = self.evaluate([], keys=[], router=router)
        self.assertEqual(row["state"], nh.ROUTER_EMPTY)

    def test_does_not_modify_input(self):
        router = _router("r1")
        nh.evaluate_router(router, [], {}, TH)
        self.assertNotIn("state", router)


class SelectRoutersTests(base.TestCase):
    def test_problems_first_and_bounded(self):
        inventory = _inventory(
            routers=[_router(f"r{i:02d}") for i in range(30)]
            + [_router("bad", status="ERROR"), _router("quiet")]
        )
        topology = {"behind": {"quiet": ["q"], "r05": ["n"]}}
        telemetry = {"interfaces": [_iface("q", 0, 10), _iface("n", 10, 10)]}
        rows = nh.evaluate_routers(inventory, topology, telemetry, TH)
        selected = nh.select_routers(rows)
        self.assertEqual(len(selected), nh.ROUTER_LIMIT)
        self.assertEqual(
            [r["id"] for r in selected[:3]], ["quiet", "bad", "r05"]
        )


class AgentRowsTests(base.TestCase):
    def test_dead_first_and_counts(self):
        agents = [_agent(f"c{i:02d}") for i in range(30)] + [
            _agent("net-2", alive=False, kind="Gateway"),
            _agent("c99", admin_up=False),
        ]
        rows, types = nh.agent_rows(agents)
        self.assertEqual(len(rows), nh.AGENT_LIMIT)
        self.assertEqual(rows[0]["host"], "net-2")
        self.assertEqual(rows[1]["host"], "c99")
        self.assertEqual(
            types,
            [
                {"type": "Gateway", "total": 1, "dead": 1},
                {"type": "OVN Controller agent", "total": 31, "dead": 0},
            ],
        )


class AgentDownTests(base.TestCase):
    def test_disabled_agents_are_not_down(self):
        self.assertTrue(nh.is_down(_agent("a", alive=False)))
        self.assertFalse(nh.is_down(_agent("a", alive=False, admin_up=False)))
        self.assertFalse(nh.is_down(_agent("a")))
        agents = [
            _agent("b", alive=False, admin_up=False),
            _agent("a", alive=False),
        ]
        self.assertEqual([a["host"] for a in nh.dead_agents(agents)], ["a"])
        rows, types = nh.agent_rows(agents)
        self.assertEqual([r["host"] for r in rows], ["a", "b"])
        self.assertEqual(types[0]["dead"], 1)
        summary = nh.summarize(
            _inventory(agents=[_agent("b", alive=False, admin_up=False)]),
            {"behind": {}},
            {"interfaces": []},
            TH,
        )
        self.assertEqual(summary["verdict"]["key"], nh.VERDICT_OK)


class DropsByProjectTests(base.TestCase):
    def test_grouping_and_order(self):
        telemetry = {
            "interfaces": [
                _iface("a", 1, 1, lost=2.0, project="p2"),
                _iface("b", 1, 1, lost=0.5, project="p2"),
                _iface("c", 1, 1, project="p2"),
                _iface("d", 1, 1, project="p1"),
                _iface("e", 1, 1, project="p1"),
                _iface("f", 1, 1, lost=None, project=None),
            ]
        }
        rows, total = nh.drops_by_project(telemetry, TH)
        self.assertEqual(total, 3)
        self.assertEqual(
            [(r["project_id"], r["interfaces"], r["lossy"]) for r in rows],
            [("p2", 3, 2), ("p1", 2, 0), ("", 1, 0)],
        )
        self.assertEqual(rows[0]["lost_pps"], 2.5)

    def test_threshold(self):
        telemetry = {"interfaces": [_iface("a", 1, 1, lost=0.5)]}
        rows, _total = nh.drops_by_project(telemetry, dict(TH, packet_loss=1))
        self.assertEqual(rows[0]["lossy"], 0)

    def test_bounded(self):
        telemetry = {
            "interfaces": [
                _iface(str(i), 1, 1, project=f"p{i}") for i in range(20)
            ]
        }
        rows, total = nh.drops_by_project(telemetry, TH)
        self.assertEqual((len(rows), total), (nh.PROJECT_LIMIT, 20))


class FloatingIPTests(base.TestCase):
    def test_counts(self):
        counts = nh.floating_ip_counts(
            [
                {"status": "ACTIVE", "associated": True},
                {"status": "DOWN", "associated": False},
                {"status": "DOWN", "associated": True},
                {"status": "ERROR", "associated": True},
            ]
        )
        self.assertEqual(
            counts,
            {
                "total": 4,
                "active": 1,
                "unassociated": 1,
                "associated_down": 2,
            },
        )


class SummarizeTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.routers = [
            _router("r1", project="p1", gateway="net-1"),
            _router("r2", project="p2", gateway="net-1"),
            _router("r3", project="p3", gateway="net-2"),
        ]
        self.topology = {"behind": {"r1": ["a"], "r2": ["b"], "r3": ["c"]}}

    def telemetry(self, quiet=(), lost=None):
        return {
            "interfaces": [
                _iface(
                    k, 0 if k in quiet else 10, 10, lost=(lost or {}).get(k, 0)
                )
                for k in ("a", "b", "c")
            ],
            "traffic": {"in_bps": 100.0, "out_bps": 50.0},
        }

    def summarize(self, agents=None, telemetry=None, topology="default", **kw):
        return nh.summarize(
            _inventory(agents=agents, routers=self.routers, **kw),
            self.topology if topology == "default" else topology,
            telemetry if telemetry is not None else self.telemetry(),
            TH,
            {"p1": "alpha", "p2": "beta"},
        )

    def test_ok(self):
        summary = self.summarize()
        self.assertEqual(summary["verdict"]["key"], nh.VERDICT_OK)
        self.assertEqual(summary["routers_judged"], 3)
        self.assertEqual(summary["in_bps"], 100.0)
        self.assertTrue(summary["routers_checked"])

    def test_platform(self):
        summary = self.summarize(
            agents=[_agent("net-1", alive=False, kind=GW), _agent("net-2")],
            telemetry=self.telemetry(quiet=("a", "b")),
        )
        self.assertEqual(summary["verdict"]["key"], nh.VERDICT_PLATFORM)
        self.assertEqual(summary["verdict"]["level"], nh.LEVEL_CRITICAL)
        self.assertEqual(summary["dead_gateways"], ["net-1"])
        self.assertEqual(summary["quiet_projects"], ["alpha", "beta"])
        self.assertEqual(summary["dead_agents"], [f"{GW} on net-1"])

    def test_l3_agent_is_a_gateway_agent(self):
        summary = self.summarize(
            agents=[_agent("net-1", alive=False, kind="L3 agent")],
            telemetry=self.telemetry(quiet=("a", "b")),
        )
        self.assertEqual(summary["dead_gateways"], ["net-1"])

    def test_dead_non_gateway_agent_on_a_gateway_host(self):
        agents = [
            _agent("net-1", kind=GW),
            _agent("net-1", alive=False, kind="OVN Metadata agent"),
            _agent("net-1", alive=False, kind="DHCP agent"),
        ]
        rows = nh.evaluate_routers(
            _inventory(agents=agents, routers=self.routers),
            self.topology,
            self.telemetry(),
            TH,
        )
        self.assertEqual([r["gateway_down"] for r in rows], [False] * 3)
        self.assertEqual({r["state"] for r in rows}, {nh.ROUTER_OK})
        summary = self.summarize(
            agents=agents, telemetry=self.telemetry(quiet=("a", "b"))
        )
        # still a platform problem, but no gateway is named down
        self.assertEqual(summary["verdict"]["key"], nh.VERDICT_PLATFORM)
        self.assertEqual(summary["dead_gateways"], [])
        self.assertEqual(summary["agents_dead"], 2)

    def test_many_projects_without_agent_down(self):
        summary = self.summarize(telemetry=self.telemetry(quiet=("a", "c")))
        self.assertEqual(
            summary["verdict"]["key"], nh.VERDICT_MANY_PROJECTS_QUIET
        )
        # unknown project IDs are shown as they are
        self.assertEqual(summary["quiet_projects"], ["alpha", "p3"])

    def test_agents_down(self):
        summary = self.summarize(agents=[_agent("net-9", alive=False)])
        self.assertEqual(summary["verdict"]["key"], nh.VERDICT_AGENTS_DOWN)
        self.assertEqual(summary["agents_dead"], 1)
        self.assertEqual(summary["dead_gateways"], [])

    def test_one_project(self):
        summary = self.summarize(telemetry=self.telemetry(quiet=("a",)))
        self.assertEqual(
            summary["verdict"]["key"], nh.VERDICT_ONE_PROJECT_QUIET
        )
        self.assertEqual(summary["routers_quiet"], 1)

    def test_routers_error(self):
        self.routers[0]["status"] = "ERROR"
        summary = self.summarize()
        self.assertEqual(summary["verdict"]["key"], nh.VERDICT_ROUTERS_ERROR)
        self.assertEqual(summary["not_active_routers"], ["router-r1"])

    def test_packet_loss(self):
        summary = self.summarize(telemetry=self.telemetry(lost={"b": 1.5}))
        self.assertEqual(summary["verdict"]["key"], nh.VERDICT_PACKET_LOSS)
        self.assertEqual(summary["interfaces_lossy"], 1)
        self.assertEqual(summary["lost_pps"], 1.5)
        self.assertEqual(summary["lossy_projects_total"], 1)

    def test_floating_ips_down(self):
        summary = self.summarize(
            floating_ips=[{"status": "DOWN", "associated": True}]
        )
        self.assertEqual(
            summary["verdict"]["key"], nh.VERDICT_FLOATING_IPS_DOWN
        )

    def test_without_telemetry(self):
        summary = nh.summarize(
            _inventory(routers=self.routers), self.topology, None, TH
        )
        self.assertEqual(summary["verdict"]["key"], nh.VERDICT_OK)
        self.assertFalse(summary["traffic_checked"])
        self.assertFalse(summary["routers_checked"])
        self.assertIsNone(summary["in_bps"])

    def test_without_inventory(self):
        summary = nh.summarize(None, None, self.telemetry(lost={"a": 1}), TH)
        self.assertFalse(summary["inventory_checked"])
        self.assertFalse(summary["routers_checked"])
        self.assertEqual(summary["agents_total"], 0)
        self.assertEqual(summary["verdict"]["key"], nh.VERDICT_PACKET_LOSS)

    def test_usual_is_one_week_earlier(self):
        self.assertEqual(nh.USUAL_OFFSET, "7d")

    def test_without_topology(self):
        summary = self.summarize(
            telemetry=self.telemetry(quiet=("a", "b")), topology=None
        )
        # no router can be judged, drops are still checked
        self.assertEqual(summary["routers_quiet"], 0)
        self.assertFalse(summary["routers_checked"])
        self.assertTrue(summary["traffic_checked"])

    def test_names_are_bounded(self):
        agents = [_agent(f"h{i:02d}", alive=False) for i in range(9)]
        summary = self.summarize(agents=agents)
        self.assertEqual(summary["agents_dead"], 9)
        self.assertEqual(len(summary["dead_agents"]), nh.NAME_LIMIT)


class FakeDataTests(base.TestCase):
    def test_fake_cloud_is_consistent(self):
        inventory = fake_network.fake_inventory()
        topology = fake_network.fake_topology()
        telemetry = fake_network.fake_interfaces()
        keys = {i["key"] for i in telemetry["interfaces"]}
        for behind in topology["behind"].values():
            self.assertTrue(set(behind) <= keys)
        summary = nh.summarize(
            inventory, topology, telemetry, TH, fake_network.FAKE_PROJECTS
        )
        self.assertEqual(
            summary["verdict"]["key"], nh.VERDICT_ONE_PROJECT_QUIET
        )
        self.assertEqual(summary["quiet_projects"], ["ci-runners"])
        self.assertEqual(summary["interfaces_lossy"], 2)
        self.assertEqual(summary["floating_ips"]["associated_down"], 1)

    def test_fake_trend(self):
        trend = fake_network.fake_traffic_trend("6h", at=7200 * 100)
        self.assertEqual(len(trend["in"]), 72)
        self.assertEqual(trend["in"][-1][0], 7200 * 100)
