#
# SPDX-License-Identifier: Apache-2.0

import time

from unittest import mock

from django.core.cache import cache
from django.template.loader import get_template
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test import override_settings
from django.urls import reverse

from grian_ui import cache as grian_cache
from grian_ui import network_inventory
from grian_ui.content.network_health import views
from grian_ui.datasource import fake
from grian_ui.datasource import fake_network
from grian_ui_tests.unit import base


VIEWS = "grian_ui.content.network_health.views"

PARTIALS = (
    views.SummaryView,
    views.AgentsView,
    views.TrafficView,
    views.RoutersView,
    views.DropsView,
    views.FloatingIPsView,
)


class _ViewTestCase(base.TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        views._JOINS.clear()
        self.addCleanup(views._JOINS.clear)
        patcher = mock.patch(f"{VIEWS}.get_datasource")
        self.mock_get_ds = patcher.start()
        self.addCleanup(patcher.stop)
        # wrap the fake sources so calls can be asserted
        self.client = mock.MagicMock(wraps=fake.get_client())
        self.mock_get_ds.return_value = self.client
        patcher = mock.patch(f"{VIEWS}.network_inventory.get_inventory")
        self.mock_get_inv = patcher.start()
        self.addCleanup(patcher.stop)
        self.network = mock.MagicMock(
            wraps=network_inventory.FakeNetworkInventory()
        )
        self.mock_get_inv.return_value = self.network

    def render_context(self, view_class, **params):
        request = RequestFactory().get("/", params)
        request.user = mock.MagicMock(services_region="RegionOne")
        view = view_class()
        view.setup(request)
        return view.get_context_data()

    def render(self, view_class, **params):
        context = self.render_context(view_class, **params)
        return context, render_to_string(view_class.template_name, context)


class FormatRateTests(base.TestCase):
    def test_units(self):
        self.assertEqual(views.format_rate(None), "–")
        self.assertEqual(views.format_rate(9.96), "10 B/s")
        self.assertEqual(views.format_rate(1500), "1.5 kB/s")
        self.assertEqual(views.format_rate(1.6e8), "160 MB/s")
        self.assertEqual(views.format_rate(2.5e15), "2500 TB/s")


class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context = self.render_context(views.NetworkHealthIndexView)
        self.assertEqual(context["ranges"], ["6h", "24h"])
        self.assertEqual(context["default_range"], "24h")
        self.assertEqual(context["thresholds"]["quiet_percent"], 10)
        self.assertEqual(context["rate_window"], "10m")
        self.assertEqual(
            context["range_steps"], ["6h: every 5m", "24h: every 15m"]
        )
        self.assertEqual(context["error_messages"], [])
        # the shell queries nothing
        self.mock_get_ds.assert_not_called()
        self.mock_get_inv.assert_not_called()

    def test_template(self):
        source = get_template(
            "admin/network_health/index.html"
        ).template.source
        for name in (
            "summary",
            "agents",
            "traffic",
            "routers",
            "drops",
            "floating_ips",
        ):
            self.assertIn(f"'horizon:admin:network_health:{name}'", source)
        self.assertIn('<details class="panel panel-default nh-section', source)
        self.assertNotIn("<details open", source)
        self.assertIn("GRIAN_PLUGIN['network_health_thresholds']", source)
        self.assertIn("GRIAN_PLUGIN['network_health_rate_window']", source)

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "prometheus",
            "network_health_thresholds": {"quiet_percent": 200},
            "network_health_rate_window": "1d",
        }
    )
    def test_every_configuration_error_is_shown(self):
        context = self.render_context(views.NetworkHealthIndexView)
        self.assertEqual(len(context["error_messages"]), 3)
        self.assertNotIn("thresholds", context)


class PartialRenderingTests(_ViewTestCase):
    def test_every_partial_renders(self):
        for view_class in PARTIALS:
            context, html = self.render(view_class)
            self.assertNotIn("error_message", context, view_class)
            self.assertNotIn("data-nh-nodata", html, view_class)

    def test_data_is_shared_between_partials(self):
        for view_class in PARTIALS:
            self.render_context(view_class)
        self.assertEqual(self.network.get_inventory.call_count, 1)
        self.assertEqual(self.network.get_topology.call_count, 1)
        self.assertEqual(self.network.project_names.call_count, 1)
        self.assertEqual(self.client.get_network_interfaces.call_count, 1)
        self.assertEqual(self.client.get_network_traffic_trend.call_count, 1)

    def test_cache_key_is_namespaced(self):
        request = RequestFactory().get("/")
        request.user = mock.MagicMock(services_region="RegionOne")
        key = views._cache_key(request, "inventory")
        self.assertTrue(key.startswith("grian_ui:network_health:v1:"))


class SummaryViewTests(_ViewTestCase):
    def test_fake_cloud(self):
        context, html = self.render(views.SummaryView)
        summary = context["summary"]
        self.assertEqual(summary["verdict"]["key"], "one_project_quiet")
        self.assertIn("Only ci-runners is affected", html)
        self.assertIn("160 MB/s", html)
        self.assertIn('class="nh-verdict nh-verdict-warning"', html)

    def test_platform_verdict(self):
        inventory = fake_network.fake_inventory()
        for agent in inventory["agents"]:
            if agent["host"] == "net-01":
                agent["alive"] = False
        telemetry = fake_network.fake_interfaces()
        # web-router (net-01) goes quiet as well as ci-router
        for interface in telemetry["interfaces"]:
            if interface["key"].startswith("0000"):
                interface["in_pps"] = 0
        self.network.get_inventory.side_effect = lambda: inventory
        self.client.get_network_interfaces.side_effect = lambda: telemetry
        context, html = self.render(views.SummaryView)
        self.assertEqual(context["summary"]["verdict"]["key"], "platform")
        self.assertIn("Platform problem: gateway net-01 is down", html)
        self.assertIn("ci-runners, web-shop", html)

    def test_ok_on_a_quiet_cloud(self):
        # the DevStack cloud: one router, too little traffic to judge
        self.network.get_inventory.side_effect = lambda: {
            "agents": [fake_network._agent("OVN Neutron agent", "dev")],
            "routers": [
                {
                    "id": "r1",
                    "name": "router1",
                    "project_id": "p",
                    "status": "ACTIVE",
                    "admin_up": True,
                    "gateway_host": "dev",
                }
            ],
            "floating_ips": [],
        }
        self.network.get_topology.side_effect = lambda: {
            "behind": {"r1": ["a"]}
        }
        self.client.get_network_interfaces.side_effect = lambda: {
            "interfaces": [
                {
                    "key": "a",
                    "project": "p",
                    "in_pps": 0.03,
                    "usual_pps": 0.05,
                    "lost_pps": 0.0,
                }
            ],
            "traffic": {"in_bps": 10.0, "out_bps": 3.0},
        }
        context, html = self.render(views.SummaryView)
        self.assertEqual(context["summary"]["verdict"]["key"], "ok")
        self.assertIn("No network problems detected", html)
        self.assertIn("Instance traffic is too low", html)
        self.assertIn("traffic too low to judge", html)

    def test_telemetry_unavailable(self):
        self.client.get_network_interfaces.side_effect = RuntimeError("down")
        context, html = self.render(views.SummaryView)
        self.assertTrue(context["interfaces_unavailable"])
        self.assertFalse(context["summary"]["traffic_checked"])
        self.assertIn("Interface telemetry could not be loaded", html)
        self.assertIn("not checked", html)
        self.assertNotIn("down", " ".join(map(str, context["partial_notes"])))

    def test_projects_unavailable(self):
        self.network.project_names.side_effect = RuntimeError("keystone")
        context, html = self.render(views.SummaryView)
        self.assertIn("showing project IDs", html)
        self.assertEqual(
            context["summary"]["quiet_projects"],
            ["2c3d4e5f6a7b4293a415c6d7e8f90a1b"],
        )

    def test_still_loading(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            context, html = self.render(views.SummaryView)
        self.assertTrue(context["loading"])
        self.assertIn("nh-retry-", html)
        self.assertIn("alert-info", html)

    def test_inventory_error_keeps_the_telemetry(self):
        self.network.get_inventory.side_effect = RuntimeError("secret detail")
        context, html = self.render(views.SummaryView)
        self.assertNotIn("error_message", context)
        self.assertTrue(context["inventory_unavailable"])
        summary = context["summary"]
        self.assertFalse(summary["inventory_checked"])
        self.assertFalse(summary["routers_checked"])
        # the drops are still judged
        self.assertEqual(summary["verdict"]["key"], "packet_loss")
        self.assertIn("The Networking API data could not be loaded", html)
        self.assertNotIn("secret detail", html)
        # no topology without routers to place it on
        self.network.get_topology.assert_not_called()

    def test_inventory_error_on_a_healthy_cloud(self):
        self.network.get_inventory.side_effect = RuntimeError("x")
        telemetry = fake_network.fake_interfaces()
        for interface in telemetry["interfaces"]:
            interface["lost_pps"] = 0.0
        self.client.get_network_interfaces.side_effect = lambda: telemetry
        context, html = self.render(views.SummaryView)
        self.assertIn("No problems detected in the interface telemetry", html)
        self.assertNotIn("No network problems detected", html)

    def test_both_sources_fail(self):
        self.network.get_inventory.side_effect = RuntimeError("secret detail")
        self.client.get_network_interfaces.side_effect = RuntimeError("x")
        with self.assertLogs(VIEWS, "WARNING"):
            context, html = self.render(views.SummaryView)
        self.assertIn("Error loading network data", html)
        self.assertNotIn("secret detail", html)
        self.assertNotIn("loading", context)

    def test_old_copy(self):
        # a copy served unmarked while another request refreshes it
        def old_copy(key, ttl, compute):
            return dict(compute(), stored_at=time.time() - 3600)

        with mock.patch(f"{VIEWS}.cache.get_or_compute", side_effect=old_copy):
            context, html = self.render(views.SummaryView)
        # older than its lifetime plus a refresh interval
        self.assertTrue(context["data_old"])
        self.assertNotIn("data_stale", context)
        self.assertIn("Refreshing: the data shown may be out of date", html)

    def test_not_old_within_lifetime_plus_refresh(self):
        ttl = views.INVENTORY_CACHE_TTL

        def copy(key, ttl_, compute):
            return dict(compute(), stored_at=time.time() - ttl_ - 25)

        with mock.patch(f"{VIEWS}.cache.get_or_compute", side_effect=copy):
            context = self.render_context(views.AgentsView)
        self.assertNotIn("data_old", context)
        self.assertEqual(ttl, 120)

    def test_stale_copy_after_failure(self):
        def failed_copy(key, ttl, compute):
            return dict(compute(), is_stale_copy=True)

        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=failed_copy
        ):
            context, html = self.render(views.SummaryView)
        self.assertTrue(context["data_stale"])
        self.assertIn("Could not be refreshed", html)

    def test_sources_and_refresh_interval(self):
        context, html = self.render(views.SummaryView)
        self.assertTrue(context["inventory_ts"])
        self.assertTrue(context["metrics_ts"])
        self.assertIn("Metrics from", html)
        self.assertIn("Networking API data from", html)
        self.assertIn("(refreshed every 2 minutes)", html)

    def test_join_is_computed_once_per_inputs(self):
        with mock.patch(
            f"{VIEWS}.network_health.summarize",
            wraps=views.network_health.summarize,
        ) as summarize:
            self.render_context(views.SummaryView)
            self.render_context(views.SummaryView)
        self.assertEqual(summarize.call_count, 1)

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "network_health_thresholds": {"bad": 1},
        }
    )
    def test_configuration_error(self):
        with self.assertLogs(VIEWS, "ERROR"):
            context, html = self.render(views.SummaryView)
        self.assertIn("invalid configuration", html)
        self.mock_get_inv.assert_not_called()


class NetworkApiLoadTests(_ViewTestCase):
    def test_lifetimes(self):
        ttls = {}
        real = grian_cache.get_or_compute

        def record(key, ttl, compute):
            ttls[key.split(":")[3]] = ttl
            return real(key, ttl, compute)

        with mock.patch(f"{VIEWS}.cache.get_or_compute", side_effect=record):
            for view_class in PARTIALS:
                self.render_context(view_class)
        self.assertEqual(ttls["inventory"], 120)
        self.assertEqual(ttls["topology"], 300)
        self.assertEqual(ttls["projects"], 300)
        self.assertEqual(ttls["interfaces"], 25)

    def test_refreshes_do_not_call_neutron(self):
        # every section of five page refreshes: one inventory snapshot
        for _refresh in range(5):
            for view_class in PARTIALS:
                self.render_context(view_class)
        self.assertEqual(self.network.get_inventory.call_count, 1)
        self.assertEqual(self.network.get_topology.call_count, 1)
        self.assertEqual(self.mock_get_inv.call_count, 1)

    def test_networking_api_sections_show_the_refresh_interval(self):
        for view_class in (
            views.AgentsView,
            views.RoutersView,
            views.FloatingIPsView,
        ):
            _context, html = self.render(view_class)
            self.assertIn("(refreshed every 2 minutes)", html, view_class)


class AgentsViewTests(_ViewTestCase):
    def test_agents(self):
        context, html = self.render(views.AgentsView)
        self.assertEqual(context["total"], 15)
        self.assertEqual(len(context["rows"]), 15)
        self.assertIn("20 seconds ago", html)
        self.assertIn("OVN Metadata agent: 6 alive", html)

    def test_dead_agent_first(self):
        inventory = fake_network.fake_inventory()
        inventory["agents"][5]["alive"] = False
        self.network.get_inventory.side_effect = lambda: inventory
        context, html = self.render(views.AgentsView)
        self.assertFalse(context["rows"][0]["alive"])
        self.assertIn("not alive", html)
        self.assertIn("1 of 6 not alive", html)

    def test_bounded(self):
        inventory = fake_network.fake_inventory()
        inventory["agents"] *= 3
        self.network.get_inventory.side_effect = lambda: inventory
        context, html = self.render(views.AgentsView)
        self.assertEqual(len(context["rows"]), 25)
        self.assertIn("25 of 45 agents shown", html)

    def test_error(self):
        self.network.get_inventory.side_effect = RuntimeError("x")
        with self.assertLogs(VIEWS, "WARNING"):
            _context, html = self.render(views.AgentsView)
        self.assertIn("from the Networking API", html)


class TrafficViewTests(_ViewTestCase):
    def test_range(self):
        context, html = self.render(views.TrafficView, range="6h")
        self.assertEqual(context["range_key"], "6h")
        self.assertEqual(len(context["chart"]["series"]["in"]), 72)
        self.assertIn("nh-traffic-data-", html)
        self.assertIn("Latest values: inbound", html)

    def test_invalid_range_uses_the_default(self):
        context = self.render_context(views.TrafficView, range="7d")
        self.assertEqual(context["range_key"], "24h")
        self.client.get_network_traffic_trend.assert_called_once()
        self.assertEqual(
            self.client.get_network_traffic_trend.call_args[0][0], "24h"
        )

    def test_no_data(self):
        self.client.get_network_traffic_trend.side_effect = (
            lambda key, at=None: {"in": [], "out": []}
        )
        context, html = self.render(views.TrafficView)
        self.assertFalse(context["has_data"])
        self.assertIn("No instance traffic", html)

    def test_error(self):
        self.client.get_network_traffic_trend.side_effect = RuntimeError("x")
        with self.assertLogs(VIEWS, "WARNING"):
            _context, html = self.render(views.TrafficView)
        self.assertIn("Error loading telemetry data", html)
        self.assertIn("nh.destroyChart('traffic')", html)


class RoutersViewTests(_ViewTestCase):
    def test_routers(self):
        context, html = self.render(views.RoutersView)
        names = [r["name"] for r in context["rows"]]
        self.assertEqual(names[0], "ci-router")
        self.assertEqual(context["rows"][0]["project_name"], "ci-runners")
        self.assertEqual(context["judged_total"], 3)
        self.assertIn("Gone quiet", html)
        self.assertIn("4 of 4", html)
        self.assertIn("Too little traffic to judge", html)

    def test_shared_rows_are_not_modified(self):
        self.render_context(views.RoutersView)
        (rows,) = views._JOINS.values()
        self.assertTrue(rows)
        self.assertTrue(all("project_name" not in r for r in rows))

    def test_without_topology(self):
        self.network.get_topology.side_effect = RuntimeError("x")
        context, html = self.render(views.RoutersView)
        self.assertTrue(context["topology_unavailable"])
        self.assertEqual({r["state"] for r in context["rows"]}, {"unknown"})
        self.assertIn("The ports behind the routers could not be loaded", html)

    def test_no_routers(self):
        inventory = fake_network.fake_inventory()
        inventory["routers"] = []
        self.network.get_inventory.side_effect = lambda: inventory
        _context, html = self.render(views.RoutersView)
        self.assertIn("No routers in the cloud", html)

    def test_bounded(self):
        inventory = fake_network.fake_inventory()
        inventory["routers"] = [
            dict(inventory["routers"][4], id=f"r{i}") for i in range(40)
        ]
        self.network.get_inventory.side_effect = lambda: inventory
        context, html = self.render(views.RoutersView)
        self.assertEqual(len(context["rows"]), 25)
        self.assertIn("25 of 40 routers shown", html)


class DropsViewTests(_ViewTestCase):
    def test_drops(self):
        context, html = self.render(views.DropsView)
        self.assertEqual(context["rows"][0]["project_name"], "data-lake")
        self.assertEqual(context["lossy_total"], 2)
        self.assertEqual(context["interfaces_total"], 21)
        self.assertIn("2 of 21 interfaces", html)

    def test_no_interfaces(self):
        self.client.get_network_interfaces.side_effect = lambda: {
            "interfaces": [],
            "traffic": {"in_bps": None, "out_bps": None},
        }
        _context, html = self.render(views.DropsView)
        self.assertIn("No instance interface reports network metrics", html)


class FloatingIPsViewTests(_ViewTestCase):
    def test_counts(self):
        context, html = self.render(views.FloatingIPsView)
        self.assertEqual(context["counts"]["associated_down"], 1)
        self.assertEqual(context["shares"]["active"], 75.0)
        self.assertIn("width: 75.0%", html)
        self.assertIn("1 associated floating IP is DOWN", html)

    def test_none(self):
        inventory = fake_network.fake_inventory()
        inventory["floating_ips"] = []
        self.network.get_inventory.side_effect = lambda: inventory
        _context, html = self.render(views.FloatingIPsView)
        self.assertIn("No floating IPs in the cloud", html)


class UrlTests(base.TestCase):
    def test_urls(self):
        self.assertEqual(
            reverse("horizon:admin:network_health:floating_ips"),
            "/dashboard/admin/network_health/floating-ips/",
        )
