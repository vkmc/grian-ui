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
    views.CapacityView,
    views.PoolsView,
    views.ServicesView,
    views.BackendsView,
    views.ProjectsView,
    views.ImagesView,
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
        self.assertEqual(context["ranges"], ["24h", "7d", "14d"])
        self.assertEqual(context["default_range"], "7d")
        self.assertNotIn("config_error", context)
        self.assertIn("every 60s", html)
        self.assertNotIn("every 10s", html)
        for name in (
            "metrics_update",
            "attention",
            "capacity",
            "pools",
            "pool_lookup",
            "pool_suggestions",
            "services",
            "backends",
            "projects",
            "images",
        ):
            self.assertIn("/dashboard/admin/volume_metrics/", html)
            self.assertIn(
                views.reverse(f"horizon:admin:volume_metrics:{name}"), html
            )
        self.assertIn('<details class="panel panel-default vm-how"', html)
        self.assertIn("GRIAN_PLUGIN['storage_thresholds']", html)
        # the replaced panels are gone
        self.assertNotIn("Detailed Pool Information", html)
        self.assertNotIn("Capacity Forecast", html)
        self.assertNotIn("Volume and Snapshot Distribution", html)

    def test_the_shell_queries_nothing(self):
        self.render(views.VolumesMetricsIndexView)
        self.mock_get_ds.assert_not_called()
        self.mock_get_inv.assert_not_called()

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "storage_thresholds": {"virtual_critical": 101},
        }
    )
    def test_configuration_error_is_shown_once(self):
        context, html = self.render(views.VolumesMetricsIndexView)
        self.assertIn("virtual_critical", context["config_error"])
        self.assertIn("alert-danger", html)


class PartialTests(_ViewTestCase):
    def test_every_partial_renders(self):
        for view_class in PARTIALS:
            context, html = self.render(view_class)
            self.assertNotIn("error_message", context, view_class)
            self.assertNotIn("data-vm-nodata", html, view_class)
            self.assertIn("vm-clock", html, view_class)

    def test_cards(self):
        context, html = self.render(views.MetricsUpdateView)
        summary = context["summary"]
        self.assertEqual(summary["total"], 12288)
        self.assertEqual(context["volumes"], 120)
        self.assertEqual(context["snapshots"], 45)
        self.assertIn("3 pools", html)
        self.assertIn("1 stale", html)
        # cinder-volume on ctl-02@nfs is down
        self.assertEqual(summary["verdict"]["key"], "service_down")
        self.assertIn("Cinder service down: cinder-volume on ctl-02@nfs", html)
        self.assertIn("1 pool is stale", html)
        self.assertIn(
            "Separately, ctl-01@lvm#storage-pool-02 fills up in about 25 "
            "days (virtual capacity).",
            html,
        )
        # 6656 of 12288 GiB allocated in the cloud
        self.assertEqual(summary["allocated_level"], "ok")
        self.assertIn("vm-card vm-ok", html)
        # 3 critical and 1 stuck volume or snapshot
        self.assertIn("4 need attention", html)
        self.assertIn("vm-card vm-critical", html)
        self.assertIn("2 clean-up candidates", html)

    def test_data_is_shared(self):
        self.render_context(views.VolumesMetricsIndexView)
        for view_class in PARTIALS:
            self.render_context(view_class)
        self.assertEqual(self.client.get_storage_pools.call_count, 1)
        self.assertEqual(self.client.get_volumes_count.call_count, 1)
        self.assertEqual(self.client.get_snapshots_count.call_count, 1)
        self.assertEqual(self.client.get_capacity_predictions.call_count, 1)
        self.assertEqual(self.client.get_volume_services.call_count, 1)
        self.assertEqual(self.client.get_volume_service_history.call_count, 1)
        self.assertEqual(self.client.get_storage_capacity_trend.call_count, 1)
        self.assertEqual(self.client.get_storage_images.call_count, 1)

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
            _context, html = self.render(views.PoolsView)
        self.assertIn("invalid configuration", html)
        self.client.get_storage_pools.assert_not_called()

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
            views.PoolsView.template_name, "admin/volume_metrics/_pools.html"
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


class VerdictTests(_ViewTestCase):
    def verdict(self):
        context, html = self.render(views.MetricsUpdateView)
        return context["summary"]["verdict"], html

    def services_up(self):
        services = [
            dict(s, up=True) for s in fake.fake_storage.fake_services()
        ]
        return mock.patch.object(
            self.client, "get_volume_services", return_value=services
        )

    def test_stale_pool(self):
        with self.services_up():
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
        with self.services_up():
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
            self.services_up(),
        ):
            verdict, html = self.verdict()
        self.assertEqual(verdict["key"], "healthy")
        self.assertIn("All 3 Cinder services up", html)
        self.assertIn("Storage is healthy", html)
        self.assertIn("the pools have 1.2 days", html)
        self.assertIn("4% physical | 0% of the virtual limit", html)


class CapacityTests(_ViewTestCase):
    def test_whole_cloud(self):
        context, html = self.render(views.CapacityView)
        self.assertEqual(context["range_key"], "7d")
        chart = context["chart"]
        self.assertEqual(
            [s["name"] for s in chart["series"]],
            ["allocated", "provisioned", "total"],
        )
        self.assertEqual(len(chart["labels"]), 168)
        self.assertEqual(len(chart["series"][0]["values"]), 168)
        self.assertEqual(chart["series"][2]["values"][-1], 12288.0)
        self.assertIn("vm-capacity-data-", html)
        self.assertIn("(refreshed every 5 minutes)", html)
        self.client.get_storage_capacity_trend.assert_called_once_with(
            "7d", pools=None, at=mock.ANY
        )

    def test_fullest_pools(self):
        context, html = self.render(
            views.CapacityView, view="pools", range="24h"
        )
        chart = context["chart"]
        self.assertEqual(len(chart["labels"]), 96)
        # stale pools are left out
        self.assertEqual(
            [s["name"] for s in chart["series"]],
            ["ctl-01@lvm#storage-pool-01", "ctl-01@lvm#storage-pool-02"],
        )
        self.assertEqual(chart["warning"], 75)
        self.assertIn("Physical use (allocated ÷ total)", html)

    def test_fullest_pools_flag_a_stale_snapshot(self):
        self.render_context(views.CapacityView, view="pools")
        self.client.get_storage_pools.side_effect = Exception("boom")
        # expire the pools, keep their last good copy
        cache.delete(self.cache_key("pools"))
        with self.assertLogs(level="ERROR"):
            context, html = self.render(views.CapacityView, view="pools")
        self.assertTrue(context["data_stale"])
        self.assertIn("Could not be refreshed", html)

    def test_invalid_choices_fall_back(self):
        context = self.render_context(
            views.CapacityView, view="nope", range="1y"
        )
        self.assertEqual(context["range_key"], "7d")
        self.assertEqual(context["view"], "cloud")

    def test_no_data(self):
        with mock.patch.object(
            self.client, "get_storage_capacity_trend", return_value={}
        ):
            context, html = self.render(views.CapacityView)
        self.assertFalse(context["has_data"])
        self.assertIn("No capacity reported", html)

    def test_each_range_and_view_is_cached(self):
        for _i in range(2):
            self.render_context(views.CapacityView, range="24h")
            self.render_context(views.CapacityView, range="14d")
            self.render_context(views.CapacityView, view="pools")
        self.assertEqual(self.client.get_storage_capacity_trend.call_count, 3)


class PoolsTests(_ViewTestCase):
    def names(self, **params):
        context, html = self.render(views.PoolsView, **params)
        return [r["name"].split("#")[1] for r in context["rows"]], html

    def test_sort_orders(self):
        names, html = self.names()
        # pool-02 is forecast full first, pool-03 is stale (no forecast)
        self.assertEqual(
            names, ["storage-pool-02", "storage-pool-01", "storage-pool-03"]
        )
        self.assertIn("Full in ~25 days (virtual)", html)
        self.assertIn("Full in ~51 days (physical)", html)
        self.assertIn("Not forecast (stale)", html)
        self.assertIn("vm-ago", html)
        names, _html = self.names(sort="full")
        # pool-02: 8192 of a 10240 GiB virtual limit (80%)
        self.assertEqual(names[0], "storage-pool-01")
        names, _html = self.names(sort="stale")
        self.assertEqual(names[0], "storage-pool-03")

    def test_top_ten(self):
        pools = [
            {
                "name": f"h@b#p{i:02d}",
                "total": 100.0,
                "allocated": float(i),
                "free": 100.0 - i,
                "provisioned": float(i),
                "virtual_free": None,
                "last_seen": time.time(),
                "stale": False,
            }
            for i in range(15)
        ]
        with mock.patch.object(
            self.client, "get_storage_pools", return_value=pools
        ):
            context, html = self.render(views.PoolsView, sort="full")
        self.assertEqual(len(context["rows"]), 10)
        self.assertEqual(context["rows"][0]["name"], "h@b#p14")
        self.assertIn("15 pools.", html)
        self.assertIn("Top 10 shown", html)
        self.assertIn("not reported", html)

    def test_lookup(self):
        context, html = self.render(
            views.PoolLookupView, name=" CTL-02@NFS#storage-pool-03 "
        )
        self.assertEqual(context["row"]["name"], "ctl-02@nfs#storage-pool-03")
        self.assertIn("Stale", html)
        context, html = self.render(views.PoolLookupView, name="nope")
        self.assertIsNone(context["row"])
        self.assertIn('No pool named "nope"', html)
        context, html = self.render(views.PoolLookupView)
        self.assertNotIn("row", context)
        self.assertEqual(html.strip(), "")
        self.client.get_storage_pools.assert_called_once()

    def test_lookup_escapes_the_name(self):
        _context, html = self.render(views.PoolLookupView, name="<b>x</b>")
        self.assertNotIn("<b>x</b>", html)

    def test_suggestions(self):
        context, html = self.render(views.PoolSuggestionsView, name="POOL-0")
        self.assertEqual(len(context["names"]), 3)
        self.assertIn('<option value="ctl-01@lvm#storage-pool-01">', html)
        context, _html = self.render(views.PoolSuggestionsView, name="nfs")
        self.assertEqual(context["names"], ["ctl-02@nfs#storage-pool-03"])

    def test_suggestions_hide_errors(self):
        self.client.get_storage_pools.side_effect = Exception("down")
        with self.assertLogs(level="ERROR"):
            context, html = self.render(views.PoolSuggestionsView, name="x")
        self.assertEqual(context["names"], [])


class ServicesTests(_ViewTestCase):
    def test_rows(self):
        context, html = self.render(views.ServicesView)
        rows = context["rows"]
        self.assertEqual(rows[0]["host"], "ctl-02@nfs")
        self.assertFalse(rows[0]["up"])
        self.assertEqual(len(rows[0]["strip"]), 96)
        self.assertEqual(rows[0]["strip"][-1], 0)
        self.assertEqual(rows[0]["strip"][0], 1)
        self.assertIsNotNone(rows[0]["down_since"])
        self.assertAlmostEqual(rows[0]["up_percent"], 87.5, delta=1.1)
        self.assertEqual(rows[1]["up_percent"], 100.0)
        self.assertIn("down since", html)
        self.assertIn("vm-down", html)

    def test_history_is_optional(self):
        with mock.patch.object(
            self.client,
            "get_volume_service_history",
            side_effect=Exception("down"),
        ):
            with self.assertLogs(level="ERROR"):
                context, html = self.render(views.ServicesView)
        self.assertIsNone(context["rows"][0]["strip"])
        self.assertIn("24-hour service history could not be loaded", html)


class BackendsTests(_ViewTestCase):
    def test_rows(self):
        context, html = self.render(views.BackendsView)
        rows = context["rows"]
        self.assertEqual(
            [r["name"] for r in rows], ["ctl-02@nfs", "ctl-01@lvm"]
        )
        self.assertTrue(rows[0]["service_down"])
        self.assertEqual(rows[1]["pools"], 2)
        self.assertAlmostEqual(rows[1]["oversubscription"], 1.3)
        self.assertEqual(rows[1]["ratio_width"], 65)
        self.assertIn("service down", html)
        self.assertIn("1.30×", html)


class ProjectsTests(_ViewTestCase):
    def test_rows(self):
        context, html = self.render(views.ProjectsView)
        rows = context["rows"]
        self.assertEqual(rows[0]["project_name"], "ml-training")
        self.assertEqual(rows[0]["volume_gib"], 4000)
        self.assertEqual(context["total"], 5)
        # no horizon here: names without the volumes link
        self.assertIsNone(context["volumes_url"])
        self.assertNotIn("<form", html)
        self.assertIn("<strong>ml-training</strong>", html)

    def test_volumes_link(self):
        with mock.patch(
            f"{VIEWS}._volumes_filter_url", return_value="/admin/volumes/"
        ):
            _context, html = self.render(views.ProjectsView)
        self.assertIn('action="/admin/volumes/"', html)
        self.assertIn(
            'name="volumes__volumes_admin_filter__q_field" value="project"',
            html,
        )
        self.assertIn(
            'name="volumes__volumes_admin_filter__q" value="ml-training"', html
        )


class ImagesTests(_ViewTestCase):
    def test_images_without_backups(self):
        context, html = self.render(views.ImagesView)
        self.assertEqual(context["images"]["images"], 48)
        self.assertFalse(context["backup_service"])
        self.assertIn("in 5 projects", html)
        self.assertIn("Backup service (cinder-backup) not deployed", html)
        self.assertIn("(refreshed every 5 minutes)", html)

    def test_without_the_service_list(self):
        self.client.get_storage_pools.side_effect = Exception("boom")
        with self.assertLogs(level="ERROR"):
            context, html = self.render(views.ImagesView)
        self.assertNotIn("error_message", context)
        self.assertEqual(context["images"]["images"], 48)
        self.assertIsNone(context["backup_service"])
        self.assertIn("the backup service is unknown", html)
        self.assertNotIn("not deployed", html)

    def test_backups(self):
        images = dict(
            fake.fake_storage.fake_images(), backups=3.0, backup_gib=120.5
        )
        with mock.patch.object(
            self.client, "get_storage_images", return_value=images
        ):
            _context, html = self.render(views.ImagesView)
        self.assertIn("<strong>3</strong> | 120.5 GiB", html)
