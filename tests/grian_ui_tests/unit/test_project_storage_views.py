#
# SPDX-License-Identifier: Apache-2.0

import importlib
import json
import re

from unittest import mock

from django.core.cache import cache
from django.template.loader import get_template
from django.test import RequestFactory
from django.urls import NoReverseMatch
from django.urls import reverse

from grian_ui import cache as grian_cache
from grian_ui import project_storage as ps
from grian_ui import project_storage_inventory
from grian_ui.content.project_storage import views
from grian_ui.datasource import fake
from grian_ui.datasource import fake_project_storage
from grian_ui_tests.unit import base


VIEWS = "grian_ui.content.project_storage.views"
P1 = "b8753cd7d01b4455b774b46991f36f83"
P2 = "7bb2692e0d994decacab9a372f166676"
PARTIALS = (
    "SummaryView",
    "QuotaView",
    "FootprintView",
    "CleanupView",
    "VolumesView",
    "ImagesView",
)
_REAL_URL = views._url


def _chart_data(html):
    match = re.search(
        r'<script id="ps-footprint-data-\w+" type="application/json">'
        r"(.*?)</script>",
        html,
        re.S,
    )
    return json.loads(match.group(1))


class _ViewTestCase(base.TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch(f"{VIEWS}.get_datasource")
        self.mock_get_ds = patcher.start()
        self.addCleanup(patcher.stop)
        # wrap the fake datasource and inventory so calls can be asserted
        self.client = mock.MagicMock(wraps=fake.get_client())
        self.mock_get_ds.return_value = self.client
        patcher = mock.patch(
            f"{VIEWS}.project_storage_inventory.get_inventory"
        )
        self.mock_get_inventory = patcher.start()
        self.addCleanup(patcher.stop)
        self.inventory = mock.MagicMock(
            wraps=project_storage_inventory.FakeProjectStorageInventory()
        )
        self.mock_get_inventory.return_value = self.inventory
        # no Horizon panel URLs in the unit tests: no links by default
        patcher = mock.patch(f"{VIEWS}._url", return_value=None)
        self.mock_url = patcher.start()
        self.addCleanup(patcher.stop)

    def _request(self, project_id=P1, **params):
        request = RequestFactory().get("/", params)
        request.user = mock.MagicMock(
            services_region="RegionOne",
            project_id=project_id,
            project_name="web-shop",
        )
        return request

    def render_context(self, view_class, project_id=P1, **params):
        view = view_class()
        view.setup(self._request(project_id, **params))
        return view.get_context_data()

    def render(self, view_class, project_id=P1, **params):
        response = view_class.as_view()(self._request(project_id, **params))
        response.render()
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def empty_project(self):
        self.inventory.block_storage.side_effect = None
        self.inventory.block_storage.return_value = {
            "limits": dict(fake_project_storage.LIMITS),
            "volumes": [],
            "snapshots": [],
            "backup_enabled": False,
        }
        self.inventory.images.return_value = {
            "images": [],
            "truncated": False,
        }
        self.client.get_project_storage_footprint.return_value = {
            "volumes": [],
            "snapshots": [],
        }
        self.client.get_project_storage_trend.return_value = {
            "gigabytes": [],
            "volumes": [],
            "snapshots": [],
        }


class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context = self.render_context(views.IndexView)
        self.assertEqual(context["ranges"], ["7d", "30d", "90d"])
        self.assertEqual(context["default_range"], "30d")
        self.assertEqual(context["refresh_seconds"], 300)
        self.assertEqual(context["refresh_minutes"], 5)
        self.assertEqual(context["project_name"], "web-shop")
        self.assertEqual(context["thresholds"]["quota_warning"], 75)
        self.assertEqual(context["retention_days"], 15)
        self.assertEqual(context["forecast_history_days"], 15)
        self.assertEqual(context["ranges_beyond_retention"], ["30d", "90d"])
        self.assertEqual(context["error_messages"], [])
        # the shell reads no API and no metric
        self.mock_get_ds.assert_not_called()
        self.mock_get_inventory.assert_not_called()

    def test_long_retention(self):
        with self.settings(
            GRIAN_PLUGIN={"datasource": "fake", "metrics_retention_days": 400}
        ):
            context = self.render_context(views.IndexView)
        self.assertEqual(context["ranges_beyond_retention"], [])
        self.assertEqual(context["forecast_history_days"], 30)

    def test_every_configuration_error_is_shown(self):
        with (
            self.settings(
                GRIAN_PLUGIN={
                    "datasource": "prometheus",
                    "metrics_retention_days": 0,
                    "project_storage_thresholds": {"nope": 1},
                }
            ),
            mock.patch(
                f"{VIEWS}.config.get_openstack_keystone_url",
                side_effect=views.config.ConfigurationError("no keystone"),
            ),
        ):
            context = self.render_context(views.IndexView)
        errors = context["error_messages"]
        self.assertEqual(len(errors), 3)
        self.assertIn("Unknown key 'nope'", errors[0])
        self.assertIn("metrics_retention_days", errors[1])
        self.assertEqual(errors[2], "no keystone")
        self.assertNotIn("thresholds", context)

    def test_template(self):
        source = get_template(
            "project/telemetry_storage/index.html"
        ).template.source
        for name in (
            "summary",
            "quota",
            "footprint",
            "cleanup",
            "volumes",
            "images",
        ):
            self.assertIn(
                f"{{% url 'horizon:project:telemetry_storage:{name}' %}}",
                source,
            )
        # only the chart follows the range
        self.assertEqual(source.count('hx-include="#ps-controls"'), 1)
        self.assertIn('id="ps-range-buttons"', source)
        # a native disclosure, collapsed by default, inside the root
        self.assertIn('<details class="panel panel-default ps-section', source)
        self.assertNotIn("<details open", source)
        self.assertLess(
            source.index('id="ps-root"'), source.index('id="ps-how"')
        )
        self.assertIn("GRIAN_PLUGIN['project_storage_thresholds']", source)
        # browser-local dates and times
        self.assertIn("toLocaleDateString", source)
        self.assertIn("toLocaleTimeString", source)
        # failed refreshes are marked, an expired session reloads
        self.assertIn("htmx:responseError", source)
        self.assertIn("status === 401", source)
        self.assertIn("Auto-refresh every", source)

    def test_urls(self):
        self.assertEqual(
            reverse("horizon:project:telemetry_storage:index"),
            "/dashboard/project/telemetry_storage/",
        )
        self.assertEqual(
            reverse("horizon:project:telemetry_storage:footprint"),
            "/dashboard/project/telemetry_storage/footprint/",
        )


class SummaryViewTests(_ViewTestCase):
    def test_cards(self):
        context = self.render_context(views.SummaryView)
        self.assertEqual(context["gigabytes"]["percent"], 84)
        self.assertEqual(context["gigabytes"]["level"], ps.LEVEL_WARNING)
        self.assertEqual(context["usage"]["unattached"], 2)
        self.assertEqual(
            (context["freed_gb"], context["freed_count"]), (480, 3)
        )
        self.assertFalse(context["data_stale"])
        self.assertTrue(context["fetched_ts"])

    def test_render(self):
        html = self.render(views.SummaryView)
        self.assertIn("84%", html)
        self.assertIn("840 of 1000 GB | volumes + snapshots", html)
        self.assertIn("5<small> / 10</small>", html)
        self.assertIn("2 unattached", html)
        self.assertIn("290 GB", html)
        self.assertIn("480 GB", html)
        self.assertIn("3 clean-up candidates", html)
        self.assertIn("ps-card ps-warning", html)
        self.assertIn("ps.updated(", html)

    def test_reads_only_the_block_storage_api(self):
        # "Could be freed" counts what frees quota: images never do
        self.render(views.SummaryView)
        self.inventory.images.assert_not_called()
        self.inventory.servers.assert_not_called()
        self.client.get_project_storage_trend.assert_not_called()

    def test_empty_project(self):
        self.empty_project()
        html = self.render(views.SummaryView)
        self.assertIn("0%", html)
        self.assertIn("none yet", html)
        self.assertIn("nothing to clean up", html)
        self.assertIn("ps-card ps-ok", html)

    def test_unlimited(self):
        block = fake_project_storage.block_storage()
        block["limits"]["gigabytes"] = None
        block["limits"]["volumes"] = None
        self.inventory.block_storage.return_value = block
        html = self.render(views.SummaryView)
        self.assertIn("840 GB", html)
        self.assertIn("no limit | volumes + snapshots", html)
        self.assertNotIn("<small> / None", html)

    def test_backend_error_hides_details(self):
        self.inventory.block_storage.side_effect = RuntimeError(
            "secret backend message"
        )
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.SummaryView)
        self.assertIn("Error loading data from the Block Storage API", html)
        self.assertNotIn("secret backend message", html)
        self.assertIn("data-ps-nodata", html)
        # an error is not retried quickly
        self.assertNotIn("ps-retry-", html)

    def test_still_computing_retries(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            html = self.render(views.SummaryView)
        self.assertIn("Still loading", html)
        self.assertIn("ps-retry-", html)
        self.assertIn("htmx.trigger(target, 'ps-retry')", html)

    def test_stale_copy(self):
        block = dict(
            fake_project_storage.block_storage(),
            fetched_at=1_700_000_000,
            is_stale_copy=True,
        )
        with mock.patch(f"{VIEWS}.cache.get_or_compute", return_value=block):
            html = self.render(views.SummaryView)
        self.assertIn("Could not be refreshed", html)
        self.assertIn('data-ts="1700000000"', html)

    def test_old_unmarked_copy_is_marked(self):
        block = dict(
            fake_project_storage.block_storage(),
            fetched_at=1_700_000_000,
            stored_at=1_700_000_000,
        )
        with mock.patch(f"{VIEWS}.cache.get_or_compute", return_value=block):
            context = self.render_context(views.SummaryView)
            html = self.render(views.SummaryView)
        self.assertTrue(context["data_old"])
        self.assertFalse(context["data_stale"])
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

    def test_invalid_setting(self):
        with self.settings(
            GRIAN_PLUGIN={
                "datasource": "fake",
                "project_storage_thresholds": {"quota_warning": "x"},
            }
        ):
            html = self.render(views.SummaryView)
        self.assertIn("invalid configuration", html)
        self.inventory.block_storage.assert_not_called()


class QuotaViewTests(_ViewTestCase):
    def test_rows_and_forecasts(self):
        context = self.render_context(views.QuotaView)
        rows = {r["key"]: r for r in context["rows"]}
        self.assertEqual(rows["gigabytes"]["used"], 840)
        self.assertEqual(
            rows["gigabytes"]["forecast"]["state"], ps.FORECAST_DAYS
        )
        self.assertEqual(
            rows["volumes"]["forecast"]["state"], ps.FORECAST_FLAT
        )
        self.assertNotIn("forecast", rows["backups"])
        self.assertEqual(context["forecast_days"], 15)

    def test_render(self):
        html = self.render(views.QuotaView)
        self.assertIn("Storage quota", html)
        self.assertIn("Block Storage API", html)
        self.assertIn("4 rows", html)
        self.assertIn("840 GB</strong>", html)
        self.assertIn("1000 GB <span class=\"text-muted\">(84%)</span>", html)
        self.assertRegex(html, r"Full in ~\d+ days")
        self.assertIn("Not growing", html)
        self.assertIn("Backup service not enabled", html)
        self.assertIn('class="ps-attached" style="flex: 0 0 19.0%;"', html)
        self.assertIn(
            "The forecast extends the trend of the last 15 days.", html
        )

    def test_forecast_history_follows_the_retention(self):
        self.render(views.QuotaView)
        self.assertEqual(
            self.client.get_project_storage_trend.call_args.args,
            (f'project="{P1}"', 15),
        )
        cache.clear()
        with self.settings(
            GRIAN_PLUGIN={"datasource": "fake", "metrics_retention_days": 60}
        ):
            html = self.render(views.QuotaView)
        self.assertEqual(
            self.client.get_project_storage_trend.call_args.args[1], 30
        )
        self.assertIn("the last 30 days", html)

    def test_not_enough_history(self):
        self.client.get_project_storage_trend.return_value = {
            "gigabytes": [[0, 1], [86400, 2]],
            "volumes": [],
            "snapshots": [],
        }
        html = self.render(views.QuotaView)
        self.assertIn("Not enough history", html)

    def test_forecast_failure_keeps_the_rows(self):
        self.client.get_project_storage_trend.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(views.QuotaView)
            html = self.render(views.QuotaView)
        self.assertTrue(context["forecast_unavailable"])
        self.assertFalse(context.get("loading"))
        self.assertIn("840 GB</strong>", html)
        self.assertIn("The forecasts could not be loaded.", html)
        self.assertNotIn("Full in", html)

    def test_unlimited(self):
        block = fake_project_storage.block_storage()
        block["limits"]["snapshots"] = None
        self.inventory.block_storage.return_value = block
        html = self.render(views.QuotaView)
        self.assertIn("no limit", html)

    def test_backups_enabled(self):
        block = dict(fake_project_storage.block_storage(), backup_enabled=True)
        self.inventory.block_storage.return_value = block
        html = self.render(views.QuotaView)
        self.assertNotIn("Backup service not enabled", html)

    def test_api_error(self):
        self.inventory.block_storage.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.QuotaView)
        self.assertIn("Error loading data from the Block Storage API", html)
        # the panel frame stays
        self.assertIn("Storage quota", html)


class FootprintViewTests(_ViewTestCase):
    def test_default_range_is_clamped_to_the_retention(self):
        context = self.render_context(views.FootprintView)
        self.assertEqual(context["range"], "30d")
        chart = context["chart"]
        self.assertEqual(len(chart["timestamps"]), 30)
        self.assertTrue(chart["beyond_retention"])
        # only the retention is queried, with the project's matcher
        args = self.client.get_project_storage_footprint.call_args
        self.assertEqual(args.args, (f'project="{P1}"', 15))
        self.assertIsNotNone(args.kwargs["at"])
        self.assertEqual(chart["volumes"][:15], [None] * 15)
        self.assertEqual(chart["limit"], 1000)

    def test_render_beyond_retention(self):
        html = self.render(views.FootprintView, range="90d")
        self.assertIn(
            "This cloud keeps 15 days of metrics: the chart starts on", html
        )
        self.assertIn('class="ps-date" data-ts="', html)
        data = _chart_data(html)
        self.assertEqual(data["range"], "90d")
        self.assertEqual(len(data["timestamps"]), 90)
        self.assertEqual(data["limit"], 1000)
        self.assertIn("Quota limit", html)
        self.assertIn("Stacked: the top of the area is the total", html)

    def test_range_within_retention(self):
        html = self.render(views.FootprintView, range="7d")
        self.assertNotIn("This cloud keeps", html)
        self.assertNotIn("History starts on", html)
        self.assertEqual(len(_chart_data(html)["timestamps"]), 7)
        self.assertEqual(
            self.client.get_project_storage_footprint.call_args.args[1], 7
        )

    def test_long_retention_queries_the_whole_range(self):
        with self.settings(
            GRIAN_PLUGIN={"datasource": "fake", "metrics_retention_days": 400}
        ):
            html = self.render(views.FootprintView, range="90d")
        self.assertEqual(
            self.client.get_project_storage_footprint.call_args.args[1], 90
        )
        self.assertNotIn("This cloud keeps", html)

    def test_history_shorter_than_the_range(self):
        now = 1_795_000_000
        self.client.get_project_storage_footprint.return_value = {
            "volumes": [[t, 10] for t in ps.day_grid(3, now)],
            "snapshots": [],
        }
        with mock.patch(f"{VIEWS}.time.time", return_value=now):
            html = self.render(views.FootprintView, range="7d")
        self.assertIn("History starts on", html)
        self.assertIn(f'data-ts="{ps.day_grid(3, now)[0]}"', html)

    def test_invalid_range_falls_back(self):
        context = self.render_context(views.FootprintView, range="2y")
        self.assertEqual(context["range"], "30d")

    def test_cached_per_range(self):
        self.render_context(views.FootprintView)
        self.render_context(views.FootprintView)
        self.assertEqual(
            self.client.get_project_storage_footprint.call_count, 1
        )
        self.render_context(views.FootprintView, range="7d")
        self.assertEqual(
            self.client.get_project_storage_footprint.call_count, 2
        )

    def test_empty_project(self):
        self.empty_project()
        html = self.render(views.FootprintView)
        self.assertIn(
            "No volumes, snapshots or private images in this project yet.",
            html,
        )
        self.assertIn("<code>volume_size</code>", html)
        self.assertIn("ps.destroyChart('footprint')", html)

    def test_images_but_no_volumes(self):
        self.empty_project()
        self.inventory.images.return_value = {
            "images": fake_project_storage.images(),
            "truncated": False,
        }
        html = self.render(views.FootprintView)
        self.assertIn("No volumes or snapshots in this project yet.", html)
        self.inventory.images.side_effect = RuntimeError("x")
        cache.clear()
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.FootprintView)
        self.assertIn("No volumes or snapshots in this project yet.", html)

    def test_volumes_without_metrics(self):
        self.client.get_project_storage_footprint.return_value = {
            "volumes": [],
            "snapshots": [],
        }
        html = self.render(views.FootprintView, range="7d")
        self.assertIn("No footprint data in the last 7 days.", html)

    def test_metrics_error(self):
        self.client.get_project_storage_footprint.side_effect = RuntimeError(
            "secret"
        )
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.FootprintView)
        self.assertIn("Error loading telemetry data", html)
        self.assertNotIn("secret", html)
        self.assertIn("ps.destroyChart('footprint')", html)

    def test_without_the_quota_limit(self):
        self.inventory.block_storage.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.FootprintView)
        self.assertIsNone(_chart_data(html)["limit"])
        self.assertIn("the quota limit is not drawn", html)
        self.assertNotIn("ps-ln ps-dash", html)


class CleanupViewTests(_ViewTestCase):
    def test_candidates(self):
        context = self.render_context(views.CleanupView)
        self.assertEqual(
            [c["name"] for c in context["candidates"]],
            [
                "logs-archive",
                "db-data-2026-09-01",
                "old-test-vol",
                "web-golden-2026-06",
                "web-assets-base",
            ],
        )
        self.assertEqual(context["total"], 5)

    def test_render(self):
        html = self.render(views.CleanupView)
        self.assertIn("max 10 rows", html)
        self.assertIn("Volume | unattached for 41 days", html)
        self.assertIn('class="ps-tag ps-tag-review">review', html)
        self.assertIn('class="ps-tag ps-tag-consider">consider', html)
        self.assertIn(
            'title="Only copy of this volume&#x27;s data">keep?', html
        )
        self.assertIn("not in quota", html)
        self.assertIn("Suggestions only.", html)
        self.assertNotIn(" shown.", html)

    def test_bounded(self):
        now = 1_795_000_000
        block = {
            "limits": dict(fake_project_storage.LIMITS),
            "volumes": [
                {
                    "id": f"v{i}",
                    "name": f"vol-{i:02d}",
                    "size": i + 1,
                    "status": "available",
                    "server_ids": [],
                    "created": now - 90 * 86400,
                    "updated": now - 40 * 86400,
                    "image_id": None,
                }
                for i in range(12)
            ],
            "snapshots": [],
            "backup_enabled": False,
        }
        self.inventory.block_storage.return_value = block
        self.inventory.images.return_value = {"images": [], "truncated": False}
        with mock.patch(f"{VIEWS}.time.time", return_value=now):
            context = self.render_context(views.CleanupView)
            html = self.render(views.CleanupView)
        self.assertEqual(len(context["candidates"]), 10)
        self.assertEqual(context["candidates"][0]["name"], "vol-11")
        self.assertIn("10 of 12 shown.", html)

    def test_image_api_failure_keeps_volume_candidates(self):
        self.inventory.images.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(views.CleanupView)
            html = self.render(views.CleanupView)
        self.assertEqual(len(context["candidates"]), 4)
        self.assertIn("The Image API could not be read", html)
        self.inventory.servers.assert_not_called()

    def test_shared_image_is_not_a_candidate(self):
        images = fake_project_storage.images()
        for image in images:
            image["visibility"] = "shared"
        self.inventory.images.return_value = {
            "images": images,
            "truncated": False,
        }
        context = self.render_context(views.CleanupView)
        self.assertNotIn("image", [c["kind"] for c in context["candidates"]])
        self.assertEqual(len(context["candidates"]), 4)

    def test_compute_api_failure_skips_images(self):
        self.inventory.servers.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(views.CleanupView)
            html = self.render(views.CleanupView)
        self.assertNotIn("image", [c["kind"] for c in context["candidates"]])
        self.assertIn("unused images are not checked", html)

    def test_optional_data_loading_retries(self):
        def get_or_compute(key, ttl, compute):
            if ":images:" in key:
                raise grian_cache.StillComputing("busy")
            return compute()

        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=get_or_compute
        ):
            context = self.render_context(views.CleanupView)
            html = self.render(views.CleanupView)
        self.assertTrue(context["loading"])
        self.assertIn("Loading the private images", html)
        self.assertIn("ps-retry-", html)

    def test_empty(self):
        self.empty_project()
        html = self.render(views.CleanupView)
        self.assertIn("Nothing to clean up.", html)

    def test_api_error(self):
        self.inventory.block_storage.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.CleanupView)
        self.assertIn("Error loading data from the Block Storage API", html)


class VolumesViewTests(_ViewTestCase):
    def test_rows(self):
        context = self.render_context(views.VolumesView)
        rows = context["rows"]
        self.assertEqual(context["total"], 8)
        self.assertEqual(rows[0]["name"], "logs-archive")
        self.assertEqual(rows[1]["servers"][0]["name"], "db-01")
        self.assertEqual(rows[-1]["kind"], "snapshot")

    def test_render(self):
        html = self.render(views.VolumesView)
        self.assertIn("max 25 rows", html)
        self.assertIn("in-use | db-01", html)
        self.assertIn(
            "available <small class=\"text-muted\">(unattached 41 d)", html
        )
        self.assertIn("Snapshot of db-data", html)
        self.assertIn("210 d", html)
        # no Horizon volume pages here: plain names
        self.assertNotIn("<a ", html)

    def test_links_when_the_panels_exist(self):
        self.mock_url.side_effect = lambda name, *args: (
            f"/{name}/{'/'.join(args)}"
        )
        html = self.render(views.VolumesView)
        self.assertIn(
            'href="/horizon:project:volumes:detail/'
            f'{fake_project_storage._id("db-data")}"',
            html,
        )
        self.assertIn('href="/horizon:project:snapshots:detail/', html)
        self.assertIn(
            'href="/horizon:project:instance_metrics:index/?instance='
            f'{fake_project_storage._id("db-01")}"',
            html,
        )

    def test_bounded(self):
        block = fake_project_storage.block_storage()
        block["volumes"] = block["volumes"] * 6
        self.inventory.block_storage.return_value = block
        context = self.render_context(views.VolumesView)
        html = self.render(views.VolumesView)
        self.assertEqual(len(context["rows"]), 25)
        self.assertIn("25 of 33 shown, largest first.", html)

    def test_compute_api_failure_shows_ids(self):
        self.inventory.servers.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.VolumesView)
        self.assertIn(
            f"in-use | {fake_project_storage._id('db-01')[:8]}", html
        )
        self.assertIn("instances are shown by ID", html)

    def test_no_compute_call_without_attachments(self):
        block = fake_project_storage.block_storage()
        for volume in block["volumes"]:
            volume["server_ids"] = []
            volume["status"] = "available"
        self.inventory.block_storage.return_value = block
        self.render(views.VolumesView)
        self.inventory.servers.assert_not_called()

    def test_error_status(self):
        block = fake_project_storage.block_storage()
        block["volumes"][0]["status"] = "error_extending"
        self.inventory.block_storage.return_value = block
        html = self.render(views.VolumesView)
        self.assertIn('<span class="text-danger">error_extending</span>', html)

    def test_empty(self):
        self.empty_project()
        html = self.render(views.VolumesView)
        self.assertIn("No volumes or snapshots.", html)
        self.assertNotIn("Create a volume", html)
        self.mock_url.side_effect = lambda name, *args: f"/{name}"
        html = self.render(views.VolumesView)
        self.assertIn(
            '<a class="ps-link" href="/horizon:project:volumes:create">'
            "Create a volume…</a>",
            html,
        )

    def test_api_error(self):
        self.inventory.block_storage.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.VolumesView)
        self.assertIn("Error loading data from the Block Storage API", html)


class ImagesViewTests(_ViewTestCase):
    def test_render(self):
        html = self.render(views.ImagesView)
        self.assertIn("Private images", html)
        self.assertIn("Image API", html)
        self.assertIn("batch-worker", html)
        self.assertIn("70 days old", html)
        self.assertIn("6 instances", html)
        self.assertIn('<span class="text-warning">none</span>', html)
        self.assertIn("Total 7.5 GB in the image store.", html)

    def test_compute_api_failure(self):
        self.inventory.servers.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.ImagesView)
        self.assertIn("unknown", html)
        self.assertIn("unused images are not checked", html)

    def test_block_storage_failure_makes_users_unknown(self):
        # instances booted from a volume use the volume's image: without
        # the volumes, a count would miss them (0 instead of unknown)
        self.inventory.block_storage.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(views.ImagesView)
            html = self.render(views.ImagesView)
        self.assertEqual([r["users"] for r in context["rows"]], [None] * 3)
        self.assertNotIn("6 instances", html)
        self.assertNotIn('<span class="text-warning">none</span>', html)
        self.assertIn('<span class="text-muted">unknown</span>', html)
        self.assertIn("the instances using each image are not known", html)

    def test_shared_image_use_by_other_projects_is_unknown(self):
        images = fake_project_storage.images()
        for image in images:
            image["visibility"] = "shared"
        self.inventory.images.return_value = {
            "images": images,
            "truncated": False,
        }
        context = self.render_context(views.ImagesView)
        html = self.render(views.ImagesView)
        users = {r["name"]: (r["users"], r["shared"]) for r in context["rows"]}
        self.assertEqual(users["web-golden-2026-06"], (0, True))
        self.assertEqual(users["web-golden-2026-10"], (6, True))
        self.assertNotIn('<span class="text-warning">none</span>', html)
        self.assertIn(
            '<span class="text-muted">shared, use by other projects '
            "unknown</span>",
            html,
        )
        self.assertIn("6 instances<br><small", html)

    def test_shared_image_with_unknown_users_keeps_the_hint(self):
        images = fake_project_storage.images()
        for image in images:
            image["visibility"] = "shared"
        self.inventory.images.return_value = {
            "images": images,
            "truncated": False,
        }
        self.inventory.block_storage.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(views.ImagesView)
            html = self.render(views.ImagesView)
        self.assertEqual([r["users"] for r in context["rows"]], [None] * 3)
        self.assertEqual(
            html.count(
                '<span class="text-muted">unknown</span><br>'
                '<small class="text-muted">shared, use by other projects '
                "unknown</small>"
            ),
            3,
        )

    def test_failed_and_loading_notes_are_told_apart(self):
        def get_or_compute(key, ttl, compute):
            if ":servers:" in key:
                raise grian_cache.ComputationFailed("down")
            if ":block_storage:" in key:
                raise grian_cache.StillComputing("busy")
            return compute()

        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=get_or_compute
        ):
            context = self.render_context(views.ImagesView)
            html = self.render(views.ImagesView)
        self.assertTrue(context["loading"])
        self.assertEqual(
            [n["loading"] for n in context["partial_notes"]], [False, True]
        )
        self.assertIn(
            '<p class="text-warning" style="margin: 6px 0 0;"><i class="fa '
            'fa-exclamation-triangle" aria-hidden="true"></i> The Compute '
            "API could not be read",
            html,
        )
        self.assertIn(
            '<p class="text-muted" style="margin: 6px 0 0;"><i class="fa '
            'fa-spinner fa-spin" aria-hidden="true"></i> Loading the volumes',
            html,
        )

    def test_bounded_and_truncated(self):
        images = fake_project_storage.images() * 10
        self.inventory.images.return_value = {
            "images": images,
            "truncated": True,
        }
        html = self.render(views.ImagesView)
        self.assertIn("25 of 30 shown, largest first.", html)
        self.assertIn("The Image API returned only part of the list.", html)

    def test_empty(self):
        self.empty_project()
        html = self.render(views.ImagesView)
        self.assertIn("No private images.", html)
        self.inventory.servers.assert_not_called()

    def test_image_api_error(self):
        self.inventory.images.side_effect = RuntimeError("secret")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.ImagesView)
        self.assertIn("Error loading data from the Image API", html)
        self.assertNotIn("secret", html)


class ProjectIsolationTests(_ViewTestCase):
    """Two projects never share a cached value; one project shares all."""

    def refresh_page(self, project_id):
        for name in PARTIALS:
            self.render(getattr(views, name), project_id)

    def test_partials_of_one_project_share_the_api_lists(self):
        self.refresh_page(P1)
        self.refresh_page(P1)
        self.assertEqual(self.inventory.block_storage.call_count, 1)
        self.assertEqual(self.inventory.servers.call_count, 1)
        self.assertEqual(self.inventory.images.call_count, 1)
        self.assertEqual(
            self.client.get_project_storage_footprint.call_count, 1
        )
        self.assertEqual(self.client.get_project_storage_trend.call_count, 1)

    def test_projects_do_not_share(self):
        self.refresh_page(P1)
        self.refresh_page(P2)
        self.assertEqual(self.inventory.block_storage.call_count, 2)
        self.assertEqual(self.inventory.images.call_count, 2)
        matchers = [
            c.args[0]
            for c in self.client.get_project_storage_footprint.call_args_list
        ]
        self.assertEqual(matchers, [f'project="{P1}"', f'project="{P2}"'])

    def test_keys_hold_the_project(self):
        keys = []
        real = grian_cache.get_or_compute

        def spy(key, ttl, compute):
            keys.append(key)
            return real(key, ttl, compute)

        with (
            mock.patch(f"{VIEWS}.cache.get_or_compute", side_effect=spy),
            mock.patch(
                f"{VIEWS}.cache.cache_key",
                side_effect=AssertionError("region-wide key"),
            ),
        ):
            self.refresh_page(P1)
        self.assertTrue(keys)
        for key in keys:
            self.assertTrue(key.startswith("grian_ui:project_storage:v1:"))
            self.assertTrue(key.endswith(f":project:{P1}"), key)

    def test_no_project_is_an_error(self):
        with self.assertLogs(VIEWS, "ERROR"):
            html = self.render(views.SummaryView, project_id=None)
        self.assertIn("data-ps-nodata", html)
        self.inventory.block_storage.assert_not_called()

    def test_invalid_project_id_never_reaches_a_query(self):
        with self.assertLogs(VIEWS, "ERROR"):
            html = self.render(
                views.FootprintView, project_id='x"} or vector(1)'
            )
        self.assertIn("Error loading telemetry data", html)
        self.client.get_project_storage_footprint.assert_not_called()


class LinkTests(base.TestCase):
    def test_instance_link(self):
        base_url = "/dashboard/project/instance_metrics/"
        self.assertEqual(
            views.instance_link("abc", base_url), base_url + "?instance=abc"
        )
        self.assertEqual(
            views.instance_link("a&b", base_url), base_url + "?instance=a%26b"
        )
        self.assertIsNone(views.instance_link("abc", None))
        self.assertIsNone(views.instance_link(None, base_url))

    def test_url_both_ways(self):
        with mock.patch(f"{VIEWS}.reverse", return_value="/x/") as rev:
            self.assertEqual(
                _REAL_URL("horizon:project:volumes:detail", "v"), "/x/"
            )
        rev.assert_called_once_with(
            "horizon:project:volumes:detail", args=("v",)
        )
        with mock.patch(
            f"{VIEWS}.reverse", side_effect=NoReverseMatch("not installed")
        ):
            self.assertIsNone(_REAL_URL("horizon:project:volumes:detail", "v"))

    def test_instance_metrics_contract(self):
        with mock.patch(f"{VIEWS}.reverse", return_value="/im/") as rev:
            self.assertEqual(views.instance_metrics_url(), "/im/")
        rev.assert_called_once_with(
            "horizon:project:instance_metrics:index", args=()
        )
        with mock.patch(
            f"{VIEWS}.reverse", side_effect=NoReverseMatch("not installed")
        ):
            self.assertIsNone(views.instance_metrics_url())


class EnabledFileTests(base.TestCase):
    def test_enabled_file(self):
        module = importlib.import_module(
            "grian_ui.local.enabled._1102_project_telemetry_storage_panel"
        )
        self.assertEqual(module.PANEL, "telemetry_storage")
        self.assertEqual(module.PANEL_DASHBOARD, "project")
        self.assertEqual(module.PANEL_GROUP, "telemetry")
        self.assertEqual(
            module.ADD_PANEL,
            "grian_ui.content.project_storage.panel.ProjectStorage",
        )
        self.assertFalse(hasattr(module, "ADD_INSTALLED_APPS"))


class PanelPermissionsTests(base.TestCase):
    def test_requires_the_metric_storage_service(self):
        from grian_ui.content.project_storage import panel

        cls = next(
            c
            for c in vars(panel).values()
            if isinstance(c, type) and getattr(c, "slug", None)
        )
        self.assertIn("openstack.services.metric-storage", cls.permissions)
