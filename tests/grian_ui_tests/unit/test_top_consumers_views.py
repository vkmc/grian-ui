#
# SPDX-License-Identifier: Apache-2.0

import json
import re
import sys

from unittest import mock

from django.core.cache import cache
from django.template.loader import get_template
from django.test import RequestFactory

from grian_ui import cache as grian_cache
from grian_ui import top_consumers as tc
from grian_ui.content.top_consumers import views
from grian_ui.datasource import fake
from grian_ui.datasource import fake_top_consumers
from grian_ui_tests.unit import base


VIEWS = "grian_ui.content.top_consumers.views"
NAMES = fake_top_consumers.FAKE_PROJECT_NAMES
ID_OF = {name: pid for pid, name in NAMES.items()}


class _ViewTestCase(base.TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch(f"{VIEWS}.get_datasource")
        self.mock_get_ds = patcher.start()
        self.addCleanup(patcher.stop)
        # wrap the fake datasource so calls can be asserted
        self.client = mock.MagicMock(wraps=fake.get_client())
        self.mock_get_ds.return_value = self.client

    def _request(self, **params):
        request = RequestFactory().get("/", params)
        request.user = mock.MagicMock(services_region="RegionOne")
        return request

    def render_context(self, view_class, **params):
        view = view_class()
        view.setup(self._request(**params))
        return view.get_context_data()

    def render(self, view_class, **params):
        response = view_class.as_view()(self._request(**params))
        response.render()
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context = self.render_context(views.TopConsumersIndexView)
        self.assertEqual(
            [r["key"] for r in context["resources"]],
            ["cpu", "memory", "disk", "network"],
        )
        self.assertEqual(context["default_resource"], "cpu")
        self.assertEqual(context["default_by"], "project")
        self.assertEqual(context["refresh_seconds"], 60)
        self.assertEqual(context["current_window"], "10m")
        self.assertEqual(context["ranges"], ["1h", "6h", "24h", "7d"])
        self.assertEqual(context["default_range"], "24h")
        self.assertEqual(
            context["range_steps"],
            [
                "1h: every 1m, rates over 10m",
                "6h: every 5m, rates over 10m",
                "24h: every 15m, rates over 15m",
                "7d: every 1h, rates over 1h",
            ],
        )
        self.assertEqual(context["names_minutes"], 5)
        self.assertEqual(context["thresholds"]["usage_busy"], 80)
        self.assertEqual(context["error_messages"], [])
        # the shell does not query the datasource
        self.mock_get_ds.assert_not_called()

    def test_template(self):
        source = get_template("admin/top_consumers/index.html").template.source
        for name in ("summary", "top", "trend"):
            self.assertIn(
                f"{{% url 'horizon:admin:top_consumers:{name}' %}}", source
            )
        # a native disclosure, collapsed by default, inside the root
        self.assertIn('<details class="panel panel-default tc-section', source)
        self.assertNotIn("<details open", source)
        self.assertLess(
            source.index('id="tc-root"'), source.index('id="tc-how"')
        )
        self.assertIn("GRIAN_PLUGIN['top_consumers_thresholds']", source)
        # range buttons like Compute Host Health, default 24h
        self.assertIn('id="tc-range-buttons"', source)
        self.assertIn('name="range" id="tc-range"', source)
        self.assertIn('resource_name=~".+:instance-.+"', source)
        # same validated palette as the mockups, in a fixed order
        self.assertIn(
            "['#337ab7', '#eb6834', '#1baf7a', '#b0417a', '#6250d6']", source
        )

    @mock.patch(f"{VIEWS}.config.get_top_consumers_thresholds")
    def test_every_configuration_error_is_shown(self, mock_thresholds):
        mock_thresholds.side_effect = views.config.ConfigurationError("bad")
        with (
            mock.patch(
                f"{VIEWS}.config.get_datasource_type",
                return_value=views.config.DATASOURCE_PROMETHEUS,
            ),
            mock.patch(
                f"{VIEWS}.config.get_openstack_keystone_url",
                side_effect=views.config.ConfigurationError("no keystone"),
            ),
        ):
            context = self.render_context(views.TopConsumersIndexView)
        self.assertEqual(context["error_messages"], ["bad", "no keystone"])
        self.assertNotIn("thresholds", context)


class SummaryViewTests(_ViewTestCase):
    def test_totals(self):
        context = self.render_context(views.SummaryView)
        totals = context["totals"]
        self.assertEqual(totals["projects"], 12)
        self.assertEqual(totals["instances"], 186)
        self.assertEqual(context["cards"]["vcpus"], "744 vCPUs")
        self.assertAlmostEqual(
            context["cards"]["cpu_ratio"], 100 * totals["cpu"] / 744
        )
        self.assertFalse(context["data_stale"])

    def test_render(self):
        html = self.render(views.SummaryView)
        self.assertIn("CPU in use", html)
        self.assertIn("of 744 vCPUs allocated (58%)", html)
        self.assertIn("in 12 projects", html)
        self.assertIn('class="tc-clock"', html)

    def test_no_instances(self):
        self.client.get_top_consumers_projects.side_effect = None
        self.client.get_top_consumers_projects.return_value = {"projects": []}
        html = self.render(views.SummaryView)
        self.assertIn("No instance usage available", html)
        self.assertIn("Ceilometer compute agent", html)

    def test_snapshot_shared_between_partials(self):
        self.render_context(views.SummaryView)
        self.render_context(views.TopTableView)
        self.render_context(views.TrendView)
        self.render_context(views.TopTableView, resource="memory")
        self.assertEqual(self.client.get_top_consumers_projects.call_count, 1)

    def test_backend_error_hides_details(self):
        self.client.get_top_consumers_projects.side_effect = RuntimeError(
            "secret backend message"
        )
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.SummaryView)
        self.assertIn("Error loading telemetry data", html)
        self.assertNotIn("secret backend message", html)
        self.assertIn("data-tc-nodata", html)
        # an error is not retried quickly
        self.assertNotIn("tc-retry-", html)

    def test_still_computing_retries(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            html = self.render(views.SummaryView)
        self.assertIn("Still loading telemetry data", html)
        self.assertIn("tc-retry-", html)
        self.assertIn("htmx.trigger(target, 'tc-retry')", html)

    def test_stale_copy(self):
        snapshot = dict(
            fake.get_client().get_top_consumers_projects(),
            fetched_at=1_700_000_000,
            is_stale_copy=True,
        )
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", return_value=snapshot
        ):
            html = self.render(views.SummaryView)
        self.assertIn("could not be refreshed", html)

    def test_old_unmarked_copy_is_marked(self):
        snapshot = dict(
            fake.get_client().get_top_consumers_projects(),
            fetched_at=1_700_000_000,
            stored_at=1_700_000_000,
        )
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", return_value=snapshot
        ):
            context = self.render_context(views.SummaryView)
            html = self.render(views.SummaryView)
        # possibly out of date, not a failure
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


class TopTableByProjectTests(_ViewTestCase):
    def test_rows(self):
        context = self.render_context(views.TopTableView)
        rows = context["rows"]
        self.assertEqual(len(rows), tc.TOP_LIMIT)
        self.assertEqual(context["count"], 12)
        self.assertEqual(rows[0]["label"], "ml-training")
        self.assertEqual(rows[0]["level"], tc.LEVEL_BUSY)
        self.assertEqual(
            [r["color"] for r in rows],
            [0, 1, 2, 3, 4, None, None, None, None, None],
        )
        self.assertEqual(
            context["idle_projects"], ["decommissioned", "service"]
        )
        self.assertEqual(context["idle_more"], 0)
        self.assertEqual(
            sorted(context["sparks"]), sorted(r["key"] for r in rows)
        )
        self.assertTrue(all(r["spark_summary"] for r in rows))

    def test_render(self):
        html = self.render(views.TopTableView)
        self.assertIn("CPU: top projects", html)
        self.assertIn(f'data-tc-project="{ID_OF["ml-training"]}"', html)
        self.assertIn("94%", html)
        self.assertIn("busy", html)
        self.assertIn(
            "Projects with nothing running are not listed: decommissioned, service.",
            html,
        )
        self.assertIn("12 projects with running instances, 10 shown.", html)
        self.assertIn('data-tc-spark="', html)

    def test_sorted_by_the_chosen_resource(self):
        context = self.render_context(views.TopTableView, resource="disk")
        self.assertEqual(context["rows"][0]["label"], "backup")
        self.assertFalse(context["has_allocation"])
        html = self.render(views.TopTableView, resource="disk")
        self.assertNotIn("Used ÷ allocated</th>", html)

    def test_invalid_parameters_fall_back(self):
        context = self.render_context(
            views.TopTableView, resource="gpu", by="host", project="x"
        )
        self.assertEqual(context["resource"], "cpu")
        self.assertEqual(context["by"], "project")
        self.assertNotIn("project_filter", context)

    def test_without_project_names(self):
        with (
            mock.patch(
                f"{VIEWS}.list_project_names", side_effect=RuntimeError("down")
            ),
            self.assertLogs(VIEWS, "ERROR"),
        ):
            context = self.render_context(views.TopTableView)
            html = self.render(views.TopTableView)
        self.assertTrue(context["names_failed"])
        self.assertTrue(context["names_missing"])
        self.assertEqual(context["rows"][0]["label"], ID_OF["ml-training"])
        self.assertIn("showing project IDs", html)

    def test_table_renders_without_sparklines(self):
        self.client.get_top_consumers_trend.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(views.TopTableView)
        self.assertEqual(len(context["rows"]), tc.TOP_LIMIT)
        self.assertEqual(context["sparks"], {})
        self.assertEqual(
            context["partial_notes"], ["The CPU trends could not be loaded."]
        )
        self.assertNotIn("error_message", context)


class TopTableByInstanceTests(_ViewTestCase):
    def test_all_instances(self):
        context = self.render_context(
            views.TopTableView, by="instance", resource="disk"
        )
        rows = context["rows"]
        self.assertEqual(len(rows), tc.TOP_LIMIT)
        self.assertEqual(context["count"], 186)
        self.assertEqual(rows[0]["project_label"], "backup")
        # shares are of the cloud total
        total = tc.totals(fake.get_client().get_top_consumers_projects())[
            "disk"
        ]
        self.assertAlmostEqual(
            rows[0]["share"], 100 * rows[0]["value"] / total
        )
        # no admin instances panel in the unit tests: no link
        self.assertIsNone(rows[0]["url"])
        self.client.get_top_consumers_instances.assert_called_once_with(
            "disk", project_id=None, limit=tc.TOP_LIMIT
        )

    def test_instance_links(self):
        with mock.patch(f"{VIEWS}.reverse", return_value="/admin/i/x/detail"):
            html = self.render(views.TopTableView, by="instance")
        self.assertIn('href="/admin/i/x/detail"', html)

    def test_project_filter(self):
        pid = ID_OF["data-lake"]
        context = self.render_context(
            views.TopTableView, by="instance", project=pid, resource="memory"
        )
        self.assertEqual(
            context["project_filter"], {"id": pid, "label": "data-lake"}
        )
        self.assertEqual(context["count"], 40)
        self.assertTrue(all(r["project_id"] == pid for r in context["rows"]))
        self.assertAlmostEqual(
            sum(r["share"] for r in context["rows"]), 100 * 355 / 820
        )
        html = self.render(
            views.TopTableView, by="instance", project=pid, resource="memory"
        )
        self.assertIn("Memory: top instances of data-lake", html)
        self.assertIn('id="tc-clear-project"', html)

    def test_project_with_nothing_running(self):
        pid = ID_OF["service"]
        context = self.render_context(
            views.TopTableView, by="instance", project=pid
        )
        self.assertEqual(context["rows"], [])
        self.assertEqual(context["count"], 0)
        self.client.get_top_consumers_instances.assert_not_called()
        html = self.render(views.TopTableView, by="instance", project=pid)
        self.assertIn("service has no running instance", html)

    def test_invalid_project_is_ignored(self):
        context = self.render_context(
            views.TopTableView, by="instance", project='x"} or vector(1)'
        )
        self.assertNotIn("project_filter", context)
        self.client.get_top_consumers_instances.assert_called_once_with(
            "cpu", project_id=None, limit=tc.TOP_LIMIT
        )

    def test_unknown_number_of_instances(self):
        # no instance count in the snapshot (e.g. no ceilometer_vcpus):
        # the footer must not claim 0 or only the rows shown
        snapshot = fake.get_client().get_top_consumers_projects()
        for project in snapshot["projects"]:
            project["instances"] = None
        self.client.get_top_consumers_projects.side_effect = None
        self.client.get_top_consumers_projects.return_value = snapshot
        pid = ID_OF["data-lake"]
        for params in ({}, {"project": pid}):
            cache.clear()
            context = self.render_context(
                views.TopTableView, by="instance", **params
            )
            self.assertTrue(context["rows"])
            self.assertIsNone(context["count"])
            html = self.render(views.TopTableView, by="instance", **params)
            self.assertIn(
                "Number of running instances unknown, "
                f"{len(context['rows'])} shown.",
                html,
            )
            self.assertNotIn("running instances,", html)

    def test_instance_lists_cached_per_resource_and_project(self):
        self.render_context(views.TopTableView, by="instance")
        self.render_context(views.TrendView, by="instance")
        self.assertEqual(self.client.get_top_consumers_instances.call_count, 1)
        self.render_context(views.TopTableView, by="instance", resource="disk")
        self.assertEqual(self.client.get_top_consumers_instances.call_count, 2)


class EmptyTableTests(_ViewTestCase):
    def test_empty_state_spans_the_columns(self):
        self.client.get_top_consumers_projects.side_effect = None
        self.client.get_top_consumers_projects.return_value = {"projects": []}
        self.client.get_top_consumers_instances.side_effect = None
        self.client.get_top_consumers_instances.return_value = []
        for by in ("project", "instance"):
            for resource in ("cpu", "disk"):
                cache.clear()
                html = self.render(
                    views.TopTableView, by=by, resource=resource
                )
                thead = re.search(r"<thead>(.*?)</thead>", html, re.S)
                columns = thead.group(1).count("<th")
                colspan = re.search(r'<td colspan="(\d+)"', html)
                self.assertEqual(
                    int(colspan.group(1)), columns, (by, resource)
                )
                if by == "instance":
                    # nothing running: a known 0
                    self.assertIn("0 running instances, 0 shown.", html)


class TrendViewTests(_ViewTestCase):
    def test_top_five_and_other(self):
        table = self.render_context(views.TopTableView)
        context = self.render_context(views.TrendView)
        datasets = context["chart"]["datasets"]
        self.assertEqual(
            [d["label"] for d in datasets],
            [r["label"] for r in table["rows"][:5]] + ["Other"],
        )
        self.assertEqual([d["color"] for d in datasets], [0, 1, 2, 3, 4, None])
        self.assertEqual(context["chart"]["unit"], "cores")
        self.assertEqual(len(context["chart"]["timestamps"]), 96)
        self.assertTrue(context["has_data"])
        # the stack adds up to the total
        snapshot = fake.get_client().get_top_consumers_projects()
        trend = fake.get_client().get_top_consumers_trend(
            "cpu", "project", [], at=context["chart"]["timestamps"][-1]
        )
        stacked = sum(d["values"][-1] for d in datasets)
        self.assertAlmostEqual(stacked, trend["total"][-1][1], places=6)
        self.assertGreater(tc.totals(snapshot)["cpu"], 0)

    def test_one_trend_query_per_view(self):
        self.render_context(views.TrendView, resource="network")
        self.client.get_top_consumers_trend.assert_called_once()
        args, kwargs = self.client.get_top_consumers_trend.call_args
        self.assertEqual(args[:2], ("network", "project"))
        self.assertEqual(len(args[2]), tc.CHART_SERIES)
        self.assertIsNone(kwargs["project_id"])
        self.assertEqual(kwargs["range_key"], "24h")

    def test_range(self):
        context = self.render_context(views.TrendView, range="7d")
        self.assertEqual(context["chart"]["range"], "7d")
        self.assertEqual(len(context["chart"]["timestamps"]), 168)
        self.assertEqual(
            self.client.get_top_consumers_trend.call_args.kwargs["range_key"],
            "7d",
        )
        html = self.render(views.TrendView, range="1h")
        self.assertIn("CPU over the last 1h, by project", html)

    def test_invalid_range_falls_back_to_24h(self):
        context = self.render_context(views.TrendView, range="2y")
        self.assertEqual(context["chart"]["range"], "24h")

    def test_trends_cached_per_range(self):
        self.render_context(views.TrendView)
        self.render_context(views.TrendView)
        self.assertEqual(self.client.get_top_consumers_trend.call_count, 1)
        self.render_context(views.TrendView, range="1h")
        self.assertEqual(self.client.get_top_consumers_trend.call_count, 2)

    def test_sparklines_follow_the_range(self):
        context = self.render_context(views.TopTableView, range="6h")
        self.assertEqual(len(next(iter(context["sparks"].values()))), 72)
        html = self.render(views.TopTableView, range="6h")
        self.assertIn("CPU, 6h", html)

    def test_render(self):
        html = self.render(views.TrendView, by="instance", resource="disk")
        self.assertIn("Disk I/O over the last 24h, by instance", html)
        match = re.search(
            r'<script id="tc-trend-data-\w+" type="application/json">(.*?)</script>',
            html,
            re.S,
        )
        data = json.loads(match.group(1))
        self.assertEqual(data["unit"], "bytes_per_second")
        self.assertEqual(data["datasets"][0]["label"], "backup-01")

    def test_old_copy_is_flagged(self):
        real = views.TrendView.get_trend

        def get_trend(view, *args):
            return dict(real(view, *args), is_old_copy=True)

        with mock.patch.object(views.TrendView, "get_trend", get_trend):
            context = self.render_context(views.TrendView)
            html = self.render(views.TrendView)
        self.assertTrue(context["data_old"])
        self.assertIn("may be out of date", html)

    def test_fill_skips_a_series_without_history(self):
        real = fake.get_client().get_top_consumers_trend
        table = self.render_context(views.TopTableView)
        keys = [r["key"] for r in table["rows"][: tc.CHART_SERIES]]
        for missing, fills in (
            (keys[1], ["origin", 0, 0, 2, 3, 4]),
            (keys[0], ["origin", "origin", 1, 2, 3, 4]),
        ):

            def trend(*args, missing=missing, **kwargs):
                result = real(*args, **kwargs)
                del result["series"][missing]
                return result

            cache.clear()
            self.client.get_top_consumers_trend.side_effect = trend
            context = self.render_context(views.TrendView)
            datasets = context["chart"]["datasets"]
            self.assertEqual([d["label"] for d in datasets][-1], "Other")
            self.assertEqual([d["fill"] for d in datasets], fills)
            # the dataset without history is all None
            gone = datasets[keys.index(missing)]["values"]
            self.assertTrue(all(v is None for v in gone))

    def test_no_data(self):
        self.client.get_top_consumers_projects.side_effect = None
        self.client.get_top_consumers_projects.return_value = {"projects": []}
        context = self.render_context(views.TrendView)
        self.assertFalse(context["has_data"])
        self.client.get_top_consumers_trend.assert_not_called()
        html = self.render(views.TrendView)
        self.assertIn("No usage data in the last 24h", html)

    def test_error(self):
        self.client.get_top_consumers_trend.side_effect = RuntimeError("x")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.TrendView)
        self.assertIn("Error loading telemetry data", html)
        self.assertIn("tc.destroyChart('trend')", html)


class ProjectNamesTests(base.TestCase):
    def test_fake(self):
        self.assertEqual(views.list_project_names(None), NAMES)

    def test_keystone(self):
        keystone = mock.MagicMock()
        project = mock.MagicMock(id="p1")
        project.name = "admin"
        keystone.tenant_list.return_value = ([project], False)
        api = mock.MagicMock(keystone=keystone)
        modules = {
            "openstack_dashboard": mock.MagicMock(api=api),
            "openstack_dashboard.api": api,
            "openstack_dashboard.api.keystone": keystone,
        }
        request = object()
        with (
            mock.patch.dict(sys.modules, modules),
            mock.patch(
                f"{VIEWS}.config.get_datasource_type",
                return_value=views.config.DATASOURCE_PROMETHEUS,
            ),
        ):
            self.assertEqual(
                views.list_project_names(request), {"p1": "admin"}
            )
        keystone.tenant_list.assert_called_once_with(request, admin=True)


class ProjectNamesSnapshotTests(_ViewTestCase):
    """Keystone is called once per region and lifetime, never per refresh."""

    def refresh_page(self, **params):
        self.render_context(views.TopConsumersIndexView)
        self.render(views.SummaryView)
        self.render(views.TopTableView, **params)
        self.render(views.TrendView, **params)

    def test_one_keystone_call_for_many_refreshes(self):
        with mock.patch(
            f"{VIEWS}.list_project_names", return_value=dict(NAMES)
        ) as keystone:
            for params in (
                {},
                {"resource": "memory"},
                {"by": "instance"},
                {"by": "instance", "project": ID_OF["erp"]},
                {"range": "7d"},
                {},
            ):
                self.refresh_page(**params)
        keystone.assert_called_once()

    def test_failure_is_cached_too(self):
        with (
            mock.patch(
                f"{VIEWS}.list_project_names", side_effect=RuntimeError("x")
            ) as keystone,
            self.assertLogs(VIEWS, "ERROR"),
        ):
            for _i in range(3):
                self.refresh_page()
            html = self.render(views.TopTableView)
        keystone.assert_called_once()
        # still renders, with the IDs and a note
        self.assertIn(ID_OF["ml-training"], html)
        self.assertIn("showing project IDs", html)

    def test_last_names_kept_while_keystone_fails(self):
        with mock.patch(
            f"{VIEWS}.list_project_names", return_value=dict(NAMES)
        ):
            self.render(views.TopTableView)
        # the shared value expires; the next computation fails
        cache.delete(views._cache_key(self._request(), "project_names"))
        with (
            mock.patch(
                f"{VIEWS}.list_project_names", side_effect=RuntimeError("x")
            ),
            self.assertLogs(VIEWS, "ERROR"),
        ):
            context = self.render_context(views.TopTableView)
            html = self.render(views.TopTableView)
        self.assertEqual(context["rows"][0]["label"], "ml-training")
        self.assertTrue(context["names_failed"])
        self.assertFalse(context["names_missing"])
        self.assertIn("could not be reached", html)

    def test_footer_says_when_and_how_often(self):
        html = self.render(views.TopTableView)
        self.assertIn("Project names from", html)
        self.assertIn("(refreshed every 5 minutes)", html)
        self.assertNotIn("possibly out of date", html)

    def test_old_names_are_flagged(self):
        names = {
            "names": dict(NAMES),
            "fetched_at": 1_700_000_000,
            "failed": False,
            "is_old_copy": True,
            "stored_at": 1_700_000_000,
        }
        with mock.patch.object(
            views._TopConsumersMixin, "get_project_names", return_value=names
        ):
            html = self.render(views.TopTableView)
        self.assertIn("(possibly out of date)", html)

    def test_names_still_loading(self):
        with mock.patch.object(
            views._TopConsumersMixin,
            "get_or_compute",
            autospec=True,
            side_effect=lambda view, name, ttl, compute: (
                (_ for _ in ()).throw(grian_cache.StillComputing("busy"))
                if name == "project_names"
                else dict(compute(), stored_at=None)
            ),
        ):
            context = self.render_context(views.TopTableView)
        self.assertTrue(context["names_loading"])
        self.assertEqual(context["rows"][0]["label"], ID_OF["ml-training"])

    def test_join_once_per_pair_of_inputs(self):
        views._JOINS.clear()
        with mock.patch(f"{VIEWS}.join_rows", wraps=views.join_rows) as join:
            self.render_context(views.TopTableView)
            self.render_context(views.TrendView)
            self.render_context(views.TopTableView)
        join.assert_called_once()
