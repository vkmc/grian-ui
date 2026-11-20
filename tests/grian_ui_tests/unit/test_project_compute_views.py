#
# SPDX-License-Identifier: Apache-2.0

import json
import re

from unittest import mock

from django.core.cache import cache
from django.template.loader import get_template
from django.test import RequestFactory
from django.test import override_settings
from django.urls import NoReverseMatch
from django.urls import reverse

from grian_ui import cache as grian_cache
from grian_ui import project_compute as pc
from grian_ui import project_compute_inventory
from grian_ui.content.project_compute import views
from grian_ui.datasource import fake
from grian_ui.datasource import fake_project_compute
from grian_ui.local.enabled import _1101_project_telemetry_compute_panel
from grian_ui_tests.unit import base


VIEWS = "grian_ui.content.project_compute.views"
PROJECT = "b8753cd7d01b4455b774b46991f36f83"
OTHER_PROJECT = "7bb2692e0d994decacab9a372f166676"
METRICS = "/dashboard/project/instance_metrics/"
_REAL_METRICS_BASE = views.instance_metrics_base
_REAL_INSTANCE_URL = views.instance_url
_REAL_RESIZE_URL = views.resize_url


def _id(index):
    return fake_project_compute.instance_id(index)


#: Sample servers by name.
IDS = {
    name: _id(i + 1)
    for i, (name, *_rest) in enumerate(fake_project_compute._SERVERS)
}


class _ViewTestCase(base.TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # wrap the fake datasource and inventory so calls can be asserted
        self.client = mock.MagicMock(wraps=fake.get_client())
        patcher = mock.patch(
            f"{VIEWS}.get_datasource", return_value=self.client
        )
        self.mock_get_ds = patcher.start()
        self.addCleanup(patcher.stop)
        self.inventory = mock.MagicMock(
            wraps=project_compute_inventory.FakeComputeInventory()
        )
        patcher = mock.patch(
            f"{VIEWS}.project_compute_inventory.get_inventory",
            return_value=self.inventory,
        )
        self.mock_get_inventory = patcher.start()
        self.addCleanup(patcher.stop)
        # the Instance metrics panel is not installed in the unit tests;
        # LinkTests check both cases
        for name in ("instance_metrics_base", "instance_url", "resize_url"):
            patcher = mock.patch(f"{VIEWS}.{name}", return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _request(self, project_id=PROJECT, **params):
        request = RequestFactory().get("/", params)
        request.user = mock.MagicMock(
            services_region="RegionOne",
            project_id=project_id,
            project_name="web-shop",
        )
        return request

    def render_context(self, view_class, project_id=PROJECT, **params):
        view = view_class()
        view.setup(self._request(project_id, **params))
        return view.get_context_data()

    def render(self, view_class, project_id=PROJECT, **params):
        response = view_class.as_view()(self._request(project_id, **params))
        response.render()
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context = self.render_context(views.ProjectComputeIndexView)
        self.assertEqual(context["project_name"], "web-shop")
        self.assertEqual(context["refresh_seconds"], 60)
        self.assertEqual(context["default_range"], "24h")
        self.assertEqual(context["default_resource"], "cores")
        self.assertEqual(
            [r["key"] for r in context["trend_resources"]],
            ["instances", "cores", "ram"],
        )
        # 15 days of metrics: no 30-day chart, a 15-day forecast
        self.assertEqual(context["retention_days"], 15)
        self.assertEqual(context["forecast_days"], 15)
        self.assertEqual(
            [(r["key"], r["enabled"]) for r in context["ranges"]],
            [("6h", True), ("24h", True), ("7d", True), ("30d", False)],
        )
        self.assertEqual(context["rs_thresholds"]["cpu_high"], 80)
        self.assertEqual(context["thresholds"]["quota_warning"], 75)
        self.assertFalse(context["instance_metrics_enabled"])
        self.assertEqual(context["error_messages"], [])
        # the shell does not query anything
        self.mock_get_ds.assert_not_called()
        self.mock_get_inventory.assert_not_called()

    @override_settings(
        GRIAN_PLUGIN={"datasource": "fake", "metrics_retention_days": 90}
    )
    def test_long_retention(self):
        context = self.render_context(views.ProjectComputeIndexView)
        self.assertEqual(context["forecast_days"], 30)
        self.assertTrue(all(r["enabled"] for r in context["ranges"]))

    def test_template(self):
        source = get_template(
            "project/telemetry_compute/index.html"
        ).template.source
        for name in (
            "summary",
            "quota",
            "trend",
            "idle",
            "rightsizing",
            "attention",
        ):
            self.assertIn(
                f"{{% url 'horizon:project:telemetry_compute:{name}' %}}",
                source,
            )
        self.assertIn(
            '{% url "horizon:project:telemetry_compute:instance" %}', source
        )
        self.assertIn('<details class="panel panel-default pc-section', source)
        self.assertNotIn("<details open", source)
        self.assertIn("GRIAN_PLUGIN['project_rightsizing_thresholds']", source)
        self.assertIn("GRIAN_PLUGIN['project_compute_thresholds']", source)
        self.assertIn('resource_name=~".+:instance-.+"', source)
        # browser-local times
        self.assertIn("toLocaleTimeString", source)
        self.assertIn('id="pc-range-buttons"', source)
        self.assertIn('id="pc-resource-buttons"', source)
        # translated text never goes through innerHTML (escapejs does not
        # escape for HTML)
        self.assertIsNone(re.search(r"\.innerHTML\s*=", source))
        self.assertIn("pc.text.loading", source)

    def test_every_configuration_error_is_shown(self):
        settings = {
            "datasource": "prometheus",
            "project_compute_thresholds": {"bogus": 1},
            "project_rightsizing_thresholds": {"cpu_low": 99},
            "metrics_retention_days": 0,
        }
        with (
            override_settings(GRIAN_PLUGIN=settings),
            mock.patch(
                f"{VIEWS}.config.get_openstack_keystone_url",
                side_effect=views.config.ConfigurationError("no keystone"),
            ),
        ):
            context = self.render_context(views.ProjectComputeIndexView)
        messages = context["error_messages"]
        self.assertEqual(len(messages), 4)
        self.assertIn("bogus", messages[0])
        self.assertIn("cpu_low", messages[1])
        self.assertIn("metrics_retention_days", messages[2])
        self.assertEqual(messages[3], "no keystone")
        self.assertNotIn("thresholds", context)
        # falls back to the default retention for the buttons
        self.assertEqual(context["retention_days"], 15)


class CacheIsolationTests(_ViewTestCase):
    def test_key_holds_the_project(self):
        key_a = views._cache_key(self._request(PROJECT), "nova")
        key_b = views._cache_key(self._request(OTHER_PROJECT), "nova")
        self.assertNotEqual(key_a, key_b)
        self.assertIn(f"project:{PROJECT}", key_a)
        self.assertIn("project_compute", key_a)

    def test_projects_never_share_a_value(self):
        for view in (
            views.SummaryView,
            views.QuotaView,
            views.IdleView,
            views.AttentionView,
        ):
            self.render_context(view, PROJECT)
        self.assertEqual(self.inventory.read.call_count, 1)
        self.render_context(views.SummaryView, OTHER_PROJECT)
        self.assertEqual(self.inventory.read.call_count, 2)
        self.assertEqual(
            self.client.get_project_compute_snapshot.call_count, 2
        )

    def test_queries_carry_the_project(self):
        self.render_context(views.SummaryView)
        self.render_context(views.TrendView)
        self.render_context(views.RightsizingView)
        self.render_context(views.InstanceView, instance=IDS["db-01"])
        for method in (
            "get_project_compute_snapshot",
            "get_project_compute_rightsizing",
            "get_project_compute_forecast",
            "get_project_compute_trend",
            "get_project_compute_cpu_series",
            "get_project_compute_instance_trend",
        ):
            calls = getattr(self.client, method).call_args_list
            self.assertTrue(calls, method)
            for call in calls:
                self.assertEqual(call.args[0], f'project="{PROJECT}"', method)

    def test_no_project(self):
        html = self.render(views.RightsizingView, project_id=None)
        self.assertIn("data-pc-nodata", html)
        self.client.get_project_compute_rightsizing.assert_not_called()
        html = self.render(views.SummaryView, project_id="bad id")
        self.assertIn("data-pc-nodata", html)


class SummaryViewTests(_ViewTestCase):
    def test_cards(self):
        context = self.render_context(views.SummaryView)
        self.assertEqual(context["highest"]["key"], pc.QUOTA_CORES)
        self.assertEqual(context["highest"]["level"], pc.LEVEL_WARNING)
        self.assertEqual(str(context["highest_detail"]), "vCPUs")
        self.assertEqual(
            context["counts"], {"total": 18, "running": 16, "not_running": 2}
        )
        self.assertEqual(context["idle"]["ram"], 12288)
        self.assertEqual(context["idle_level"], pc.LEVEL_WARNING)
        self.assertEqual(context["resize"]["counts"]["under"], 4)
        self.assertEqual(context["resize"]["counts"]["over"], 3)
        self.assertTrue(context["resize"]["provisional"])
        self.assertNotIn("history_hours", context)
        self.assertFalse(context.get("metrics_unavailable", False))

    def test_render(self):
        html = self.render(views.SummaryView)
        self.assertIn("Highest quota use", html)
        self.assertIn("86%", html)
        self.assertIn("16<small> / 18</small>", html)
        self.assertIn("2 not running", html)
        self.assertIn("12 GiB", html)
        self.assertIn("6 vCPU | 2 instances | 12% of your RAM quota", html)
        self.assertIn("4 undersized | 3 oversized (provisional)", html)
        self.assertIn('class="pc-clock"', html)
        self.assertNotIn("pc-history-alert", html)

    def test_ram_is_the_highest(self):
        inventory = fake_project_compute.get_inventory()
        inventory["limits"]["maxTotalRAMSize"] = 86000
        self.inventory.read.side_effect = None
        self.inventory.read.return_value = inventory
        context = self.render_context(views.SummaryView)
        self.assertEqual(context["highest"]["key"], pc.QUOTA_RAM)
        self.assertEqual(
            str(context["highest_detail"]), "RAM (83 GiB of 84 GiB)"
        )
        self.assertEqual(context["highest"]["level"], pc.LEVEL_CRITICAL)

    def test_idle_ram_turns_red(self):
        with override_settings(
            GRIAN_PLUGIN={
                "datasource": "fake",
                "project_compute_thresholds": {"idle_ram_critical": 10},
            }
        ):
            context = self.render_context(views.SummaryView)
        self.assertEqual(context["idle_level"], pc.LEVEL_CRITICAL)

    def test_short_history(self):
        self.client.get_project_compute_forecast.side_effect = None
        self.client.get_project_compute_forecast.return_value = {
            "slopes": dict.fromkeys(pc.QUOTAS, 0.0),
            "hours": 54.0,
            "days": 15,
        }
        html = self.render(views.SummaryView)
        self.assertIn("About 54 hours of history.", html)
        self.assertIn("provisional and there is no forecast yet", html)

    @override_settings(
        GRIAN_PLUGIN={"datasource": "fake", "metrics_retention_days": 3}
    )
    def test_short_retention(self):
        html = self.render(views.SummaryView)
        self.assertIn("Metrics are kept for 3 days only", html)

    def test_metrics_unavailable(self):
        self.client.get_project_compute_snapshot.side_effect = RuntimeError(
            "secret backend message"
        )
        self.client.get_project_compute_rightsizing.side_effect = RuntimeError(
            "secret"
        )
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(views.SummaryView)
            cache.clear()
            html = self.render(views.SummaryView)
        self.assertTrue(context["metrics_unavailable"])
        self.assertNotIn("resize", context)
        # the cards from the Compute API are still shown
        self.assertIn("Could not load telemetry data", html)
        self.assertIn("Quota usage below comes from the Compute API", html)
        self.assertIn("metrics unavailable", html)
        self.assertIn("16<small> / 18</small>", html)
        self.assertNotIn("secret", html)

    def test_nova_error(self):
        self.inventory.read.side_effect = RuntimeError("nova secret")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.SummaryView)
        self.assertIn("Error loading the instances and quota", html)
        self.assertIn("data-pc-nodata", html)
        self.assertNotIn("nova secret", html)
        self.assertNotIn("pc-retry-", html)

    def test_still_computing_retries(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            html = self.render(views.SummaryView)
        self.assertIn("Still loading", html)
        self.assertIn("pc-retry-", html)
        self.assertIn("htmx.trigger(target, 'pc-retry')", html)

    def test_stale_nova_copy(self):
        nova = dict(
            fake_project_compute.get_inventory(),
            fetched_at=1_700_000_000,
            is_stale_copy=True,
        )
        real = views._ProjectComputeMixin.get_or_compute

        def get_or_compute(view, name, ttl, compute):
            if name == "nova":
                return nova
            return real(view, name, ttl, compute)

        with mock.patch.object(
            views._ProjectComputeMixin, "get_or_compute", get_or_compute
        ):
            html = self.render(views.SummaryView)
        self.assertIn("The Compute API could not be reached", html)
        self.assertIn('data-ts="1700000000"', html)

    def test_old_unmarked_copy_is_marked(self):
        nova = dict(
            fake_project_compute.get_inventory(),
            fetched_at=1_700_000_000,
            stored_at=1_700_000_000,
        )

        def get_or_compute(key, ttl, compute):
            return nova if ":nova:" in key else compute()

        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=get_or_compute
        ):
            context = self.render_context(views.SummaryView)
            html = self.render(views.SummaryView)
        self.assertTrue(context["nova_old"])
        self.assertFalse(context["nova_stale"])
        self.assertIn("may be out of date", html)

    def test_configuration_error_in_the_computation(self):
        error = grian_cache.ComputationFailed("failed")
        error.__cause__ = views.config.ConfigurationError("bad")
        with (
            mock.patch(f"{VIEWS}.cache.get_or_compute", side_effect=error),
            self.assertLogs(VIEWS, "ERROR"),
        ):
            context = self.render_context(views.SummaryView)
        self.assertIn("invalid configuration", str(context["error_message"]))


class QuotaViewTests(_ViewTestCase):
    def test_rows_and_forecast(self):
        context = self.render_context(views.QuotaView)
        rows = {r["key"]: r for r in context["rows"]}
        self.assertEqual(rows["instances"]["forecast"]["days"], 28)
        self.assertEqual(rows["cores"]["forecast"]["days"], 19)
        self.assertEqual(rows["cores"]["forecast"]["level"], pc.LEVEL_WARNING)
        self.assertEqual(rows["ram"]["forecast"]["days"], 12)
        self.assertEqual(rows["ram"]["forecast"]["level"], pc.LEVEL_CRITICAL)
        self.assertEqual(context["forecast_days"], 15)
        self.assertFalse(context["any_mismatch"])
        # the forecast looks back at most the retention
        self.client.get_project_compute_forecast.assert_called_once_with(
            f'project="{PROJECT}"', 15
        )

    def test_render(self):
        html = self.render(views.QuotaView)
        self.assertIn("Compute API", html)
        self.assertIn("43</strong> / 50", html)
        self.assertIn("(86%)", html)
        self.assertIn("Full in ~19 days", html)
        self.assertIn("Full in ~12 days", html)
        self.assertIn("pc-seg-running", html)
        self.assertIn("pc-seg-stopped", html)
        self.assertIn("pc-seg-error", html)
        self.assertIn('aria-label="vCPUs: 43 of 50 used', html)
        self.assertIn("(Metrics are kept for 15 days.)", html)

    @override_settings(
        GRIAN_PLUGIN={"datasource": "fake", "metrics_retention_days": 45}
    )
    def test_forecast_window_is_clamped_to_30_days(self):
        self.render_context(views.QuotaView)
        self.client.get_project_compute_forecast.assert_called_once_with(
            f'project="{PROJECT}"', 30
        )
        html = self.render(views.QuotaView)
        self.assertIn("trend of the last 30 days", html)
        self.assertNotIn("Metrics are kept", html)

    def test_not_enough_history(self):
        self.client.get_project_compute_forecast.side_effect = None
        self.client.get_project_compute_forecast.return_value = {
            "slopes": dict.fromkeys(pc.QUOTAS, 0.0),
            "hours": 54.0,
            "days": 15,
        }
        html = self.render(views.QuotaView)
        self.assertEqual(html.count("Not enough history"), 3)

    def test_not_growing(self):
        self.client.get_project_compute_forecast.side_effect = None
        self.client.get_project_compute_forecast.return_value = {
            "slopes": dict.fromkeys(pc.QUOTAS, 0.0),
            "hours": 360.0,
            "days": 15,
        }
        html = self.render(views.QuotaView)
        self.assertEqual(html.count("Not growing"), 3)

    def test_forecast_unavailable(self):
        self.client.get_project_compute_forecast.side_effect = RuntimeError()
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.QuotaView)
        # the bars still come from the Compute API
        self.assertIn("43</strong> / 50", html)
        self.assertEqual(html.count("Forecast unavailable"), 3)

    def test_mismatch_note(self):
        inventory = fake_project_compute.get_inventory()
        inventory["limits"]["totalInstancesUsed"] = 19
        self.inventory.read.side_effect = None
        self.inventory.read.return_value = inventory
        html = self.render(views.QuotaView)
        self.assertIn("can differ while instances are being created", html)
        self.assertIn("does not count shelved-offloaded instances", html)

    def test_unlimited(self):
        inventory = fake_project_compute.get_inventory()
        inventory["limits"]["maxTotalCores"] = None
        self.inventory.read.side_effect = None
        self.inventory.read.return_value = inventory
        html = self.render(views.QuotaView)
        self.assertIn("vCPUs: 43 used, no limit", html)
        self.assertIn("No limit", html)

    def test_nova_error(self):
        self.inventory.read.side_effect = RuntimeError("boom")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.QuotaView)
        self.assertIn("data-pc-nodata", html)
        self.assertIn("Compute API", html)


class TrendViewTests(_ViewTestCase):
    def chart(self, html):
        match = re.search(
            r'<script id="pc-trend-data-[0-9a-f]+" type="application/json">'
            r"(.*?)</script>",
            html,
        )
        return json.loads(match.group(1))

    def test_default(self):
        context = self.render_context(views.TrendView)
        chart = context["chart"]
        self.assertEqual(chart["range"], "24h")
        self.assertEqual(chart["resource"], "cores")
        self.assertEqual(len(chart["timestamps"]), 96)
        self.assertEqual(chart["values"][-1], 36.0)
        self.assertEqual(chart["quota_now"], 43)
        self.assertEqual(chart["limit"], 50)
        self.assertEqual(
            context["gap"], {"quota": "43", "running": "36", "gap": "7"}
        )
        self.assertNotIn("data_since_ts", context)

    def test_render(self):
        html = self.render(views.TrendView, resource="ram", range="7d")
        self.assertIn("RAM of running instances (metrics)", html)
        self.assertIn("Counted by the quota now (Compute API)", html)
        self.assertIn("Quota limit", html)
        self.assertIn("The quota counts <strong>83 GiB</strong>", html)
        chart = self.chart(html)
        self.assertEqual(chart["range"], "7d")
        self.assertEqual(len(chart["timestamps"]), 84)
        self.assertEqual(chart["limit"], 102400)

    def test_gap_note_on_30_days(self):
        context = self.render_context(views.TrendView, range="30d")
        self.assertEqual(len(context["chart"]["timestamps"]), 90)
        self.assertIn("data_since_ts", context)
        self.assertIsNone(context["chart"]["values"][0])

    def test_invalid_parameters_fall_back(self):
        context = self.render_context(
            views.TrendView, range="1y", resource="gpus"
        )
        self.assertEqual(context["trend_range"], "24h")
        self.assertEqual(context["resource"], "cores")

    def test_trends_cached_per_range_and_resource(self):
        self.render_context(views.TrendView)
        self.render_context(views.TrendView)
        self.render_context(views.TrendView, range="6h")
        self.render_context(views.TrendView, resource="ram")
        self.assertEqual(self.client.get_project_compute_trend.call_count, 3)

    def test_without_nova(self):
        self.inventory.read.side_effect = RuntimeError("down")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.TrendView)
        chart = self.chart(html)
        self.assertIsNone(chart["quota_now"])
        self.assertIsNone(chart["limit"])
        self.assertIn("The quota could not be read from the Compute API", html)
        self.assertNotIn("Counted by the quota now (Compute API)", html)

    def test_no_data(self):
        self.client.get_project_compute_trend.side_effect = None
        self.client.get_project_compute_trend.return_value = {"points": []}
        html = self.render(views.TrendView)
        self.assertIn("No running instance reported in the last 24h", html)
        # the quota lines are still drawn
        self.assertIn("pc-trend-canvas-", html)

    def test_no_data_and_no_nova(self):
        self.client.get_project_compute_trend.side_effect = None
        self.client.get_project_compute_trend.return_value = {"points": []}
        self.inventory.read.side_effect = RuntimeError("down")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.TrendView)
        self.assertNotIn("pc-trend-canvas-", html)
        self.assertIn("pc.destroyChart('trend')", html)

    def test_error(self):
        self.client.get_project_compute_trend.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.TrendView)
        self.assertIn("Trend unavailable: no metrics.", html)
        self.assertIn("pc.destroyChart('trend')", html)


class IdleViewTests(_ViewTestCase):
    def test_rows(self):
        context = self.render_context(views.IdleView)
        self.assertEqual(
            [r["name"] for r in context["rows"]], ["build-01", "old-test"]
        )
        self.assertEqual(context["count"], 2)
        self.assertEqual(context["more"], 0)
        self.assertEqual(context["rows"][0]["since"], "23 days")

    def test_render(self):
        html = self.render(views.IdleView)
        self.assertIn("build-01", html)
        self.assertIn("m1.large | for 23 days", html)
        self.assertIn("No valid host was found.", html)
        self.assertIn("(8% of RAM quota)", html)
        # no Instances panel in the unit tests: no buttons
        self.assertNotIn("Delete…", html)

    def test_empty(self):
        inventory = fake_project_compute.get_inventory()
        inventory["servers"] = [
            s for s in inventory["servers"] if s["status"] == "ACTIVE"
        ]
        self.inventory.read.side_effect = None
        self.inventory.read.return_value = inventory
        html = self.render(views.IdleView)
        self.assertIn("Every instance that holds quota is running.", html)

    def test_bounded(self):
        inventory = fake_project_compute.get_inventory()
        inventory["servers"] = [
            dict(s, id=_id(100 + i), name=f"stopped-{i:02d}", status="SHUTOFF")
            for i, s in enumerate(inventory["servers"])
        ]
        self.inventory.read.side_effect = None
        self.inventory.read.return_value = inventory
        context = self.render_context(views.IdleView)
        self.assertEqual(len(context["rows"]), pc.IDLE_LIMIT)
        self.assertEqual(context["more"], 8)
        html = self.render(views.IdleView)
        self.assertIn("8 more instances not shown.", html)

    def test_error(self):
        self.inventory.read.side_effect = RuntimeError("down")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.IdleView)
        self.assertIn("Error loading the instances and quota", html)


class RightsizingViewTests(_ViewTestCase):
    def test_rows(self):
        context = self.render_context(views.RightsizingView)
        rows = context["rows"]
        self.assertEqual(len(rows), 16)
        self.assertEqual(
            [r["name"] for r in rows[:4]],
            ["api-03", "api-02", "api-01", "db-01"],
        )
        self.assertEqual(rows[4]["name"], "batch-02")
        self.assertEqual(rows[-1]["name"], "probe-01")
        self.assertEqual(
            [(f["key"], f["count"]) for f in context["filters"]],
            [("", 16), ("under", 4), ("over", 3), ("ok", 8)],
        )
        self.assertEqual(rows[0]["tag_class"], "pc-tag-crit")
        # sparklines for the assessed rows only, against the flavor
        self.assertEqual(len(context["sparks"]), 15)
        spark = context["sparks"][IDS["api-03"]]
        self.assertEqual(len(spark["values"]), 84)
        self.assertEqual(spark["max"], 2)
        self.assertTrue(spark["busy"])

    def test_render(self):
        html = self.render(views.RightsizingView)
        self.assertIn(
            "Running instances use <strong>10.8 of the 36 vCPUs", html
        )
        self.assertIn("api-03", html)
        self.assertIn("1.86 of 2", html)
        self.assertIn("(93%)", html)
        self.assertIn("swap</span>", html)
        self.assertIn("Undersized", html)
        self.assertIn("provisional | 9 h of data", html)
        self.assertIn("No data", html)
        self.assertIn(f'data-pc-instance="{IDS["db-01"]}"', html)
        self.assertIn('data-pc-spark="', html)
        self.assertIn("7-day statistics from", html)

    def test_filter(self):
        context = self.render_context(views.RightsizingView, group="over")
        self.assertEqual(
            [r["name"] for r in context["rows"]],
            ["batch-02", "cache-01", "batch-01"],
        )
        self.assertEqual(context["matching"], 3)
        html = self.render(views.RightsizingView, group="under")
        self.assertRegex(html, r'data-group="under"\s+aria-pressed="true"')
        self.assertRegex(html, r'data-group=""\s+aria-pressed="false"')
        # an unknown group shows all
        context = self.render_context(views.RightsizingView, group="bogus")
        self.assertIsNone(context["group"])
        self.assertEqual(len(context["rows"]), 16)

    def test_empty_group(self):
        stats = fake_project_compute.get_rightsizing()
        for row in stats["instances"].values():
            row["swap"] = 0
            row["cpu_avg"] = row["cpu_p95"] = 0.5
            row["memory_avg"] = 400
        self.client.get_project_compute_rightsizing.side_effect = None
        self.client.get_project_compute_rightsizing.return_value = stats
        html = self.render(views.RightsizingView, group="under")
        self.assertIn("No instances in this group.", html)

    def test_bounded(self):
        inventory = fake_project_compute.get_inventory()
        extra = [
            dict(
                inventory["servers"][0], id=_id(200 + i), name=f"web-x{i:02d}"
            )
            for i in range(30)
        ]
        inventory["servers"] += extra
        self.inventory.read.side_effect = None
        self.inventory.read.return_value = inventory
        context = self.render_context(views.RightsizingView)
        self.assertEqual(len(context["rows"]), pc.TABLE_LIMIT)
        self.assertEqual(context["total"], 46)
        html = self.render(views.RightsizingView)
        self.assertIn("46 instances, 25 shown.", html)
        # the sparklines are only queried for the listed rows
        ids = self.client.get_project_compute_cpu_series.call_args.args[1]
        self.assertLessEqual(len(ids), pc.TABLE_LIMIT)

    def test_without_nova_names_come_from_the_metrics(self):
        self.inventory.read.side_effect = RuntimeError("down")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(views.RightsizingView)
        self.assertTrue(context["nova_unavailable"])
        names = {r["name"] for r in context["rows"]}
        # probe-01 reports no metrics: unknown without the Compute API
        self.assertEqual(len(names), 15)
        self.assertIn("db-01", names)
        self.assertIsNone(context["rows"][0]["flavor"])
        self.assertIn(
            "The Compute API could not be reached",
            str(context["partial_notes"][0]),
        )

    def test_without_sparklines(self):
        self.client.get_project_compute_cpu_series.side_effect = RuntimeError()
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.RightsizingView)
        self.assertIn("The CPU sparklines could not be loaded.", html)
        self.assertIn("api-03", html)

    def test_error(self):
        self.client.get_project_compute_rightsizing.side_effect = (
            RuntimeError()
        )
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.RightsizingView)
        self.assertIn("Unavailable: no metrics.", html)
        self.assertIn("data-pc-nodata", html)

    def test_loading(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            html = self.render(views.RightsizingView)
        self.assertIn("pc-retry-", html)


class InstanceViewTests(_ViewTestCase):
    def test_chart(self):
        context = self.render_context(
            views.InstanceView, instance=IDS["db-01"]
        )
        self.assertEqual(context["name"], "db-01")
        chart = context["chart"]
        self.assertEqual(len(chart["timestamps"]), 168)
        self.assertEqual((chart["vcpus"], chart["ram"]), (4, 8192))
        self.assertTrue(context["has_data"])
        self.assertEqual(context["assessment"]["assessment"], pc.UNDERSIZED)

    def test_render(self):
        html = self.render(views.InstanceView, instance=IDS["cache-01"])
        self.assertIn("CPU used (vCPUs)", html)
        self.assertIn("vCPUs in flavor", html)
        self.assertIn("Flavor RAM", html)
        self.assertIn(
            "A smaller flavor would free quota (up to 1 vCPUs)", html
        )
        self.assertIn("pc-detail-cpu-", html)

    def test_invalid(self):
        html = self.render(views.InstanceView, instance="../../etc")
        self.assertIn("Invalid instance ID.", html)
        self.client.get_project_compute_instance_trend.assert_not_called()

    def test_no_data(self):
        html = self.render(views.InstanceView, instance=IDS["probe-01"])
        self.assertIn("No metrics for this instance in the last 7 days.", html)

    def test_cached_per_instance(self):
        self.render_context(views.InstanceView, instance=IDS["db-01"])
        self.render_context(views.InstanceView, instance=IDS["db-01"])
        self.render_context(views.InstanceView, instance=IDS["api-01"])
        self.assertEqual(
            self.client.get_project_compute_instance_trend.call_count, 2
        )

    def test_unknown_instance_uses_the_metrics(self):
        self.inventory.read.side_effect = RuntimeError("down")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(
                views.InstanceView, instance=IDS["db-01"]
            )
        self.assertEqual(context["name"], "db-01")
        self.assertEqual(context["chart"]["vcpus"], 4)

    def test_error(self):
        self.client.get_project_compute_instance_trend.side_effect = (
            RuntimeError()
        )
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.InstanceView, instance=IDS["db-01"])
        self.assertIn("data-pc-nodata", html)
        self.assertIn("pc.destroyCharts('detail-", html)


class AttentionViewTests(_ViewTestCase):
    def test_rows(self):
        context = self.render_context(views.AttentionView)
        self.assertEqual(
            [(r["name"], r["severity"]) for r in context["rows"]],
            [("api-03", 3), ("old-test", 3), ("api-02", 2), ("db-01", 2)],
        )
        self.assertIn("from the host", str(context["rows"][0]["advice_text"]))
        self.assertEqual(context["count"], 4)

    def test_render(self):
        html = self.render(views.AttentionView)
        self.assertIn("CPU wait 25%", html)
        self.assertIn("ERROR state", html)
        self.assertIn("Fault:</strong> No valid host was found.", html)
        self.assertIn("swap 0.8 MiB/s", html)
        self.assertIn("disk latency 34 ms", html)
        self.assertIn("Resize to a flavor with more RAM.", html)
        self.assertIn("Symptoms use the last 10 minutes.", html)

    def test_nothing_to_report(self):
        inventory = fake_project_compute.get_inventory()
        inventory["servers"] = [
            s for s in inventory["servers"] if s["status"] != "ERROR"
        ]
        self.inventory.read.side_effect = None
        self.inventory.read.return_value = inventory
        self.client.get_project_compute_snapshot.side_effect = None
        self.client.get_project_compute_snapshot.return_value = {
            "instances": {}
        }
        html = self.render(views.AttentionView)
        self.assertIn("No instance in this project needs attention.", html)

    def test_without_metrics(self):
        self.client.get_project_compute_snapshot.side_effect = RuntimeError()
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.AttentionView)
        self.assertIn("old-test", html)
        self.assertNotIn("api-03", html)
        self.assertIn("Telemetry data could not be loaded", html)

    def test_without_nova(self):
        self.inventory.read.side_effect = RuntimeError()
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.AttentionView)
        self.assertIn("api-03", html)
        self.assertNotIn("old-test", html)
        self.assertIn("The Compute API could not be reached", html)

    def test_both_fail(self):
        self.inventory.read.side_effect = RuntimeError()
        self.client.get_project_compute_snapshot.side_effect = RuntimeError()
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.AttentionView)
        self.assertIn("data-pc-nodata", html)
        self.assertNotIn("pc-retry-", html)

    def test_both_loading(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            html = self.render(views.AttentionView)
        self.assertIn("Still loading", html)
        self.assertIn("pc-retry-", html)

    def test_bounded(self):
        inventory = fake_project_compute.get_inventory()
        template = inventory["servers"][0]
        inventory["servers"] = [
            dict(
                template, id=_id(300 + i), name=f"err-{i:02d}", status="ERROR"
            )
            for i in range(30)
        ]
        self.inventory.read.side_effect = None
        self.inventory.read.return_value = inventory
        context = self.render_context(views.AttentionView)
        self.assertEqual(len(context["rows"]), pc.TABLE_LIMIT)
        html = self.render(views.AttentionView)
        self.assertIn("30 instances need attention, 25 shown.", html)


class LinkTests(_ViewTestCase):
    """Instance names link to the Instance metrics page when it exists."""

    def test_metrics_link(self):
        self.assertEqual(
            views.metrics_link(METRICS, IDS["db-01"]),
            f"{METRICS}?instance={IDS['db-01']}",
        )
        self.assertIsNone(views.metrics_link(None, IDS["db-01"]))
        self.assertIsNone(views.metrics_link(METRICS, None))
        self.assertEqual(
            views.metrics_link(METRICS, "a&b=c"),
            f"{METRICS}?instance=a%26b%3Dc",
        )

    def test_instance_metrics_base(self):
        # setUp replaces it: test the real function, both ways
        with mock.patch(f"{VIEWS}.reverse", return_value=METRICS) as rev:
            self.assertEqual(_REAL_METRICS_BASE(), METRICS)
        rev.assert_called_once_with("horizon:project:instance_metrics:index")
        with mock.patch(
            f"{VIEWS}.reverse", side_effect=NoReverseMatch("not installed")
        ):
            self.assertIsNone(_REAL_METRICS_BASE())

    def test_instance_pages(self):
        with mock.patch(f"{VIEWS}.reverse", return_value="/x/") as rev:
            self.assertEqual(_REAL_INSTANCE_URL("i"), "/x/")
            self.assertEqual(_REAL_RESIZE_URL("i"), "/x/")
        rev.assert_any_call("horizon:project:instances:detail", args=["i"])
        rev.assert_any_call("horizon:project:instances:resize", args=["i"])
        self.assertIsNone(_REAL_INSTANCE_URL("i"))
        self.assertIsNone(_REAL_RESIZE_URL("i"))

    def test_without_the_panel_names_are_plain(self):
        for view, params in (
            (views.IdleView, {}),
            (views.RightsizingView, {}),
            (views.AttentionView, {}),
            (views.InstanceView, {"instance": IDS["db-01"]}),
        ):
            html = self.render(view, **params)
            self.assertNotIn("instance_metrics", html)
            self.assertNotIn("?instance=", html)

    def test_names_link_to_the_metrics_page(self):
        with (
            mock.patch(f"{VIEWS}.instance_metrics_base", return_value=METRICS),
            mock.patch(f"{VIEWS}.instance_url", return_value="/detail/"),
            mock.patch(f"{VIEWS}.resize_url", return_value="/resize/"),
        ):
            idle = self.render(views.IdleView)
            rightsizing = self.render(views.RightsizingView)
            attention = self.render(views.AttentionView)
            detail = self.render(views.InstanceView, instance=IDS["db-01"])
            index = self.render_context(views.ProjectComputeIndexView)
        old_test = f'href="{METRICS}?instance={IDS["old-test"]}"'
        self.assertIn(old_test, idle)
        self.assertIn('href="/detail/"', idle)
        self.assertIn("Delete…", idle)
        self.assertIn("Manage…", idle)
        self.assertIn(f'href="{METRICS}?instance={IDS["db-01"]}"', rightsizing)
        self.assertIn(old_test, attention)
        self.assertIn(f'href="{METRICS}?instance={IDS["db-01"]}"', detail)
        self.assertIn("All metrics for db-01", detail)
        self.assertIn('href="/resize/"', detail)
        self.assertTrue(index["instance_metrics_enabled"])


class PanelTests(base.TestCase):
    def test_enabled_file(self):
        enabled = _1101_project_telemetry_compute_panel
        self.assertEqual(enabled.PANEL, "telemetry_compute")
        self.assertEqual(enabled.PANEL_DASHBOARD, "project")
        self.assertEqual(enabled.PANEL_GROUP, "telemetry")
        self.assertEqual(
            enabled.ADD_PANEL,
            "grian_ui.content.project_compute.panel.ProjectCompute",
        )
        self.assertFalse(hasattr(enabled, "ADD_INSTALLED_APPS"))

    def test_urls(self):
        for name in (
            "index",
            "summary",
            "quota",
            "trend",
            "idle",
            "rightsizing",
            "instance",
            "attention",
        ):
            self.assertTrue(
                reverse(
                    f"horizon:project:telemetry_compute:{name}"
                ).startswith("/dashboard/project/telemetry_compute/")
            )


class PanelPermissionsTests(base.TestCase):
    def test_requires_the_metric_storage_service(self):
        from grian_ui.content.project_compute import panel

        cls = next(
            c
            for c in vars(panel).values()
            if isinstance(c, type) and getattr(c, "slug", None)
        )
        self.assertIn("openstack.services.metric-storage", cls.permissions)
