#
# SPDX-License-Identifier: Apache-2.0

import time

from unittest import mock

from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test import override_settings

from grian_ui import storage_inventory
from grian_ui.content.volume_metrics import views
from grian_ui.datasource import fake
from grian_ui.datasource import fake_storage
from grian_ui_tests.unit import base
from grian_ui_tests.unit import settings as test_settings


VIEWS = "grian_ui.content.volume_metrics.views"

#: Render the index page without horizon: stub its base templates.
INDEX_TEMPLATES = [
    dict(
        test_settings.TEMPLATES[0],
        OPTIONS={
            "loaders": [
                (
                    "django.template.loaders.locmem.Loader",
                    {
                        "base.html": "{% block page_header %}{% endblock %}"
                        "{% block main %}{% endblock %}",
                        "horizon/common/_page_header.html": "{{ title }}",
                    },
                ),
                "django.template.loaders.filesystem.Loader",
            ]
        },
    )
]

PARTIALS = (
    views.MetricsUpdateView,
    views.AttentionView,
    views.CapacityPredictionView,
)


class _ViewTestCase(base.TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch(f"{VIEWS}.get_datasource")
        self.mock_get_ds = patcher.start()
        self.addCleanup(patcher.stop)
        # wrap the fake client so calls can be asserted
        self.client = mock.MagicMock(wraps=fake.get_client())
        self.mock_get_ds.return_value = self.client
        patcher = mock.patch(f"{VIEWS}.storage_inventory.get_inventory")
        self.mock_get_inv = patcher.start()
        self.addCleanup(patcher.stop)
        self.inventory = mock.MagicMock(
            wraps=storage_inventory.FakeStorageInventory()
        )
        self.mock_get_inv.return_value = self.inventory

    def render_context(self, view_class, **params):
        request = RequestFactory().get("/", params)
        request.user = mock.MagicMock(services_region="RegionOne")
        view = view_class()
        view.setup(request)
        return view.get_context_data()

    def render(self, view_class, **params):
        context = self.render_context(view_class, **params)
        return context, render_to_string(view_class.template_name, context)

    def cache_key(self, name):
        request = mock.MagicMock()
        request.user.services_region = "RegionOne"
        return views.cache.cache_key(
            request, views.CACHE_NAMESPACE, name, version=views.CACHE_VERSION
        )


@override_settings(TEMPLATES=INDEX_TEMPLATES, STATIC_URL="/static/")
class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context, html = self.render(views.VolumesMetricsIndexView)
        self.assertEqual(context["page_title"], "Storage Health")
        self.assertEqual(context["refresh_seconds"], 60)
        pools = context["storage_pools"]
        self.assertEqual(len(pools), 3)
        self.assertEqual(pools[0]["usage_percent"], 80)
        self.assertTrue(pools[2]["stale"])
        self.assertTrue(pools[2]["last_seen_ts"].isdigit())
        self.assertIn("Stale", html)
        self.assertIn("every 60s", html)
        self.assertNotIn("every 10s", html)
        self.assertIn("data_ts", context)

    def test_error_shows_a_plain_message(self):
        self.client.get_storage_pools.side_effect = Exception(
            "secret backend detail"
        )
        with self.assertLogs(level="ERROR"):
            context, html = self.render(views.VolumesMetricsIndexView)
        self.assertEqual(context["storage_pools"], [])
        self.assertIn("Error loading telemetry data", html)
        self.assertNotIn("secret backend detail", html)


class PartialTests(_ViewTestCase):
    def test_every_partial_renders(self):
        for view_class in PARTIALS:
            context, html = self.render(view_class)
            self.assertNotIn("error_message", context, view_class)
            self.assertNotIn("data-vm-nodata", html, view_class)
            self.assertIn('class="vm-clock"', html, view_class)

    def test_cards(self):
        context, html = self.render(views.MetricsUpdateView)
        summary = context["summary"]
        self.assertEqual(summary["total"], 12288)
        self.assertEqual(context["volumes"], 120)
        self.assertEqual(context["snapshots"], 45)
        self.assertIn("3 pools", html)
        self.assertIn("1 stale", html)
        # 6656 of 12288 GiB allocated in the cloud
        self.assertEqual(summary["allocated_level"], "ok")
        self.assertIn("vm-card vm-ok", html)
        # 3 critical and 1 stuck volume or snapshot
        self.assertIn("4 need attention", html)
        self.assertIn("vm-card vm-critical", html)
        self.assertIn("2 clean-up candidates", html)

    def test_forecast_shows_the_limiting_factor(self):
        _context, html = self.render(views.CapacityPredictionView)
        self.assertIn("Full in 24.5 days (virtual)", html)
        self.assertIn("Declining: full in 51.2 days (physical)", html)

    def test_forecast_without_history(self):
        prediction = {
            "name": "pool",
            "current_free": 28.5,
            "current_virtual_free": 568.0,
            "days_physical": None,
            "days_virtual": None,
            "days_until_full": None,
            "limiting": None,
            "will_run_out": False,
            "history_days": 1.2,
            "enough_history": False,
        }
        with mock.patch.object(
            self.client, "get_capacity_predictions", return_value=[prediction]
        ):
            _context, html = self.render(views.CapacityPredictionView)
        self.assertIn("Insufficient data (1.2 of 7 days)", html)

    def test_data_is_shared(self):
        self.render_context(views.VolumesMetricsIndexView)
        for view_class in PARTIALS:
            self.render_context(view_class)
        self.assertEqual(self.client.get_storage_pools.call_count, 1)
        self.assertEqual(self.client.get_volumes_count.call_count, 1)
        self.assertEqual(self.client.get_snapshots_count.call_count, 1)
        self.assertEqual(self.client.get_capacity_predictions.call_count, 1)

    def test_failure_without_a_copy(self):
        self.client.get_storage_pools.side_effect = Exception("boom")
        with self.assertLogs(level="ERROR"):
            context, html = self.render(views.MetricsUpdateView)
        self.assertIn("data-vm-nodata", html)
        self.assertIn("Error loading telemetry data", html)
        self.assertNotIn("boom", html)

    def test_failure_shows_the_last_good_copy(self):
        self.render_context(views.MetricsUpdateView)
        self.client.get_storage_pools.side_effect = Exception("boom")
        # expire the value, keep the last good copy
        cache.delete(self.cache_key("pools"))
        with self.assertLogs(level="ERROR"):
            context, html = self.render(views.MetricsUpdateView)
        self.assertNotIn("error_message", context)
        self.assertTrue(context["data_stale"])
        self.assertIn("Could not be refreshed", html)
        self.assertIn('class="vm-age"', html)
        self.assertNotIn("boom", html)
        self.assertEqual(context["summary"]["total"], 12288)

    def test_old_copy_is_flagged(self):
        real = views.cache.get_or_compute
        stored_at = time.time() - views.POOLS_CACHE_TTL - 31

        def old_pools(key, ttl, compute):
            if ":pools:" not in key:
                return real(key, ttl, compute)
            return dict(compute(), fetched_at=stored_at, stored_at=stored_at)

        with mock.patch(f"{VIEWS}.cache.get_or_compute", old_pools):
            context, html = self.render(views.MetricsUpdateView)
        self.assertTrue(context["data_old"])
        self.assertIn("may be out of date", html)

    def test_still_computing(self):
        with mock.patch(f"{VIEWS}.cache.get_or_compute") as get:
            get.side_effect = views.cache.StillComputing("k")
            context, html = self.render(views.AttentionView)
        self.assertTrue(context["loading"])
        self.assertIn("vm-retry", html)

    @override_settings(GRIAN_PLUGIN={"datasource": "prometheus"})
    def test_configuration_error(self):
        with self.assertLogs(level="ERROR"):
            _context, html = self.render(views.CapacityPredictionView)
        self.assertIn("invalid configuration", html)
        self.client.get_capacity_predictions.assert_not_called()

    def test_template_names(self):
        self.assertEqual(
            views.MetricsUpdateView.template_name,
            "admin/volume_metrics/_metrics_cards.html",
        )
        self.assertEqual(
            views.AttentionView.template_name,
            "admin/volume_metrics/_attention.html",
        )
        self.assertEqual(
            views.CapacityPredictionView.template_name,
            "admin/volume_metrics/_capacity_prediction.html",
        )

    @override_settings(
        GRIAN_PLUGIN={"datasource": "fake", "storage_stale_minutes": 0}
    )
    def test_invalid_stale_minutes(self):
        with self.assertLogs(level="ERROR"):
            _context, html = self.render(views.MetricsUpdateView)
        self.assertIn("invalid configuration", html)
        self.client.get_storage_pools.assert_not_called()


class AttentionTests(_ViewTestCase):
    def test_rows(self):
        context, html = self.render(views.AttentionView)
        rows = context["rows"]
        self.assertEqual(
            [r["name"] for r in rows],
            [
                "ci-cache-112",
                "ci-snap",
                "pg-data-07",
                "ml-ckpt-31",
                "erp-db-2026-08",
                "erp-archive-2025",
            ],
        )
        self.assertEqual(rows[0]["project_name"], "ci-runners")
        self.assertEqual(rows[0]["for_text"], "3 d")
        self.assertEqual(rows[3]["for_text"], "47 min")
        # no horizon in the unit tests: no detail links
        self.assertIsNone(rows[0]["url"])
        self.assertIn("3 critical, 1 warning, 2 clean-up.", html)
        self.assertIn("Not listed yet:", html)
        self.assertIn("web-tmp (unattached 2.0 days)", html)
        self.assertIn("Deletion failed", html)
        self.assertIn("Stuck for more than 30 minutes", html)
        self.assertIn("Block Storage API data from", html)
        self.assertIn("(refreshed every 2 minutes)", html)

    def test_nothing_needs_attention(self):
        with (
            mock.patch.object(self.inventory, "list_volumes", return_value=[]),
            mock.patch.object(
                self.inventory, "list_snapshots", return_value=[]
            ),
        ):
            context, html = self.render(views.AttentionView)
        self.assertEqual(context["rows"], [])
        self.assertIn("nothing to clean up", html)

    def test_twenty_five_rows_are_shown(self):
        volumes = [
            dict(v, id=f"v{i}", name=f"vol-{i}", status="error")
            for i, v in enumerate(fake_storage.fake_volumes() * 5)
        ]
        with mock.patch.object(
            self.inventory, "list_volumes", return_value=volumes
        ):
            context, html = self.render(views.AttentionView)
        self.assertEqual(len(context["rows"]), 25)
        self.assertEqual(context["attention"]["total"], 42)
        self.assertEqual(len(context["attention"]["rows"]), 42)
        self.assertIn("25 of 42 shown, worst first.", html)

    def test_project_names_are_optional(self):
        with mock.patch.object(
            self.inventory, "project_names", side_effect=Exception("down")
        ):
            with self.assertLogs(level="ERROR"):
                context, html = self.render(views.AttentionView)
        self.assertEqual(context["rows"][0]["project_name"], "p-ci")
        self.assertIn("showing project IDs", html)

    def test_block_storage_api_failure(self):
        with mock.patch.object(
            self.inventory, "list_volumes", side_effect=Exception("secret")
        ):
            with self.assertLogs(level="ERROR"):
                context, html = self.render(views.AttentionView)
        self.assertIn("Block Storage API", context["error_message"])
        self.assertNotIn("secret", html)

    def test_cinder_is_read_once_per_inventory_lifetime(self):
        for _i in range(3):
            for view_class in PARTIALS:
                self.render_context(view_class)
        self.assertEqual(self.inventory.list_volumes.call_count, 1)
        self.assertEqual(self.inventory.list_snapshots.call_count, 1)
        self.assertEqual(self.inventory.project_names.call_count, 1)

    def test_index_never_calls_cinder(self):
        with override_settings(
            TEMPLATES=INDEX_TEMPLATES, STATIC_URL="/static/"
        ):
            self.render(views.VolumesMetricsIndexView)
        self.mock_get_inv.assert_not_called()


class VerdictTests(_ViewTestCase):
    def verdict(self):
        context, html = self.render(views.MetricsUpdateView)
        return context["summary"]["verdict"], html

    def test_pools_nearly_full(self):
        verdict, html = self.verdict()
        # storage-pool-02 is forecast full in 24.5 days: a warning;
        # storage-pool-01 is 80% allocated: a warning; a stale pool
        self.assertEqual(verdict["key"], "pools_stale")
        self.assertIn("1 pool stopped reporting", html)
        self.assertIn("storage-pool-03", html)

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "storage_thresholds": {"physical_critical": 80},
        }
    )
    def test_threshold_override(self):
        verdict, html = self.verdict()
        self.assertEqual(verdict["key"], "pools_full")
        self.assertEqual(verdict["level"], "critical")
        self.assertIn("1 pool nearly full", html)

    def test_inventory_failure_keeps_the_metrics(self):
        with mock.patch.object(
            self.inventory, "list_volumes", side_effect=Exception("down")
        ):
            with self.assertLogs(level="ERROR"):
                context, html = self.render(views.MetricsUpdateView)
        self.assertNotIn("error_message", context)
        self.assertIsNone(context["summary"]["attention"])
        self.assertIn("attention not checked", html)
        self.assertIn("Block Storage API data could not be loaded", html)

    def test_healthy(self):
        pools = [
            {
                "name": "devstack@lvm#lvm",
                "total": 28.5,
                "allocated": 1.0,
                "free": 28.5,
                "provisioned": 2.0,
                "virtual_free": 568.0,
                "last_seen": time.time(),
                "stale": False,
            }
        ]
        prediction = {
            "name": "devstack@lvm#lvm",
            "current_free": 28.5,
            "current_virtual_free": 568.0,
            "days_physical": None,
            "days_virtual": None,
            "days_until_full": None,
            "limiting": None,
            "will_run_out": False,
            "history_days": 1.2,
            "enough_history": False,
        }
        with (
            mock.patch.object(
                self.client, "get_storage_pools", return_value=pools
            ),
            mock.patch.object(
                self.client,
                "get_capacity_predictions",
                return_value=[prediction],
            ),
            mock.patch.object(self.inventory, "list_volumes", return_value=[]),
            mock.patch.object(
                self.inventory, "list_snapshots", return_value=[]
            ),
        ):
            verdict, html = self.verdict()
        self.assertEqual(verdict["key"], "healthy")
        self.assertIn("Storage is healthy", html)
        self.assertIn("the pools have 1.2 days", html)
        self.assertIn("4% physical | 0% of the virtual limit", html)
