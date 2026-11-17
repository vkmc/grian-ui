#
# SPDX-License-Identifier: Apache-2.0

import re
import time

from unittest import mock

from django.core.cache import cache
from django.template.loader import get_template
from django.template.loader import render_to_string
from django.test import RequestFactory

from grian_ui import compute_health
from grian_ui.content.compute_host_health import views
from grian_ui.datasource import fake
from grian_ui_tests.unit import base


VIEWS = "grian_ui.content.compute_host_health.views"

LOOKUP_LABEL = re.compile(r'<label[^>]*for="chh-lookup-name"[^>]*>')


_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "col", "wbr"}


def _retry_target(html):
    """
    Replay _retry.html's lookup on a rendered partial.

    Returns "hx-get" when the retry span finds an element with hx-get the
    way the script does (closest ancestor with hx-get, else the closest
    ancestor containing one), None otherwise.
    """
    from html.parser import HTMLParser

    class Node:
        def __init__(self, tag, attrs, parent):
            self.tag, self.attrs, self.parent = tag, dict(attrs), parent
            self.children = []

    class Tree(HTMLParser):
        def __init__(self):
            super().__init__()
            self.root = self.current = Node("root", [], None)
            self.retry = None

        def handle_starttag(self, tag, attrs):
            node = Node(tag, attrs, self.current)
            self.current.children.append(node)
            if (node.attrs.get("id") or "").startswith("chh-retry-"):
                self.retry = node
            if tag not in _VOID:
                self.current = node

        def handle_endtag(self, tag):
            node = self.current
            while node is not self.root and node.tag != tag:
                node = node.parent
            if node is not self.root:
                self.current = node.parent

    def contains_hx_get(node):
        return any(
            "hx-get" in child.attrs or contains_hx_get(child)
            for child in node.children
        )

    tree = Tree()
    tree.feed(html)
    node = tree.retry
    if node is None:
        return None
    # strict: the closest ancestor with hx-get, or the span's parent
    # containing one, so the include works even without the walk-up
    ancestor = node.parent
    while ancestor is not None:
        if "hx-get" in ancestor.attrs:
            return "hx-get"
        ancestor = ancestor.parent
    return "hx-get" if contains_hx_get(node.parent) else None


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
        request = RequestFactory().get("/", params)
        request.user = mock.MagicMock(services_region="RegionOne")
        view = view_class()
        view.setup(request)
        return view.get_context_data()


class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context = self.render_context(views.ComputeHostHealthIndexView)
        self.assertEqual(context["ranges"], ["1h", "6h", "24h", "7d"])
        self.assertEqual(context["default_range"], "6h")
        self.assertEqual(context["thresholds"]["cpu_critical"], 90)
        # the shell does not query the datasource
        self.mock_get_ds.assert_not_called()

    def test_how_this_page_is_calculated(self):
        context = self.render_context(views.ComputeHostHealthIndexView)
        # the bands follow the thresholds; every range says its step
        self.assertEqual(
            context["band_labels"],
            ["0–25%", "25–50%", "50–75%", "75–90%", "≥ 90%"],
        )
        self.assertEqual(
            context["range_steps"],
            [
                "1h: every 1m",
                "6h: every 5m",
                "24h: every 15m",
                "7d: every 1h",
            ],
        )
        self.assertEqual(context["average_window"], "15m")
        source = get_template(
            "admin/compute_host_health/index.html"
        ).template.source
        # a native disclosure, collapsed by default, after the status rules
        self.assertIn(
            '<details class="panel panel-default chh-section', source
        )
        self.assertNotIn("<details open", source)
        self.assertLess(
            source.index("Status rules"), source.index('id="chh-how"')
        )
        # the host label the code really reads, and the real settings key
        self.assertIn(
            'vm_instance="compute.&lt;hypervisor hostname&gt;"', source
        )
        self.assertIn("GRIAN_PLUGIN['compute_host_thresholds']", source)
        # the source line moved into the panel
        self.assertNotIn("(Nova compute monitors) through Prometheus", source)
        # inside #chh-root, where the contrast overrides apply
        root = source.index('<div id="chh-root"')
        self.assertLess(root, source.index('id="chh-how"'))
        self.assertLess(source.index("</details>"), source.rindex("</div>"))

    @mock.patch(f"{VIEWS}.config.get_compute_host_thresholds")
    def test_bands_need_valid_thresholds(self, mock_thresholds):
        mock_thresholds.side_effect = views.config.ConfigurationError("bad")
        context = self.render_context(views.ComputeHostHealthIndexView)
        self.assertNotIn("band_labels", context)

    @mock.patch(f"{VIEWS}.config.get_compute_host_thresholds")
    def test_configuration_error(self, mock_thresholds):
        mock_thresholds.side_effect = views.config.ConfigurationError("bad")
        context = self.render_context(views.ComputeHostHealthIndexView)
        self.assertEqual(context["error_messages"], ["bad"])

    @mock.patch(f"{VIEWS}.config.get_compute_host_thresholds")
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
            context = self.render_context(views.ComputeHostHealthIndexView)
        self.assertEqual(context["error_messages"], ["bad", "no keystone"])


class SummaryViewTests(_ViewTestCase):
    def test_summary(self):
        context = self.render_context(views.SummaryView)
        summary = context["summary"]
        self.assertEqual(summary["known"], 26)
        self.assertEqual(summary["attention_total"], 6)
        stale_rows = [
            r for r in summary["attention"] if r["status"]["key"] == "stale"
        ]
        self.assertTrue(all(r["last_seen_age"] for r in stale_rows))

    def test_snapshot_is_cached_between_partials(self):
        self.render_context(views.SummaryView)
        self.render_context(views.DistributionView)
        self.render_context(views.TopHostsView)
        self.assertEqual(self.client.get_compute_host_snapshot.call_count, 1)

    def test_error_is_generic_and_logged(self):
        self.client.get_compute_host_snapshot.side_effect = Exception("boom")
        # the shared cache logs the traceback once; the view one line
        with (
            self.assertLogs("grian_ui.cache", level="ERROR") as cache_logs,
            self.assertLogs(views.LOG, level="WARNING") as logs,
        ):
            context = self.render_context(views.SummaryView)
        self.assertIn("boom", "\n".join(cache_logs.output))
        self.assertTrue(all(r.exc_info is None for r in logs.records))
        self.assertNotIn("boom", str(context["error_message"]))
        self.assertIn("Horizon logs", str(context["error_message"]))
        self.assertIn("boom", logs.output[0])
        self.assertNotIn("summary", context)

    def test_shared_data_still_loading(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=views.cache.StillComputing("busy"),
        ):
            context = self.render_context(views.SummaryView)
        self.assertTrue(context["loading"])
        self.assertIn("Still loading", str(context["error_message"]))

    def test_failed_computation_is_an_error_not_loading(self):
        with (
            mock.patch(
                f"{VIEWS}.cache.get_or_compute",
                side_effect=views.cache.ComputationFailed("aetos down"),
            ),
            self.assertLogs(views.LOG, level="WARNING"),
        ):
            context = self.render_context(views.SummaryView)
        self.assertNotIn("loading", context)
        self.assertIn("Horizon logs", str(context["error_message"]))

    def test_fleet_trend_query_time_is_the_grid_time(self):
        context = self.render_context(views.FleetTrendView, range="1h")
        (call,) = self.client.get_compute_fleet_trend.call_args_list
        at = call.kwargs["at"]
        grid_end = context["chart"]["series"]["p50"][-1][0]
        self.assertEqual(grid_end, int(at // 60 * 60))

    def test_fetched_at(self):
        context = self.render_context(views.SummaryView)
        self.assertAlmostEqual(context["fetched_at"], time.time(), delta=5)

    def test_snapshot_ttl_is_shorter_than_refresh(self):
        # every refresh gets new data, computed once
        self.assertLess(views.SNAPSHOT_CACHE_TTL, views.REFRESH_SECONDS)


class DistributionViewTests(_ViewTestCase):
    def test_chart(self):
        context = self.render_context(views.DistributionView)
        chart = context["chart"]
        self.assertEqual(len(chart["labels"]), 5)
        self.assertEqual(sum(chart["counts"]), 24)
        self.assertEqual(chart["total"], 24)


class FleetTrendViewTests(_ViewTestCase):
    def test_range_parameter(self):
        context = self.render_context(views.FleetTrendView, range="24h")
        self.assertEqual(context["range_key"], "24h")
        self.client.get_compute_fleet_trend.assert_called_once_with(
            "24h", at=mock.ANY
        )
        self.assertTrue(context["has_data"])
        self.assertEqual(context["chart"]["critical"], 90)

    def test_invalid_range(self):
        context = self.render_context(views.FleetTrendView, range="1y")
        self.assertEqual(context["range_key"], "6h")

    def _fleet(self, values, fetched_at):
        """A cached fleet trend: values at the last steps of its grid."""
        grid = views.timeseries.range_grid("6h", fetched_at)
        points = [[t, v] for t, v in zip(grid[-len(values) :], values)]
        return {
            "series": {"p50": points, "p95": points, "max": points},
            "fetched_at": fetched_at,
        }

    def test_grid_ends_when_the_trend_was_evaluated(self):
        # a trend cached 3 hours ago keeps its last value at its end
        fetched_at = time.time() - 3 * 3600
        with mock.patch.object(
            views._ComputeHostHealthMixin,
            "get_fleet_trend",
            return_value=self._fleet([10.0, 20.0], fetched_at),
        ):
            context = self.render_context(views.FleetTrendView)
        last = context["chart"]["series"]["p50"][-1]
        self.assertEqual(
            last, [views.timeseries.range_grid("6h", fetched_at)[-1], 20.0]
        )
        self.assertEqual(context["latest"]["p50"], 20.0)

    def test_latest_values_tolerate_one_empty_last_step(self):
        # the last point can lag behind the time the trend was evaluated
        fleet = self._fleet([10.0, 20.0, None], time.time())
        with mock.patch.object(
            views._ComputeHostHealthMixin,
            "get_fleet_trend",
            return_value=fleet,
        ):
            context = self.render_context(views.FleetTrendView)
            html = render_to_string(
                views.FleetTrendView.template_name, context
            )
        self.assertEqual(context["latest"]["p50"], 20.0)
        self.assertIn("Latest values: median 20.0%", html)

    def test_latest_values_tolerate_only_one_empty_step(self):
        fleet = self._fleet([10.0, 20.0, None, None], time.time())
        with mock.patch.object(
            views._ComputeHostHealthMixin,
            "get_fleet_trend",
            return_value=fleet,
        ):
            context = self.render_context(views.FleetTrendView)
            html = render_to_string(
                views.FleetTrendView.template_name, context
            )
        self.assertIsNone(context["latest"])
        self.assertIn("No recent data", html)

    def test_text_alternative_when_the_chart_ends_in_a_gap(self):
        fetched_at = time.time()
        fleet = self._fleet([10.0] + [None] * 24, fetched_at)
        with mock.patch.object(
            views._ComputeHostHealthMixin,
            "get_fleet_trend",
            return_value=fleet,
        ):
            context = self.render_context(views.FleetTrendView)
            html = render_to_string(
                views.FleetTrendView.template_name, context
            )
        self.assertIsNone(context["latest"])
        self.assertEqual(context["last_data_age"], "2 hours ago")
        self.assertIn("No recent data", html)
        self.assertNotIn("Latest values", html)


class TopHostsViewTests(_ViewTestCase):
    def test_sparklines_end_when_the_trends_were_evaluated(self):
        fetched_at = time.time() - 3 * 3600
        end = views.timeseries.range_grid("6h", fetched_at)[-1]
        with mock.patch.object(
            views._ComputeHostHealthMixin,
            "get_host_trends",
            return_value={
                "trends": {"compute-03": [[end, 42.0]]},
                "fetched_at": fetched_at,
            },
        ):
            context = self.render_context(views.TopHostsView)
        self.assertEqual(context["trends"]["compute-03"][-1], 42.0)

    def test_default_busiest_cpu(self):
        context = self.render_context(views.TopHostsView)
        self.assertEqual(context["by"], "cpu")
        self.assertEqual(len(context["rows"]), 10)
        self.assertEqual(context["total"], 24)
        self.assertEqual(context["rows"][0]["name"], "compute-03")
        self.assertIn("detail", context["rows"][0])
        self.assertEqual(len(context["trends"]), 10)
        # per-host queries are limited to the displayed hosts
        names = self.client.get_compute_host_details.call_args.args[0]
        self.assertEqual(len(names), 10)

    def test_stale_does_not_query_details(self):
        context = self.render_context(views.TopHostsView, by="stale")
        self.assertEqual(
            [r["name"] for r in context["rows"]], ["compute-25", "compute-26"]
        )
        self.client.get_compute_host_details.assert_not_called()
        self.assertTrue(context["rows"][0]["last_seen_age"].endswith("ago"))

    def test_band(self):
        context = self.render_context(views.TopHostsView, by="band", band="4")
        self.assertEqual(context["band_label"], "≥ 90%")
        self.assertEqual([r["name"] for r in context["rows"]], ["compute-03"])

    def test_invalid_parameters(self):
        context = self.render_context(views.TopHostsView, by="evil", band="x")
        self.assertEqual(context["by"], "cpu")
        self.assertIsNone(context["band"])


class HostLookupViewTests(_ViewTestCase):
    def test_trend_summary_reads_the_drawn_values(self):
        at = time.time()
        grid = views.timeseries.range_grid("6h", at)
        step = grid[1] - grid[0]
        trend = [
            # before the grid (Prometheus 2 can return it): not drawn
            [grid[0] - step, 99.0],
            [grid[-2], 10.0],
            [grid[-1], 20.0],
        ]
        with mock.patch.object(
            views._ComputeHostHealthMixin,
            "get_host_trends",
            return_value={"trends": {"compute-03": trend}, "fetched_at": at},
        ):
            context = self.render_context(
                views.HostLookupView, name="compute-03"
            )
        self.assertEqual(
            context["trend_summary"],
            {"latest": 20.0, "min": 10.0, "max": 20.0},
        )

    def test_lookup_box_has_a_visible_label(self):
        source = get_template(
            "admin/compute_host_health/index.html"
        ).template.source
        label = LOOKUP_LABEL.search(source)
        self.assertIsNotNone(label)
        self.assertNotIn("sr-only", label.group(0))

    def test_lookup_in_another_case_shows_the_reported_name(self):
        context = self.render_context(views.HostLookupView, name="COMPUTE-03")
        self.assertTrue(context["found"])
        self.assertEqual(context["name"], "compute-03")
        self.assertEqual(context["chart"]["name"], "compute-03")
        self.client.get_compute_host_details.assert_called_with(["compute-03"])

    def test_lookup_of_a_name_matching_several_hosts(self):
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [
                {"name": "cmp", "cpu_avg": 10.0, "iowait_avg": 0.0},
                {"name": "CMP", "cpu_avg": 20.0, "iowait_avg": 0.0},
            ],
            "stale": [],
        }
        context = self.render_context(views.HostLookupView, name="Cmp")
        html = render_to_string(views.HostLookupView.template_name, context)
        self.assertEqual(context["ambiguous"], ["CMP", "cmp"])
        self.assertIn("Several hosts match <strong>Cmp</strong>", html)
        self.assertIn("(CMP, cmp)", html)
        self.assertNotIn("No compute host named", html)
        self.assertIn(
            "<span hidden data-chh-announce>Several hosts match Cmp", html
        )

    def test_chart_ends_when_the_trends_were_evaluated(self):
        host_at = time.time() - 3 * 3600
        fleet_at = time.time() - 2 * 3600
        host_end = views.timeseries.range_grid("6h", host_at)[-1]
        fleet_end = views.timeseries.range_grid("6h", fleet_at)[-1]
        with (
            mock.patch.object(
                views._ComputeHostHealthMixin,
                "get_host_trends",
                return_value={
                    "trends": {"compute-03": [[host_end, 42.0]]},
                    "fetched_at": host_at,
                },
            ),
            mock.patch.object(
                views._ComputeHostHealthMixin,
                "get_fleet_trend",
                return_value={
                    "series": {"p50": [[fleet_end, 30.0]]},
                    "fetched_at": fleet_at,
                },
            ),
        ):
            context = self.render_context(
                views.HostLookupView, name="compute-03"
            )
        self.assertEqual(context["chart"]["host"][-1], [host_end, 42.0])
        self.assertEqual(
            context["chart"]["fleet_median"][-1], [fleet_end, 30.0]
        )

    def test_empty_name(self):
        context = self.render_context(views.HostLookupView)
        self.assertEqual(context["name"], "")
        self.mock_get_ds.assert_not_called()

    def test_unknown_host(self):
        context = self.render_context(views.HostLookupView, name="nope")
        self.assertFalse(context["found"])
        self.client.get_compute_host_details.assert_not_called()

    def test_reporting_host(self):
        context = self.render_context(
            views.HostLookupView, name="compute-10", range="1h"
        )
        self.assertTrue(context["found"])
        self.assertEqual(context["status"]["key"], "critical")
        self.assertEqual(context["chart"]["range"], "1h")
        self.assertEqual(len(context["chart"]["host"]), 60)
        self.assertTrue(context["last_seen_age"])

    def test_stale_host(self):
        context = self.render_context(views.HostLookupView, name="compute-25")
        self.assertEqual(context["status"]["key"], "stale")
        self.assertEqual(context["last_seen_age"], "12 minutes ago")
        self.client.get_compute_host_details.assert_not_called()


class HostSuggestionsViewTests(_ViewTestCase):
    def test_suggestions_from_cached_snapshot(self):
        self.render_context(views.SummaryView)  # warms the cache
        context = self.render_context(
            views.HostSuggestionsView, name="compute-1"
        )
        names = [r["name"] for r in context["suggestions"]]
        self.assertEqual(names[:3], ["compute-10", "compute-11", "compute-12"])
        self.assertEqual(self.client.get_compute_host_snapshot.call_count, 1)

    def test_rendered_options_are_escaped_and_bounded(self):
        context = self.render_context(views.HostSuggestionsView, name="")
        html = render_to_string(
            views.HostSuggestionsView.template_name, context
        )
        self.assertEqual(html.count("<option "), 20)
        self.assertIn('<option value="compute-03">Critical</option>', html)

    def test_error_returns_no_suggestions(self):
        self.client.get_compute_host_snapshot.side_effect = Exception("down")
        # the shared cache logs the failure once; the view stays quiet
        with self.assertLogs("grian_ui.cache", level="ERROR"):
            context = self.render_context(views.HostSuggestionsView, name="c")
        self.assertEqual(context["suggestions"], [])

    def test_loading_returns_no_suggestions_without_traceback(self):
        with (
            mock.patch(
                f"{VIEWS}.cache.get_or_compute",
                side_effect=views.cache.StillComputing("busy"),
            ),
            self.assertNoLogs(views.LOG, level="INFO"),
        ):
            context = self.render_context(views.HostSuggestionsView, name="c")
        self.assertEqual(context["suggestions"], [])


class SnapshotMergeTests(_ViewTestCase):
    def test_long_stale_list_is_merged_and_cached_longer(self):
        now = time.time()
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [{"name": "up", "cpu_avg": 10.0, "iowait_avg": 1.0}],
            "stale": [{"name": "recent", "last_seen": now - 700}],
        }
        self.client.get_compute_stale_hosts.return_value = [
            {"name": "recent", "last_seen": now - 3600},  # older reading
            {"name": "gone", "last_seen": now - 20 * 3600},
            {"name": "up", "last_seen": now - 3600},  # reports again
        ]
        snapshot = self.render_context(views.SummaryView)["summary"]
        names = {r["name"] for r in snapshot["attention"]}
        self.assertEqual(names, {"recent", "gone"})
        self.assertEqual(snapshot["stale"], 2)
        # the recent reading wins
        recent = next(
            r for r in snapshot["attention"] if r["name"] == "recent"
        )
        self.assertAlmostEqual(recent["last_seen"], now - 700, delta=1)
        # the 24 h query is shared longer than the snapshot
        cache.delete(views._cache_key(self._request(), "snapshot"))
        self.render_context(views.SummaryView)
        self.assertEqual(self.client.get_compute_host_snapshot.call_count, 2)
        self.client.get_compute_stale_hosts.assert_called_once()
        self.assertGreater(
            views.STALE_LIST_CACHE_TTL, views.SNAPSHOT_CACHE_TTL
        )

    def test_long_stale_list_failure_does_not_block_the_page(self):
        self.client.get_compute_stale_hosts.side_effect = Exception("slow")
        with self.assertLogs("grian_ui.cache", level="ERROR"):
            context = self.render_context(views.SummaryView)
        self.assertNotIn("error_message", context)
        self.assertEqual(context["summary"]["reporting"], 24)
        # ...but the page says hosts silent for longer may be missing
        self.assertTrue(context["stale_list_incomplete"])

    def test_old_copy_of_the_list_is_not_flagged(self):
        # the snapshot's lookback covers the oldest copy that can be served

        stale_copy = {"stale": [], "fetched_at": 0, "is_stale_copy": True}
        real = views.cache.get_or_compute

        def get_or_compute(key, ttl, compute):
            if ":stale:" in key:
                return stale_copy
            return real(key, ttl, compute)

        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=get_or_compute
        ):
            context = self.render_context(views.SummaryView)
        self.assertFalse(context["stale_list_incomplete"])
        # the list being old does not make the page data old
        self.assertFalse(context["data_stale"])

    def test_list_still_computing_shows_loading(self):
        real = views.cache.get_or_compute

        def get_or_compute(key, ttl, compute):
            if ":stale:" in key:
                raise views.cache.StillComputing("cold")
            return real(key, ttl, compute)

        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=get_or_compute
        ):
            context = self.render_context(views.SummaryView)
        # a cold cache is not a failure: load and retry like other data
        self.assertTrue(context["loading"])
        self.assertNotIn("stale_list_incomplete", context)

    def test_recent_lookback_covers_the_oldest_list_served(self):
        # a host must never fall between the snapshot's short lookback and
        # an old (cached, or last good) copy of the 24 h list
        oldest_list = (
            views.timeseries.duration_seconds(compute_health.STALE_AFTER)
            + views.STALE_LIST_CACHE_TTL
            + views.cache.LAST_GOOD_TTL
        )
        self.assertGreaterEqual(
            views.timeseries.duration_seconds(
                compute_health.STALE_RECENT_LOOKBACK
            ),
            oldest_list,
        )


class HostDataCacheTests(_ViewTestCase):
    def test_top_hosts_data_is_shared(self):
        self.render_context(views.TopHostsView)
        self.render_context(views.TopHostsView)
        self.client.get_compute_host_details.assert_called_once()
        self.client.get_compute_host_trends.assert_called_once()

    def test_per_range_trends(self):
        self.render_context(views.TopHostsView, range="1h")
        self.render_context(views.TopHostsView, range="24h")
        self.client.get_compute_host_details.assert_called_once()
        self.assertEqual(self.client.get_compute_host_trends.call_count, 2)

    def test_lookup_reuses_the_cache(self):
        self.render_context(views.HostLookupView, name="compute-01")
        self.render_context(views.HostLookupView, name="compute-01")
        self.client.get_compute_host_details.assert_called_once()


class OldCopyTests(_ViewTestCase):
    """A copy served while a slow backend is refreshed it is marked too."""

    def _old_copies(self, age):
        real = views.cache.get_or_compute

        def get_or_compute(key, ttl, compute):
            value = real(key, ttl, compute)
            if "stored_at" in value:
                value = dict(value, stored_at=time.time() - age)
            return value

        return mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=get_or_compute
        )

    def test_values_older_than_their_lifetime_are_marked(self):
        with self._old_copies(10 * 60):
            for view in (
                views.SummaryView,
                views.DistributionView,
                views.FleetTrendView,
                views.TopHostsView,
            ):
                with self.subTest(view=view.__name__):
                    self.assertTrue(self.render_context(view)["data_stale"])
            lookup = self.render_context(
                views.HostLookupView, name="compute-03"
            )
            self.assertTrue(lookup["data_stale"])

    def test_values_within_their_lifetime_are_not_marked(self):
        with self._old_copies(views.SNAPSHOT_CACHE_TTL):
            for view in (views.SummaryView, views.TopHostsView):
                with self.subTest(view=view.__name__):
                    self.assertFalse(self.render_context(view)["data_stale"])

    def test_each_value_is_aged_against_its_own_lifetime(self):
        # 10 minutes: within the 7d trend's lifetime, past the 6h one's
        with self._old_copies(10 * 60):
            for range_key, stale in (("7d", False), ("6h", True)):
                with self.subTest(range=range_key):
                    context = self.render_context(
                        views.FleetTrendView, range=range_key
                    )
                    self.assertEqual(context["data_stale"], stale)

    def test_age_counts_from_the_end_of_the_computation(self):
        # a slow query does not make the fresh value look old
        clock = [time.time()]

        def slow_fleet_trend(range_key, at=None):
            clock[0] += 120  # the query takes 2 minutes
            return {}

        client = mock.Mock()
        client.get_compute_fleet_trend.side_effect = slow_fleet_trend
        self.mock_get_ds.return_value = client
        with mock.patch(f"{VIEWS}.time.time", side_effect=lambda: clock[0]):
            context = self.render_context(views.FleetTrendView, range="1h")
        client.get_compute_fleet_trend.assert_called_once()
        self.assertFalse(context["data_stale"])

    def test_old_details_alone_are_marked(self):
        real = views.cache.get_or_compute

        def get_or_compute(key, ttl, compute):
            value = real(key, ttl, compute)
            if ":details" in key and "stored_at" in value:
                value = dict(value, stored_at=time.time() - 10 * 60)
            return value

        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=get_or_compute
        ):
            self.assertTrue(
                self.render_context(views.TopHostsView)["data_stale"]
            )


class CacheKeyTests(base.TestCase):
    def test_namespace_and_version(self):
        request = mock.MagicMock()
        request.user.services_region = "RegionOne"
        key = views._cache_key(request, "snapshot")
        # versioned so values cached by older code are never read
        self.assertTrue(
            key.startswith(
                f"grian_ui:compute_host_health:v{views.CACHE_VERSION}:snapshot:"
            )
        )


class FleetTrendCacheTests(_ViewTestCase):
    def test_shared_between_fleet_chart_and_lookup(self):
        self.render_context(views.FleetTrendView, range="6h")
        self.render_context(
            views.HostLookupView, name="compute-01", range="6h"
        )
        self.client.get_compute_fleet_trend.assert_called_once_with(
            "6h", at=mock.ANY
        )

    def test_cached_per_range(self):
        self.render_context(views.FleetTrendView, range="6h")
        self.render_context(views.FleetTrendView, range="24h")
        self.assertEqual(self.client.get_compute_fleet_trend.call_count, 2)

    def test_every_range_has_a_ttl(self):
        self.assertEqual(
            set(views.FLEET_TREND_CACHE_TTL), set(views.timeseries.RANGES)
        )


class TemplateRenderTests(_ViewTestCase):
    """Render the partials with real contexts (fake datasource)."""

    def render(self, view_class, **params):
        context = self.render_context(view_class, **params)
        return render_to_string(view_class.template_name, context)

    def test_summary(self):
        html = self.render(views.SummaryView)
        self.assertIn("Hosts reporting", html)
        self.assertIn("6 hosts need attention", html)
        self.assertIn('data-chh-host="compute-03"', html)
        self.assertIn("chh-clock", html)

    def test_summary_single_host_down_keeps_cards(self):
        self.client.get_compute_stale_hosts.return_value = []
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [],
            "stale": [{"name": "a", "last_seen": time.time() - 900}],
        }
        html = self.render(views.SummaryView)
        self.assertIn("No compute host is reporting", html)
        self.assertIn("The only known host stopped reporting.", html)
        self.assertNotIn("telemetry pipeline is down", html)
        # the cards and attention list are still rendered
        self.assertIn("Hosts reporting", html)
        self.assertIn('data-chh-host="a"', html)

    def test_summary_pipeline_down(self):
        now = time.time()
        self.client.get_compute_stale_hosts.return_value = []
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [],
            "stale": [
                {"name": f"h{i}", "last_seen": now - 900 - i * 30}
                for i in range(4)
            ],
        }
        html = self.render(views.SummaryView)
        self.assertIn("telemetry pipeline is down", html)
        self.assertIn("Hosts reporting", html)

    def test_summary_shows_stale_data_warning(self):
        request_key = (
            "grian_ui:compute_host_health:v{}:snapshot:fake:RegionOne".format(
                views.CACHE_VERSION
            )
        )
        cache.set(
            request_key + ":last",
            {
                "hosts": [{"name": "a", "cpu_avg": 10.0, "iowait_avg": 0.0}],
                "stale": [],
                "fetched_at": time.time() - 120,
            },
            600,
        )
        cache.set(request_key + ":failed", True, 5)
        html = self.render(views.SummaryView)
        self.assertIn("could not be refreshed", html)
        self.assertRegex(html, r'class="chh-clock" data-ts="\d+"')
        # every partial served the stale snapshot says so
        for view_class, params in (
            (views.DistributionView, {}),
            (views.TopHostsView, {}),
            (views.HostLookupView, {"name": "a"}),
        ):
            self.assertIn(
                "Could not be refreshed", self.render(view_class, **params)
            )
        self.client.get_compute_host_snapshot.assert_not_called()

    def test_outage_with_missing_list_is_not_a_missing_monitor(self):
        # nothing reports and the 24 h list failed: the pipeline, not
        # nova.conf
        self.client.get_compute_stale_hosts.side_effect = Exception("slow")
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [],
            "stale": [],
        }
        with self.assertLogs("grian_ui.cache", level="ERROR"):
            html = self.render(views.SummaryView)
        self.assertNotIn("No compute host metrics available", html)
        self.assertIn("could not be loaded", html)
        self.assertIn("telemetry pipeline", html)

    def test_incomplete_list_warning(self):
        self.client.get_compute_stale_hosts.side_effect = Exception("slow")
        with self.assertLogs("grian_ui.cache", level="ERROR"):
            html = self.render(views.SummaryView)
        self.assertIn("may be missing", html)
        self.assertIn("Hosts reporting", html)

    def test_incomplete_stale_table_is_not_shown_as_complete(self):
        self.client.get_compute_stale_hosts.side_effect = Exception("slow")
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [{"name": "a", "cpu_avg": 10.0, "iowait_avg": 0.0}],
            "stale": [],
        }
        with self.assertLogs("grian_ui.cache", level="ERROR"):
            html = self.render(views.TopHostsView, by="stale")
        self.assertIn("No hosts in this list.", html)
        self.assertIn("may be missing", html)
        self.assertNotIn("fa-check-circle", html)
        # the other lists are not affected by the stale list
        self.assertNotIn("may be missing", self.render(views.TopHostsView))

    def test_configuration_error_is_shown_and_logged_once(self):
        views._LOGGED_CONFIG_ERRORS.clear()
        self.addCleanup(views._LOGGED_CONFIG_ERRORS.clear)
        with (
            mock.patch(
                f"{VIEWS}.config.get_compute_host_thresholds",
                side_effect=views.config.ConfigurationError("bad threshold"),
            ),
            self.assertLogs(views.LOG, level="ERROR") as logs,
        ):
            first = self.render(views.SummaryView)
            second = self.render(views.TopHostsView)
        # the message is shown once, by the index; partials point to it
        for html in (first, second):
            self.assertIn("invalid configuration", html)
            self.assertNotIn("bad threshold", html)
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(logs.records[0].exc_info)

    def test_colours_meet_wcag_contrast(self):
        # the index extends horizon's base.html: check its CSS source
        import re as re_mod

        source = get_template(
            "admin/compute_host_health/index.html"
        ).template.source

        def colour(rule):
            match = re_mod.search(
                re_mod.escape(rule) + r" \{ [a-z-]+: (#[0-9a-f]{3,6});", source
            )
            self.assertIsNotNone(match, rule)
            value = match.group(1)
            if len(value) == 4:
                value = "#" + "".join(c * 2 for c in value[1:])
            return value

        def luminance(hex_colour):
            def channel(c):
                c = int(hex_colour[c : c + 2], 16) / 255
                return (
                    c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                )

            return (
                0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)
            )

        def contrast(a, b):
            high, low = sorted((luminance(a), luminance(b)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        white = "#ffffff"
        # text: 4.5:1
        for rule in (
            "#chh-root .label-success",
            "#chh-root .label-warning",
            "#chh-root .label-danger",
            "#chh-root .label-default",
            ".chh-card.chh-ok .panel-heading",
            ".chh-card.chh-warn .panel-heading",
            ".chh-card.chh-crit .panel-heading",
        ):
            self.assertGreaterEqual(contrast(colour(rule), white), 4.5, rule)
        self.assertGreaterEqual(
            contrast(colour("#chh-root .text-muted"), white), 4.5
        )
        # the placeholder of the lookup box (Horizon's theme: #bbb)
        self.assertGreaterEqual(
            contrast(colour("#chh-root #chh-lookup-name::placeholder"), white),
            4.5,
        )
        # highlighted rows, also on hover (Horizon's theme colours)
        for row in ("#f2dede", "#ebcccc", "#fcf8e3", "#faf2cc"):
            for rule in (
                "#chh-root a.chh-host-link",
                ".chh-age",
                "#chh-root .text-warning",
                "#chh-root .text-muted",
            ):
                self.assertGreaterEqual(
                    contrast(colour(rule), row), 4.5, f"{rule} on {row}"
                )
        # pressed buttons: white text at 4.5:1; the page is white too, so
        # this also covers the 3:1 the button needs against the page
        pressed = colour("#chh-root .btn-group .btn.active")
        self.assertGreaterEqual(contrast(pressed, white), 4.5)
        # graphics: 3:1 against the white gaps between them
        for rule in (".chh-seg-user", ".chh-seg-kernel", ".chh-seg-iowait"):
            self.assertGreaterEqual(contrast(colour(rule), white), 3, rule)

    def test_summary_empty(self):
        self.client.get_compute_stale_hosts.return_value = []
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [],
            "stale": [],
        }
        html = self.render(views.SummaryView)
        self.assertIn("No compute host metrics available", html)

    def test_suggestions_are_escaped(self):
        self.client.get_compute_stale_hosts.return_value = []
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [
                {"name": 'evil"<&>', "cpu_avg": 50.0, "iowait_avg": 1.0}
            ],
            "stale": [],
        }
        html = self.render(views.HostSuggestionsView, name="evil")
        self.assertIn("evil&quot;&lt;&amp;&gt;", html)
        self.assertNotIn('evil"<&>', html)

    def test_displayed_host_follows_range_changes(self):
        html = self.render(views.HostLookupView, name="compute-01")
        self.assertIn("chh-range from:body", html)
        self.assertIn("chh-retry", html)

    def test_selected_band_is_pressed(self):
        html = self.render(views.DistributionView, by="band", band="1")
        self.assertEqual(html.count('aria-pressed="true"'), 1)
        self.assertRegex(
            html,
            r'class="btn btn-default active"\s+onclick="[^"]*'
            r"chh.setTop\('band', 1\)",
        )
        html = self.render(views.DistributionView, by="cpu", band="1")
        self.assertNotIn('aria-pressed="true"', html)

    def test_selected_empty_band_stays_focusable(self):
        # one host in the lowest band, one in the highest
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [
                {"name": "low", "cpu_avg": 10.0, "iowait_avg": 0.0},
                {"name": "high", "cpu_avg": 95.0, "iowait_avg": 0.0},
            ],
            "stale": [],
        }
        bands = self.render_context(views.DistributionView)["bands"]
        empty = [i for i, b in enumerate(bands) if not b["count"]]
        self.assertGreater(len(empty), 1)
        html = self.render(views.DistributionView, by="band", band=empty[0])
        self.assertRegex(
            html, rf'id="chh-band-{empty[0]}"[^>]*aria-pressed="true"\s*>'
        )
        # other empty bands cannot be picked, but stay focusable: a
        # focused band that empties on a refresh keeps the focus
        for index in empty[1:]:
            self.assertRegex(
                html, rf'id="chh-band-{index}"[^>]*aria-disabled="true">'
            )
        self.assertNotRegex(html, r"\sdisabled[\s>]")

    def test_lookup_chart_label_uses_the_raw_name(self):
        html = self.render(views.HostLookupView, name="compute-03")
        self.assertIn(
            "%(host)s CPU usage'.split('%(host)s').join(d.name)", html
        )

    def test_fleet_chart_has_a_text_summary(self):
        html = self.render(views.FleetTrendView, range="1h")
        self.assertRegex(
            html, r'aria-describedby="chh-fleet-summary-[0-9a-f]{12}"'
        )
        self.assertIn("Latest values: median", html)

    def test_lookup_announces_the_status(self):
        html = self.render(views.HostLookupView, name="compute-03")
        self.assertRegex(
            html, r"<span hidden data-chh-announce>compute-03: Critical</span>"
        )
        html = self.render(views.HostLookupView, name="nope")
        self.assertIn(
            "<span hidden data-chh-announce>No compute host named nope</span>",
            html,
        )
        html = self.render(views.HostLookupView, name="compute-26")
        self.assertIn(
            "<span hidden data-chh-announce>compute-26: Stale</span>", html
        )

    def test_lookup_degrades_when_the_fleet_median_is_loading(self):
        # the host was found: show its status, retry for the median
        with mock.patch.object(
            views._ComputeHostHealthMixin,
            "get_fleet_trend",
            side_effect=views.cache.StillComputing("busy"),
        ):
            context = self.render_context(
                views.HostLookupView, name="compute-03"
            )
            html = self.render(views.HostLookupView, name="compute-03")
        self.assertTrue(context["found"])
        self.assertNotIn("error_message", context)
        self.assertTrue(context["fleet_unavailable"])
        # the status is final: announced once, not marked as loading
        self.assertIn(
            "<span hidden data-chh-announce>compute-03: Critical</span>", html
        )
        self.assertIn("Loading the fleet median", html)
        self.assertNotIn("No compute host named", html)
        # the retry script must find the displayed host's poller
        self.assertEqual(_retry_target(html), "hx-get")

    def test_lookup_error_announces_the_error(self):
        with mock.patch.object(
            views._ComputeHostHealthMixin,
            "get_snapshot",
            side_effect=views.cache.StillComputing("busy"),
        ):
            html = self.render(views.HostLookupView, name="compute-03")
        self.assertIn(
            "<span hidden data-chh-announce data-chh-loading>"
            "Still loading telemetry data",
            html,
        )

    def _host_data_patches(self, details=None, trends=None):
        """Make the per-host details and/or trends raise."""
        patches = []
        for method, exc in (
            ("get_host_details", details),
            ("get_host_trends", trends),
        ):
            if exc is not None:
                patches.append(
                    mock.patch.object(
                        views._ComputeHostHealthMixin, method, side_effect=exc
                    )
                )
        return patches

    def test_top_hosts_retry_while_their_details_load(self):
        busy = views.cache.StillComputing("busy")
        p1, p2 = self._host_data_patches(details=busy, trends=busy)
        with p1, p2:
            context = self.render_context(views.TopHostsView)
            html = self.render(views.TopHostsView)
        self.assertTrue(context["loading"])
        self.assertIn("Loading the CPU breakdown", html)
        self.assertIn("Loading the trends", html)
        self.assertIn('class="chh-retry"', html)
        # the rows do not claim that the hosts sent no data
        self.assertIn("loading…", html)
        self.assertNotIn("no recent data", html)

    def test_lookup_retries_while_its_details_load(self):
        busy = views.cache.StillComputing("busy")
        p1, p2 = self._host_data_patches(details=busy, trends=busy)
        with p1, p2:
            html = self.render(views.HostLookupView, name="compute-03")
        self.assertIn("Loading the CPU breakdown", html)
        self.assertIn("Loading the trends", html)
        self.assertEqual(_retry_target(html), "hx-get")

    def test_breakdown_does_not_depend_on_the_trends(self):
        failed = views.cache.ComputationFailed("slow 7d query")
        (p1,) = self._host_data_patches(trends=failed)
        with p1:
            top = self.render(views.TopHostsView)
            lookup = self.render_context(
                views.HostLookupView, name="compute-03"
            )
            lookup_html = self.render(views.HostLookupView, name="compute-03")
        for html in (top, lookup_html):
            self.assertIn("The trends could not be loaded.", html)
            self.assertNotIn("CPU breakdown could not be loaded", html)
            # the breakdown is shown
            self.assertIn('class="chh-cpu-bar"', html)
        self.assertNotIn("details_unavailable", lookup)
        self.assertFalse(lookup.get("loading"))

    def test_trends_do_not_depend_on_the_breakdown(self):
        busy = views.cache.StillComputing("busy")
        (p1,) = self._host_data_patches(details=busy)
        with p1:
            context = self.render_context(views.TopHostsView)
            html = self.render(views.TopHostsView)
        self.assertIn("Loading the CPU breakdown", html)
        self.assertNotIn("Loading the trends", html)
        self.assertTrue(any(context["trends"].values()))
        self.assertTrue(context["loading"])

    def test_error_after_a_loading_part_is_not_loading(self):
        # a part still computing, then a real error in the same request
        busy = views.cache.StillComputing("busy")
        (p1,) = self._host_data_patches(details=busy)
        with (
            p1,
            mock.patch(
                f"{VIEWS}.timeseries.gap_filled_series",
                side_effect=RuntimeError("boom"),
            ),
            self.assertLogs(views.LOG, level="ERROR"),
        ):
            context = self.render_context(
                views.HostLookupView, name="compute-03"
            )
        self.assertNotIn("loading", context)
        self.assertNotIn("partial_notes", context)
        with (
            p1,
            mock.patch(
                f"{VIEWS}.timeseries.gap_filled_series",
                side_effect=RuntimeError("boom"),
            ),
            self.assertLogs(views.LOG, level="ERROR"),
        ):
            html = self.render(views.HostLookupView, name="compute-03")
        self.assertNotIn("data-chh-loading", html)
        self.assertNotIn("alert-info", html)
        self.assertNotIn('class="chh-retry"', html)
        self.assertIn("alert-danger", html)

    def _with_stale_copies(self, *parts):
        real = views.cache.get_or_compute

        def get_or_compute(key, ttl, compute):
            value = real(key, ttl, compute)
            if any(f":{part}" in key for part in parts):
                return dict(value, is_stale_copy=True)
            return value

        return mock.patch(
            f"{VIEWS}.cache.get_or_compute", side_effect=get_or_compute
        )

    def test_old_host_data_is_flagged_per_source(self):
        # each source alone must raise the warning
        for part in ("details", "trends"):
            cache.clear()
            with self._with_stale_copies(part):
                top = self.render(views.TopHostsView)
                lookup = self.render(views.HostLookupView, name="compute-03")
            self.assertIn("Could not be refreshed", top, part)
            self.assertIn("Could not be refreshed", lookup, part)
        cache.clear()
        with self._with_stale_copies("fleet"):
            lookup = self.render(views.HostLookupView, name="compute-03")
        self.assertIn("Could not be refreshed", lookup)
        cache.clear()
        with self._with_stale_copies():
            self.assertNotIn(
                "Could not be refreshed", self.render(views.TopHostsView)
            )

    def test_partial_outage_names_the_hosts_that_stopped_together(self):
        now = time.time()
        self.client.get_compute_stale_hosts.return_value = []
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [],
            "stale": [{"name": "old", "last_seen": now - 1500}]
            + [
                {"name": f"h{i}", "last_seen": now - 700 - i * 30}
                for i in range(3)
            ],
        }
        html = self.render(views.SummaryView)
        self.assertIn("3 of the 4 known hosts stopped reporting", html)
        self.assertNotIn("All 4 known hosts", html)

    def test_reporting_hosts_get_their_last_seen_from_the_details(self):
        context = self.render_context(views.TopHostsView)
        reporting = [
            r for r in context["rows"] if r["status"]["key"] != "stale"
        ]
        self.assertTrue(reporting)
        self.assertTrue(all(r["last_seen_age"] for r in reporting))

    def test_top_hosts_render_without_their_details(self):
        self.client.get_compute_host_details.side_effect = Exception("slow")
        with self.assertLogs("grian_ui.cache", level="ERROR"):
            html = self.render(views.TopHostsView)
        self.assertIn('data-chh-host="compute-03"', html)
        self.assertIn("could not be loaded", html)
        self.assertNotIn("alert-danger", html)
        self.assertNotIn("no recent data", html)

    def test_cpu_bar_tooltip_with_a_missing_meter(self):
        detail = {"cpu": 50.0, "user": 40.0, "kernel": None, "iowait": 2.0}
        html = render_to_string(
            "admin/compute_host_health/_cpu_bar.html",
            {"detail": detail, "meters": views._meters(detail)},
        )
        self.assertIn('title="user 40% | kernel – | I/O wait 2%"', html)
        self.assertIn("user 40%, kernel –, I/O wait 2%", html)
        self.assertNotIn("None", html)

    def test_late_lookup_error_replaces_the_announcement(self):
        # the failure comes after the status announcement was prepared
        with (
            mock.patch(
                f"{VIEWS}.timeseries.gap_filled_series",
                side_effect=RuntimeError("boom"),
            ),
            self.assertLogs(views.LOG, level="ERROR"),
        ):
            html = self.render(views.HostLookupView, name="compute-03")
        self.assertRegex(
            html, r"<span hidden data-chh-announce>Error loading telemetry"
        )
        self.assertNotIn("compute-03: Critical", html)

    def test_meters(self):
        self.assertEqual(
            views._meters({"user": 40.0, "kernel": 12.24, "iowait": 0.0}),
            {"user": "40%", "kernel": "12.2%", "iowait": "0%", "idle": "–"},
        )

    def test_lookup_rows_say_loading_while_the_breakdown_loads(self):
        busy = views.cache.StillComputing("busy")
        (p1,) = self._host_data_patches(details=busy)
        with p1:
            html = self.render(views.HostLookupView, name="compute-03")
        self.assertEqual(html.count("loading…"), 4)
        self.assertNotIn("<td>n/a</td>", html)

    def test_lookup_breakdown_with_a_missing_meter(self):
        self.client.get_compute_host_details.return_value = {
            "compute-03": {
                "cpu": 50.0,
                "user": 40.0,
                "kernel": None,
                "iowait": 2.0,
                "idle": 58.0,
                "frequency": None,
                "last_seen": None,
            }
        }
        html = self.render(views.HostLookupView, name="compute-03")
        self.assertIn("<td>40% / – / 2% / 58%</td>", html)

    def test_decorative_icons_are_hidden(self):
        import glob
        import os
        import re

        import grian_ui

        folder = os.path.join(
            os.path.dirname(grian_ui.__file__),
            "templates",
            "admin",
            "compute_host_health",
        )
        self.assertTrue(glob.glob(os.path.join(folder, "*.html")))
        for path in glob.glob(os.path.join(folder, "*.html")):
            with open(path) as f:
                source = f.read()
            # icons sit next to a text that says the same
            for icon in re.findall(r"<i class=\"fa [^>]*>", source):
                self.assertIn('aria-hidden="true"', icon, path)

    def test_cpu_bar_without_details(self):
        def bar(**context):
            return render_to_string(
                "admin/compute_host_health/_cpu_bar.html",
                dict(context, detail=None),
            )

        reporting = {"key": "healthy"}
        self.assertIn(
            "loading…",
            bar(
                status=reporting,
                details_unavailable=True,
                details_loading=True,
            ),
        )
        self.assertIn("n/a", bar(status=reporting, details_unavailable=True))
        # the details came back without the host
        self.assertIn("no recent data", bar(status=reporting))
        # a stale host has no recent data, whatever happened to the details
        self.assertIn(
            "no recent data",
            bar(status={"key": "stale"}, details_unavailable=True),
        )

    def test_lookup_without_details_does_not_claim_no_data(self):
        self.client.get_compute_host_details.side_effect = Exception("slow")
        with self.assertLogs("grian_ui.cache", level="ERROR"):
            html = self.render(views.HostLookupView, name="compute-03")
        self.assertIn("The CPU breakdown could not be loaded.", html)
        # the trend does not depend on the breakdown
        self.assertRegex(html, r'id="chh-host-summary-[0-9a-f]{12}">Latest ')
        self.assertNotIn("no recent data", html)

    def test_interactive_elements_keep_their_ids(self):
        # htmx gives focus back to an element with the same id after a swap
        top = self.render(views.TopHostsView)
        for element_id in (
            "chh-sort-cpu",
            "chh-sort-iowait",
            "chh-sort-stale",
            "chh-top-host-compute-03",
        ):
            self.assertIn(f'id="{element_id}"', top)
        self.assertIn('id="chh-band-0"', self.render(views.DistributionView))
        self.assertIn(
            'id="chh-attention-host-compute-03"',
            self.render(views.SummaryView),
        )

    def test_trends_have_text_alternatives(self):
        top = self.render(views.TopHostsView)
        self.assertRegex(
            top,
            r'<span class="sr-only">latest [\d.]+%, lowest [\d.]+%, highest',
        )
        lookup = self.render(views.HostLookupView, name="compute-03")
        self.assertRegex(
            lookup, r'aria-describedby="chh-host-summary-[0-9a-f]{12}"'
        )
        self.assertIn("in the range.", lookup)

    def test_trend_summary(self):
        self.assertIsNone(views._trend_summary([None, None]))
        self.assertEqual(
            views._trend_summary([None, 10.04, 50.0, None, 30.0]),
            {"latest": 30.0, "min": 10.0, "max": 50.0},
        )

    def test_wrapped_configuration_error_shows_its_message(self):
        views._LOGGED_CONFIG_ERRORS.clear()
        self.addCleanup(views._LOGGED_CONFIG_ERRORS.clear)
        error = views.cache.ComputationFailed("computing failed")
        error.__cause__ = views.config.ConfigurationError(
            "OPENSTACK_KEYSTONE_URL is not configured"
        )
        with (
            mock.patch(f"{VIEWS}.cache.get_or_compute", side_effect=error),
            self.assertLogs(views.LOG, level="ERROR"),
        ):
            html = self.render(views.SummaryView)
        self.assertIn("invalid configuration", html)

    def test_every_section_shows_a_datasource_configuration_error(self):
        # with the real cache: requests that wait for a failed shared
        # computation get no cause, so the settings are checked first
        views._LOGGED_CONFIG_ERRORS.clear()
        self.addCleanup(views._LOGGED_CONFIG_ERRORS.clear)
        self.mock_get_ds.side_effect = views.config.ConfigurationError(
            "OPENSTACK_KEYSTONE_URL is not configured in settings"
        )
        with (
            mock.patch(
                f"{VIEWS}.config.get_datasource_type",
                return_value=views.config.DATASOURCE_PROMETHEUS,
            ),
            mock.patch(
                f"{VIEWS}.config.get_openstack_keystone_url",
                side_effect=views.config.ConfigurationError(
                    "OPENSTACK_KEYSTONE_URL is not configured in settings"
                ),
            ),
            self.assertLogs(views.LOG, level="ERROR"),
        ):
            for view in (
                views.SummaryView,
                views.DistributionView,
                views.FleetTrendView,
                views.TopHostsView,
            ):
                with self.subTest(view=view.__name__):
                    self.assertIn("invalid configuration", self.render(view))
            html = self.render(views.HostLookupView, name="compute-03")
            self.assertIn("invalid configuration", html)

    def test_index_checks_the_datasource_settings(self):
        with (
            mock.patch(
                f"{VIEWS}.config.get_datasource_type",
                return_value=views.config.DATASOURCE_PROMETHEUS,
            ),
            mock.patch(
                f"{VIEWS}.config.get_openstack_keystone_url",
                side_effect=views.config.ConfigurationError(
                    "OPENSTACK_KEYSTONE_URL is not configured in settings"
                ),
            ),
        ):
            context = self.render_context(views.ComputeHostHealthIndexView)
        self.assertEqual(
            context["error_messages"],
            ["OPENSTACK_KEYSTONE_URL is not configured in settings"],
        )

    def test_host_poller_queues_behind_range_changes(self):
        html = self.render(views.HostLookupView, name="compute-03", range="7d")
        self.assertIn('hx-sync="#chh-lookup-panel:queue last"', html)
        # the page fetches the host again if the range changed meanwhile
        self.assertIn('data-chh-range="7d"', html)

    def test_sparklines_are_drawn_in_their_own_table(self):
        html = self.render(views.TopHostsView)
        self.assertIn(".closest('.panel')", html)
        self.assertNotIn(
            "document.querySelectorAll('canvas[data-chh-spark]')", html
        )

    def test_band_selection_is_text_not_a_button(self):
        html = self.render(views.TopHostsView, by="band", band="0")
        self.assertIn("Listing the CPU", html)
        self.assertEqual(html.count('aria-pressed="true"'), 0)

    def test_empty_list_check_mark_only_for_stale(self):
        self.client.get_compute_stale_hosts.return_value = []
        self.client.get_compute_host_snapshot.return_value = {
            "hosts": [],
            "stale": [],
        }
        self.assertIn(
            "fa-check-circle", self.render(views.TopHostsView, by="stale")
        )
        self.assertNotIn("fa-check-circle", self.render(views.TopHostsView))

    def test_summary_cards_share_their_height(self):
        html = self.render(views.SummaryView)
        self.assertIn('<div class="row chh-cards">', html)
        self.assertIn("critical | ", html)

    def test_cpu_breakdown_has_text(self):
        html = self.render(views.TopHostsView)
        self.assertRegex(
            html,
            r'<span class="sr-only">user [\d.]+%, kernel [\d.]+%, I/O wait',
        )
        self.assertIn('class="chh-cpu-bar" aria-hidden="true"', html)

    def test_suggestions_ignore_configuration_errors(self):
        with (
            mock.patch(
                f"{VIEWS}.config.get_compute_host_thresholds",
                side_effect=views.config.ConfigurationError("bad"),
            ),
            self.assertNoLogs(views.LOG, level="INFO"),
        ):
            html = self.render(views.HostSuggestionsView, name="c")
        self.assertNotIn("<option", html)

    def test_summary_loading_retries(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=views.cache.StillComputing("busy"),
        ):
            html = self.render(views.SummaryView)
        self.assertIn("alert-info", html)
        # only this partial reloads, not every section of the page
        self.assertNotIn("document.body", html)
        self.assertIn("htmx.trigger(target, 'chh-retry')", html)
        self.assertRegex(
            html,
            r'<span id="chh-retry-[0-9a-f]{12}" class="chh-retry" hidden>',
        )

    def test_summary_error(self):
        self.client.get_compute_host_snapshot.side_effect = Exception("x")
        with self.assertLogs("grian_ui.cache", level="ERROR"):
            html = self.render(views.SummaryView)
        self.assertIn("alert-danger", html)

    def test_distribution(self):
        html = self.render(views.DistributionView)
        self.assertRegex(html, r'id="chh-dist-data-[0-9a-f]{12}"')
        # keyboard-accessible band buttons
        self.assertIn("chh.setTop('band', 4)", html)

    def test_element_ids_are_unique_per_response(self):
        """
        Regression: re-rendered partials must not reuse element ids.

        htmx "settles" a new element with the attributes of the old element
        that has the same id, after the inline script drew the chart; that
        reset the canvas size and cleared the chart on every range change.
        """
        import re

        for view_class, params in (
            (views.FleetTrendView, {"range": "6h"}),
            (views.DistributionView, {}),
            (views.TopHostsView, {}),
            (views.HostLookupView, {"name": "compute-10"}),
        ):
            first = set(
                re.findall(r'id="([^"]+)"', self.render(view_class, **params))
            )
            second = set(
                re.findall(r'id="([^"]+)"', self.render(view_class, **params))
            )
            # controls keep stable ids on purpose, so htmx restores focus
            # to them; only their attributes are settled, which is harmless
            stable = re.compile(
                r"chh-(sort-|band-\d|top-host-|attention-host-)"
            )
            first = {i for i in first if not stable.match(i)}
            second = {i for i in second if not stable.match(i)}
            self.assertTrue(first, view_class.__name__)
            self.assertFalse(first & second, view_class.__name__)

    def test_fleet_trend_has_gaps_as_null(self):
        html = self.render(views.FleetTrendView, range="1h")
        self.assertRegex(html, r'id="chh-fleet-data-[0-9a-f]{12}"')
        self.assertNotIn("NaN", html)

    def test_top_hosts(self):
        html = self.render(views.TopHostsView)
        self.assertIn('data-chh-spark="compute-03"', html)
        self.assertIn("Showing 10 of 24 matching hosts.", html)
        # widths are not localized (CSS needs a dot)
        self.assertRegex(html, r"width: \d+(\.\d)?%;")

    def test_top_hosts_stale(self):
        html = self.render(views.TopHostsView, by="stale")
        self.assertIn("compute-26", html)
        self.assertIn("minutes ago", html)

    def test_host_lookup(self):
        html = self.render(views.HostLookupView, name="compute-10", range="1h")
        self.assertRegex(html, r'id="chh-host-data-[0-9a-f]{12}"')
        self.assertIn(
            "/dashboard/admin/compute_host_health/host/?name=compute-10", html
        )

    def test_host_lookup_unknown_is_escaped(self):
        html = self.render(views.HostLookupView, name="<b>x</b>")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)

    def test_host_lookup_empty(self):
        html = self.render(views.HostLookupView)
        self.assertIn("chh.destroyChart('host')", html)
