#
# SPDX-License-Identifier: Apache-2.0

import time

from unittest import mock

from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test import override_settings

from grian_ui.content.volume_metrics import views
from grian_ui.datasource import fake
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
    views.ChartUpdateView,
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


class PoolTotalsTests(base.TestCase):
    def test_totals(self):
        pools = fake.get_client().get_storage_pools()
        self.assertEqual(
            views.pool_totals(pools),
            {
                "total_storage": 12288,
                "allocated_storage": 6656,
                "pools_total": 3,
                "pools_stale": 1,
            },
        )

    def test_no_pools(self):
        self.assertEqual(views.pool_totals([])["total_storage"], 0)


@override_settings(TEMPLATES=INDEX_TEMPLATES, STATIC_URL="/static/")
class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context, html = self.render(views.VolumesMetricsIndexView)
        self.assertEqual(context["page_title"], "Volume Metrics")
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
        self.assertEqual(context["overview"]["total_storage"], 12288)
        self.assertEqual(context["volumes"], 120)
        self.assertEqual(context["snapshots"], 45)
        self.assertIn("3 pools", html)
        self.assertIn("1 stale", html)

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
        self.assertEqual(context["overview"]["total_storage"], 12288)

    def test_old_copy_is_flagged(self):
        stored_at = time.time() - views.POOLS_CACHE_TTL - 31
        with mock.patch(f"{VIEWS}.cache.get_or_compute") as get:
            get.return_value = {
                "pools": [],
                "volumes": 0,
                "snapshots": 0,
                "fetched_at": stored_at,
                "stored_at": stored_at,
            }
            context, html = self.render(views.MetricsUpdateView)
        self.assertTrue(context["data_old"])
        self.assertIn("may be out of date", html)

    def test_still_computing(self):
        with mock.patch(f"{VIEWS}.cache.get_or_compute") as get:
            get.side_effect = views.cache.StillComputing("k")
            context, html = self.render(views.ChartUpdateView)
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
            views.ChartUpdateView.template_name,
            "admin/volume_metrics/_storage_chart.html",
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
