#
# SPDX-License-Identifier: Apache-2.0

import re
import time

from unittest import mock

from django.core.cache import cache
from django.template.loader import get_template
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test import override_settings
from django.urls import NoReverseMatch
from django.urls import reverse

from grian_ui import cache as grian_cache
from grian_ui import project_network_inventory
from grian_ui.content.project_network import views
from grian_ui.datasource import fake
from grian_ui.datasource import fake_project_network
from grian_ui_tests.unit import base


VIEWS = "grian_ui.content.project_network.views"
PROJECT = "b8753cd7d01b4455b774b46991f36f83"
MATCHER = f'project="{PROJECT}"'
INSTANCE_METRICS = "/dashboard/project/instance_metrics/"

PARTIALS = (
    views.SummaryView,
    views.TopologyView,
    views.TrafficView,
    views.QuotaView,
    views.FloatingIPsView,
    views.InterfacesView,
)


def _quiet(telemetry, *names):
    """Make the interfaces of these instances busy, then quiet."""
    for interface in telemetry["interfaces"]:
        interface["usual_pps"] = 5.0
        interface["in_pps"] = 0.0 if interface["name"] in names else 5.0
        interface["lost_pps"] = 0.0
    return telemetry


class _ViewTestCase(base.TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch(f"{VIEWS}.get_datasource")
        self.mock_get_ds = patcher.start()
        self.addCleanup(patcher.stop)
        # wrap the fake sources so calls can be asserted
        self.client = mock.MagicMock(wraps=fake.get_client())
        self.mock_get_ds.return_value = self.client
        patcher = mock.patch(
            f"{VIEWS}.project_network_inventory.get_inventory"
        )
        self.mock_get_inv = patcher.start()
        self.addCleanup(patcher.stop)
        self.network = mock.MagicMock(
            wraps=project_network_inventory.FakeProjectNetworkInventory()
        )
        self.mock_get_inv.return_value = self.network
        # the instance metrics page is another panel: absent by default
        patcher = mock.patch(f"{VIEWS}.instance_metrics_url")
        self.mock_im_url = patcher.start()
        self.mock_im_url.return_value = None
        self.addCleanup(patcher.stop)

    def request(self, project_id=PROJECT, **params):
        request = RequestFactory().get("/", params)
        request.user = mock.MagicMock(
            services_region="RegionOne",
            project_id=project_id,
            project_name="demo",
        )
        return request

    def render_context(self, view_class, project_id=PROJECT, **params):
        view = view_class()
        view.setup(self.request(project_id, **params))
        return view.get_context_data()

    def render(self, view_class, **params):
        context = self.render_context(view_class, **params)
        return context, render_to_string(view_class.template_name, context)

    def set_telemetry(self, telemetry):
        self.client.get_project_network_interfaces.side_effect = (
            lambda matcher: telemetry
        )


class FormatRateTests(base.TestCase):
    def test_units(self):
        self.assertEqual(views.format_rate(None), "–")
        self.assertEqual(views.format_rate(5.1), "5 B/s")
        self.assertEqual(views.format_rate(14500), "14.5 kB/s")
        self.assertEqual(views.format_rate(1.6e8), "160 MB/s")


class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context = self.render_context(views.IndexView)
        self.assertEqual(context["ranges"], ["1h", "6h", "24h"])
        self.assertEqual(context["default_range"], "6h")
        self.assertEqual(context["thresholds"]["quiet_min_pps"], 0.5)
        self.assertEqual(context["quiet_window"], "15m")
        self.assertEqual(context["error_messages"], [])
        # the shell queries nothing
        self.mock_get_ds.assert_not_called()
        self.mock_get_inv.assert_not_called()

    def test_template(self):
        source = get_template(
            "project/telemetry_network/index.html"
        ).template.source
        for name in (
            "summary",
            "topology",
            "traffic",
            "quota",
            "floating_ips",
            "interfaces",
        ):
            self.assertIn(
                f"'horizon:project:telemetry_network:{name}'", source
            )
            # resolvable in the Project dashboard namespace
            reverse(f"horizon:project:telemetry_network:{name}")
        self.assertIn("GRIAN_PLUGIN['project_network_thresholds']", source)
        self.assertIn('<details class="panel panel-default', source)
        # browser-local times
        self.assertIn("toLocaleTimeString", source)

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "prometheus",
            "project_network_thresholds": {"quiet_percent": 200},
        }
    )
    def test_every_configuration_error_is_shown(self):
        context = self.render_context(views.IndexView)
        # the thresholds and the missing Keystone URL
        self.assertEqual(len(context["error_messages"]), 2)
        self.assertNotIn("thresholds", context)


class PartialRenderingTests(_ViewTestCase):
    def test_every_partial_renders(self):
        for view_class in PARTIALS:
            context, html = self.render(view_class)
            self.assertNotIn("error_message", context, view_class)
            self.assertNotIn("data-pn-nodata", html, view_class)

    def test_sources_are_shared_between_partials(self):
        for view_class in PARTIALS:
            self.render_context(view_class)
        self.assertEqual(self.network.get_inventory.call_count, 1)
        self.assertEqual(self.network.get_instances.call_count, 1)
        self.assertEqual(
            self.client.get_project_network_interfaces.call_count, 1
        )
        self.assertEqual(
            self.client.get_project_network_traffic_trend.call_count, 1
        )
        self.assertEqual(
            self.client.get_project_network_interface_trend.call_count, 1
        )

    def test_queries_carry_the_project_matcher(self):
        for view_class in PARTIALS:
            self.render_context(view_class)
        self.client.get_project_network_interfaces.assert_called_once_with(
            MATCHER
        )
        args = self.client.get_project_network_traffic_trend.call_args
        self.assertEqual(args[0][:2], (MATCHER, "6h"))
        args = self.client.get_project_network_interface_trend.call_args
        self.assertEqual(args[0][:2], (MATCHER, "6h"))

    def test_cache_key_is_per_project(self):
        key = views._cache_key(self.request(), "inventory")
        self.assertTrue(key.startswith("grian_ui:project_network:v2:"))
        self.assertTrue(key.endswith(f":project:{PROJECT}"))
        self.assertNotEqual(
            key, views._cache_key(self.request("other"), "inventory")
        )

    def test_projects_never_share_data(self):
        self.render_context(views.SummaryView)
        self.render_context(views.SummaryView)
        self.assertEqual(self.network.get_inventory.call_count, 1)
        context = self.render_context(views.SummaryView, project_id="other")
        self.assertNotIn("error_message", context)
        self.assertEqual(self.network.get_inventory.call_count, 2)
        self.client.get_project_network_interfaces.assert_called_with(
            'project="other"'
        )

    def test_no_project(self):
        with self.assertLogs(VIEWS, "ERROR"):
            context, html = self.render(views.SummaryView, project_id="")
        self.assertIn("Error loading network data", html)
        self.network.get_inventory.assert_not_called()

    def test_uid_differs_per_response(self):
        # htmx settles same-id canvases with the old attributes
        first, html = self.render(views.TrafficView)
        second = self.render_context(views.TrafficView)
        self.assertNotEqual(first["uid"], second["uid"])
        self.assertIn(f'id="pn-traffic-canvas-{first["uid"]}"', html)


class SummaryViewTests(_ViewTestCase):
    def test_fake_project(self):
        context, html = self.render(views.SummaryView)
        summary = context["summary"]
        self.assertEqual(summary["verdict"]["key"], "packet_loss")
        self.assertIn("Packet drops or errors on 1 interface", html)
        self.assertIn('class="pn-verdict pn-verdict-warning"', html)
        self.assertIn("drops or errors in the last 10m", html)
        self.assertIn("14.5 kB/s", html)

    def test_router_wide_outage(self):
        telemetry = _quiet(
            fake_project_network.fake_interfaces(),
            "grian-ih-disk",
            "grian-ih-cpu-2",
            "web-01",
        )
        self.set_telemetry(telemetry)
        now = 1_800_000_000

        def trend(matcher, range_key, at=None):
            return {
                i["key"]: [[now - 600, 5.0], [now - 300, 0.0], [now, 0.0]]
                for i in telemetry["interfaces"]
            }

        self.client.get_project_network_interface_trend.side_effect = trend
        context, html = self.render(views.SummaryView)
        verdict = context["summary"]["verdict"]
        self.assertEqual(verdict["key"], "router_quiet")
        self.assertEqual(verdict["since_ts"], str(now - 300))
        self.assertIn("Likely a cloud network problem behind router1", html)
        self.assertIn(f'class="pn-time" data-ts="{now - 300}"', html)
        self.assertIn("contact your cloud operator", html)
        self.assertIn('class="pn-verdict pn-verdict-critical"', html)
        # the trend of the fixed range, whatever the chart range
        args = self.client.get_project_network_interface_trend.call_args
        self.assertEqual(args[0][1], views.SINCE_RANGE)

    def test_router_wide_outage_without_history(self):
        telemetry = _quiet(
            fake_project_network.fake_interfaces(),
            "grian-ih-disk",
            "grian-ih-cpu-2",
            "web-01",
        )
        self.set_telemetry(telemetry)
        self.client.get_project_network_interface_trend.side_effect = (
            RuntimeError("down")
        )
        context, html = self.render(views.SummaryView)
        self.assertIsNone(context["summary"]["verdict"]["since_ts"])
        self.assertIn("stopped receiving traffic.", html)
        self.assertNotIn("pn-time", html)

    def test_disabled_router(self):
        inventory = fake_project_network.fake_inventory()
        inventory["routers"][0]["admin_up"] = False
        self.network.get_inventory.side_effect = lambda: inventory
        self.set_telemetry(
            _quiet(
                fake_project_network.fake_interfaces(),
                "grian-ih-disk",
                "grian-ih-cpu-2",
                "web-01",
            )
        )
        context, html = self.render(views.SummaryView)
        self.assertIn("Router router1 is not active", html)
        self.assertNotIn("contact your cloud operator", html)

    def test_one_instance_quiet(self):
        self.set_telemetry(
            _quiet(fake_project_network.fake_interfaces(), "web-01")
        )
        context, html = self.render(views.SummaryView)
        self.assertEqual(
            context["summary"]["verdict"]["key"], "instances_quiet"
        )
        self.assertIn(
            "web-01 stopped receiving traffic; its neighbours are fine", html
        )
        self.assertIn("Other instances on <strong>private</strong>", html)
        self.assertIn("Check first:", html)

    def test_ok_on_the_demo_project(self):
        telemetry = fake_project_network.fake_interfaces()
        telemetry["interfaces"] = [
            i for i in telemetry["interfaces"] if i["name"] != "web-01"
        ]
        self.set_telemetry(telemetry)
        context, html = self.render(views.SummaryView)
        self.assertEqual(context["summary"]["verdict"]["key"], "ok")
        self.assertIn("No network problems detected in your project", html)
        self.assertIn("Checked 1 router and 3 instance interfaces", html)
        self.assertIn("Traffic on all your interfaces is too low", html)
        self.assertIn("traffic too low to judge", html)

    def test_resources_not_active(self):
        inventory = fake_project_network.fake_inventory()
        inventory["floating_ips"][0]["status"] = "DOWN"
        inventory["ports"][2]["status"] = "DOWN"
        self.network.get_inventory.side_effect = lambda: inventory
        context, html = self.render(views.SummaryView)
        self.assertIn("2 network resources are not active", html)
        self.assertIn("floating IP 172.24.4.120, port of web-01", html)

    def test_telemetry_unavailable(self):
        self.client.get_project_network_interfaces.side_effect = RuntimeError(
            "secret detail"
        )
        context, html = self.render(views.SummaryView)
        self.assertTrue(context["interfaces_unavailable"])
        self.assertFalse(context["summary"]["traffic_checked"])
        self.assertIn("Interface telemetry could not be loaded", html)
        self.assertIn("No problems detected in your network resources", html)
        self.assertIn("not checked", html)
        self.assertNotIn("secret detail", html)

    def test_inventory_unavailable_keeps_the_telemetry(self):
        self.network.get_inventory.side_effect = RuntimeError("secret")
        context, html = self.render(views.SummaryView)
        self.assertNotIn("error_message", context)
        self.assertTrue(context["inventory_unavailable"])
        self.assertEqual(context["summary"]["verdict"]["key"], "packet_loss")
        self.assertIn("The Networking API data could not be loaded", html)
        self.assertNotIn("secret", html)
        # no instance list without ports to join it to
        self.network.get_instances.assert_not_called()

    def test_instances_unavailable(self):
        self.network.get_instances.side_effect = RuntimeError("nova")
        context, html = self.render(views.SummaryView)
        self.assertTrue(context["instances_unavailable"])
        self.assertIn("The instance list could not be loaded", html)
        # the stopped instance's DOWN port is no longer filtered out
        self.assertEqual(context["summary"]["verdict"]["key"], "not_active")

    def test_both_sources_fail(self):
        self.network.get_inventory.side_effect = RuntimeError("secret")
        self.client.get_project_network_interfaces.side_effect = RuntimeError(
            "x"
        )
        with self.assertLogs(VIEWS, "WARNING"):
            context, html = self.render(views.SummaryView)
        self.assertIn("Error loading network data", html)
        self.assertIn("alert-danger", html)
        self.assertNotIn("secret", html)
        self.assertNotIn("loading", context)

    def test_still_loading(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            context, html = self.render(views.SummaryView)
        self.assertTrue(context["loading"])
        self.assertIn(f'id="pn-retry-{context["uid"]}"', html)
        self.assertIn("alert-info", html)
        self.assertIn("Still loading", html)

    def test_one_source_still_loading(self):
        real = grian_cache.get_or_compute

        def busy_inventory(key, ttl, compute):
            if ":inventory:" in key:
                raise grian_cache.StillComputing("busy")
            return real(key, ttl, compute)

        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=busy_inventory
        ):
            context, html = self.render(views.SummaryView)
        self.assertTrue(context["loading"])
        self.assertNotIn("error_message", context)
        self.assertIn("Loading your network resources", html)
        self.assertIn("pn-retry-", html)

    def test_old_copy(self):
        def old_copy(key, ttl, compute):
            return dict(compute(), stored_at=time.time() - 3600)

        with mock.patch(f"{VIEWS}.cache.get_or_compute", side_effect=old_copy):
            context, html = self.render(views.SummaryView)
        self.assertTrue(context["data_old"])
        self.assertIn("Refreshing: the data shown may be out of date", html)

    def test_stale_copy_after_failure(self):
        def failed_copy(key, ttl, compute):
            return dict(compute(), is_stale_copy=True)

        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=failed_copy
        ):
            context, html = self.render(views.SummaryView)
        self.assertTrue(context["data_stale"])
        self.assertIn("Could not be refreshed", html)

    def test_sources_and_times(self):
        context, html = self.render(views.SummaryView)
        self.assertTrue(context["inventory_ts"])
        self.assertTrue(context["metrics_ts"])
        self.assertIn(
            f'Metrics from <span class="pn-clock" data-ts="'
            f'{context["metrics_ts"]}">',
            html,
        )
        self.assertIn("(refreshed every 60 s)", html)

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "project_network_thresholds": {"bad": 1},
        }
    )
    def test_configuration_error(self):
        with self.assertLogs(VIEWS, "ERROR"):
            context, html = self.render(views.SummaryView)
        self.assertIn("invalid configuration", html)
        self.mock_get_inv.assert_not_called()


class TopologyViewTests(_ViewTestCase):
    def test_rows(self):
        context, html = self.render(views.TopologyView)
        self.assertEqual(
            [(r["type"], r["name"]) for r in context["rows"]],
            [
                ("router", "router1"),
                ("network", "heat-net"),
                ("network", "private"),
            ],
        )
        self.assertIn("gateway 172.24.4.67", html)
        self.assertIn("3 ports of other projects", html)
        self.assertIn("2 OVN metadata ports are DOWN by design", html)
        self.assertIn("too little traffic", html)
        self.assertIn("0 of 3", html)
        self.assertIn("the same 15 minutes yesterday", html)

    def test_outage_row(self):
        self.set_telemetry(
            _quiet(
                fake_project_network.fake_interfaces(),
                "grian-ih-disk",
                "grian-ih-cpu-2",
                "web-01",
            )
        )
        context, html = self.render(views.TopologyView)
        self.assertIn('<tr class="danger">', html)
        self.assertIn("3 of 3", html)
        self.assertIn('pn-ratio pn-crit', html)

    def test_less_than_a_day_of_history(self):
        telemetry = fake_project_network.fake_interfaces()
        for interface in telemetry["interfaces"]:
            interface["usual_basis"] = "6h"
        self.set_telemetry(telemetry)
        context, html = self.render(views.TopologyView)
        self.assertIn("less than a day of history", html)

    def test_empty(self):
        self.network.get_inventory.side_effect = lambda: {
            "routers": [],
            "networks": [],
            "ports": [],
            "floating_ips": [],
        }
        context, html = self.render(views.TopologyView)
        self.assertIn("No routers or networks in your project", html)

    def test_error(self):
        self.network.get_inventory.side_effect = RuntimeError("secret")
        with self.assertLogs(VIEWS, "WARNING"):
            context, html = self.render(views.TopologyView)
        self.assertIn("Error loading your routers and networks", html)
        self.assertNotIn("secret", html)


class TrafficViewTests(_ViewTestCase):
    def test_chart(self):
        context, html = self.render(views.TrafficView, range="1h")
        self.assertEqual(context["range_key"], "1h")
        self.assertTrue(context["has_data"])
        self.assertEqual(len(context["chart"]["series"]["in"]), 60)
        self.assertIn(f'id="pn-traffic-data-{context["uid"]}"', html)
        self.assertIn("pn.formatTime", html)

    def test_invalid_range(self):
        context = self.render_context(views.TrafficView, range="12h")
        self.assertEqual(context["range_key"], "6h")

    def test_no_data(self):
        self.client.get_project_network_traffic_trend.side_effect = (
            lambda matcher, range_key, at=None: {"in": [], "out": []}
        )
        context, html = self.render(views.TrafficView)
        self.assertIn("No traffic from your instances", html)
        self.assertIn("pn.destroyChart('traffic')", html)

    def test_error(self):
        self.client.get_project_network_traffic_trend.side_effect = (
            RuntimeError("x")
        )
        with self.assertLogs(VIEWS, "WARNING"):
            context, html = self.render(views.TrafficView)
        self.assertIn("Error loading telemetry data", html)
        self.assertIn("pn.destroyChart('traffic')", html)


class QuotaViewTests(_ViewTestCase):
    def test_rows(self):
        context, html = self.render(views.QuotaView)
        self.assertEqual(
            [r["key"] for r in context["rows"]],
            ["ports", "routers", "floating_ips", "security_groups"],
        )
        self.assertIn("9 / 50", html)
        self.assertIn("pn-quota-ok", html)

    def test_error(self):
        self.network.get_quota.side_effect = RuntimeError("x")
        with self.assertLogs(VIEWS, "WARNING"):
            context, html = self.render(views.QuotaView)
        self.assertIn("Error loading the network quota", html)

    def test_quota_failure_keeps_the_inventory(self):
        # its own cache entry: the topology, floating IPs and the verdict
        # still have the Networking API data
        self.network.get_quota.side_effect = RuntimeError("x")
        with self.assertLogs(VIEWS, "WARNING"):
            context, html = self.render(views.QuotaView)
        self.assertIn("Error loading the network quota", html)
        context, html = self.render(views.TopologyView)
        self.assertNotIn("error_message", context)
        self.assertIn("router1", html)
        context = self.render_context(views.SummaryView)
        self.assertNotIn("inventory_unavailable", context)
        self.assertTrue(context["summary"]["inventory_checked"])
        context = self.render_context(views.FloatingIPsView)
        self.assertEqual(context["total"], 2)
        self.network.get_inventory.assert_called_once_with()

    def test_inventory_failure_keeps_the_quota(self):
        self.network.get_inventory.side_effect = RuntimeError("x")
        context, html = self.render(views.QuotaView)
        self.assertNotIn("error_message", context)
        self.assertIn("9 / 50", html)
        self.network.get_inventory.assert_not_called()


class FloatingIPsViewTests(_ViewTestCase):
    def test_rows(self):
        context, html = self.render(views.FloatingIPsView)
        self.assertEqual(context["total"], 2)
        self.assertIn("→ web-01", html)
        self.assertIn("not associated", html)
        self.assertIn("DOWN by design", html)
        # no telemetry needed
        self.client.get_project_network_interfaces.assert_not_called()

    def test_empty(self):
        inventory = fake_project_network.fake_inventory()
        inventory["floating_ips"] = []
        self.network.get_inventory.side_effect = lambda: inventory
        context, html = self.render(views.FloatingIPsView)
        self.assertIn("No floating IPs", html)

    def test_error(self):
        self.network.get_inventory.side_effect = RuntimeError("x")
        with self.assertLogs(VIEWS, "WARNING"):
            context, html = self.render(views.FloatingIPsView)
        self.assertIn("Error loading the floating IPs", html)


class InterfacesViewTests(_ViewTestCase):
    def test_rows(self):
        context, html = self.render(views.InterfacesView)
        rows = context["rows"]
        # problems first
        self.assertEqual(
            [r["instance_name"] for r in rows],
            ["web-01", "grian-ih-cpu-2", "grian-ih-disk"],
        )
        self.assertEqual(rows[0]["state"], "drops")
        self.assertIn("tap6f0e9a2b-11", html)
        self.assertIn("0.40 pkt/s", html)
        self.assertIn("too little traffic", html)
        self.assertIn(
            "1 interface of instances that are not running is not checked",
            html,
        )
        # a sparkline per row, ids unique per response
        self.assertEqual(len(context["sparks"]), 3)
        self.assertIn(f'id="pn-spark-{context["uid"]}-s0"', html)
        self.assertIn(f'id="pn-sparks-{context["uid"]}"', html)

    def test_range(self):
        context = self.render_context(views.InterfacesView, range="24h")
        self.assertEqual(len(context["sparks"]["s0"]["points"]), 96)

    def test_trend_unavailable(self):
        self.client.get_project_network_interface_trend.side_effect = (
            RuntimeError("x")
        )
        context, html = self.render(views.InterfacesView)
        self.assertEqual(len(context["rows"]), 3)
        self.assertEqual(context["sparks"], {})
        self.assertIn("The traffic history could not be loaded", html)

    def test_limit(self):
        inventory = fake_project_network.fake_inventory()
        template = inventory["ports"][0]
        inventory["ports"] = [
            dict(template, id=f"{n:08x}-0000", key=f"{n:08x}-00")
            for n in range(30)
        ]
        self.network.get_inventory.side_effect = lambda: inventory
        context, html = self.render(views.InterfacesView)
        self.assertEqual(len(context["rows"]), 25)
        self.assertIn("25 of 30 interfaces shown", html)

    def test_empty(self):
        inventory = fake_project_network.fake_inventory()
        inventory["ports"] = []
        self.network.get_inventory.side_effect = lambda: inventory
        context, html = self.render(views.InterfacesView)
        self.assertIn("No running instances with a network interface", html)

    def test_error(self):
        self.network.get_inventory.side_effect = RuntimeError("x")
        self.client.get_project_network_interfaces.side_effect = RuntimeError(
            "y"
        )
        with self.assertLogs(VIEWS, "WARNING"):
            context, html = self.render(views.InterfacesView)
        self.assertIn("Error loading network data", html)
        self.assertIn("pn.destroyGroup('spark')", html)


class InstanceLinkTests(_ViewTestCase):
    def test_instance_link(self):
        self.assertEqual(
            views.instance_link(INSTANCE_METRICS, "c726b640"),
            INSTANCE_METRICS + "?instance=c726b640",
        )
        self.assertEqual(
            views.instance_link(INSTANCE_METRICS, "a&b=c"),
            INSTANCE_METRICS + "?instance=a%26b%3Dc",
        )
        self.assertIsNone(views.instance_link(None, "c726b640"))
        self.assertIsNone(views.instance_link(INSTANCE_METRICS, None))

    def test_instance_metrics_url(self):
        # setUp replaces it: test the real function, both ways
        with mock.patch(
            f"{VIEWS}.reverse", return_value=INSTANCE_METRICS
        ) as rev:
            self.assertEqual(_REAL_IM_URL(), INSTANCE_METRICS)
        rev.assert_called_once_with("horizon:project:instance_metrics:index")
        with mock.patch(
            f"{VIEWS}.reverse", side_effect=NoReverseMatch("not installed")
        ):
            self.assertIsNone(_REAL_IM_URL())

    def test_without_the_panel_names_are_plain(self):
        context, html = self.render(views.InterfacesView)
        self.assertTrue(all(r["link"] is None for r in context["rows"]))
        self.assertNotIn("<a href", html)
        self.assertIn("<strong>web-01</strong>", html)

    def test_with_the_panel_names_link_to_it(self):
        self.mock_im_url.return_value = INSTANCE_METRICS
        context, html = self.render(views.InterfacesView)
        self.assertIn(
            f'<a href="{INSTANCE_METRICS}?instance='
            '5a1e2f9c-3b4d-4e8f-9a0b-1c2d3e4f5a6b"><strong>web-01</strong>',
            html,
        )
        self.assertEqual(len(re.findall(r"<a href=", html)), 3)


_REAL_IM_URL = views.instance_metrics_url


class PanelPermissionsTests(base.TestCase):
    def test_requires_the_metric_storage_service(self):
        from grian_ui.content.project_network import panel

        cls = next(
            c
            for c in vars(panel).values()
            if isinstance(c, type) and getattr(c, "slug", None)
        )
        self.assertIn("openstack.services.metric-storage", cls.permissions)
