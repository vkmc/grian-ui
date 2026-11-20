#
# SPDX-License-Identifier: Apache-2.0

import re

from unittest import mock

from grian_ui import config
from grian_ui import project_storage as ps
from grian_ui.datasource import fake
from grian_ui.datasource import fake_project_storage
from grian_ui.datasource import prometheus_project_storage as queries
from grian_ui_tests.unit import base
from grian_ui_tests.unit.test_datasource_prometheus import _make_client


MATCHER = 'project="b8753cd7d01b4455b774b46991f36f83"'
DAY = ps.DAY_SECONDS
T = config.DEFAULT_PROJECT_STORAGE_THRESHOLDS
METRICS = ("ceilometer_volume_size", "ceilometer_volume_snapshot_size")


def _matrix(values):
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {}, "values": values}] if values else [],
        },
    }


def _all_queries():
    return list(queries.footprint_queries(MATCHER, 30).values()) + list(
        queries.trend_queries(MATCHER, 30).values()
    )


class QueryTests(base.TestCase):
    def test_every_selector_has_the_project_and_a_metric_name(self):
        for query in _all_queries():
            with self.subTest(query=query):
                selectors = re.findall(r"([A-Za-z_:]*)\{([^}]*)\}", query)
                self.assertTrue(selectors)
                for name, labels in selectors:
                    # Aetos rejects selectors without a metric name
                    self.assertIn(name, METRICS)
                    self.assertEqual(labels, MATCHER)
                self.assertNotIn("__name__", query)

    def test_footprint_queries(self):
        self.assertEqual(
            queries.footprint_queries(MATCHER, 15),
            {
                "volumes": "(sum(max by (resource) (last_over_time("
                f"ceilometer_volume_size{{{MATCHER}}}[10m]))))[15d:1d]",
                "snapshots": "(sum(max by (resource) (last_over_time("
                f"ceilometer_volume_snapshot_size{{{MATCHER}}}[10m]))))"
                "[15d:1d]",
            },
        )

    def test_one_series_per_volume(self):
        # a renamed volume has two series (resource_name changes) in the
        # window: grouped by resource first, it counts once
        for query in _all_queries():
            with self.subTest(query=query):
                self.assertRegex(
                    query,
                    r"^\((sum|count)\(max by \(resource\) \(last_over_time\(",
                )

    def test_trend_queries(self):
        found = queries.trend_queries(MATCHER, 30)
        self.assertEqual(
            sorted(found),
            ["snapshots_count", "snapshots_gb", "volumes_count", "volumes_gb"],
        )
        self.assertTrue(found["volumes_count"].startswith("(count("))
        self.assertTrue(found["volumes_gb"].startswith("(sum("))
        for query in found.values():
            self.assertTrue(query.endswith("[30d:6h]"), query)

    def test_matcher_is_checked(self):
        for bad in (
            "",
            None,
            'project=~".*"',
            'project="a"} or vector(1) #"',
            'project="a",user="b"',
            'project="a|b"',
        ):
            with self.subTest(matcher=bad):
                self.assertRaises(
                    ValueError, queries.footprint_queries, bad, 7
                )
                self.assertRaises(ValueError, queries.trend_queries, bad, 7)

    def test_days_are_checked(self):
        self.assertRaises(ValueError, queries.footprint_queries, MATCHER, 0)
        self.assertIn(
            "[7d:1d]", queries.footprint_queries(MATCHER, "7")["volumes"]
        )


class FootprintTests(base.TestCase):
    def test_parses_both_series(self):
        client, prom = _make_client()
        prom._get.side_effect = [
            _matrix([[DAY, "10"], [2 * DAY, "12.3456"]]),
            _matrix([[2 * DAY, "5"]]),
        ]
        found = queries.get_footprint(client, MATCHER, 15, at=2 * DAY + 60)
        self.assertEqual(
            found,
            {
                "volumes": [[DAY, 10.0], [2 * DAY, 12.346]],
                "snapshots": [[2 * DAY, 5.0]],
            },
        )
        calls = prom._get.call_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call.args[0], "query")
            self.assertIn(MATCHER, call.args[1]["query"])
            # evaluated at the time the chart grid is built from
            self.assertEqual(call.args[1]["time"], f"{2 * DAY + 60:.3f}")

    def test_empty(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([])
        self.assertEqual(
            queries.get_footprint(client, MATCHER, 7),
            {"volumes": [], "snapshots": []},
        )

    def test_through_the_client(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([[DAY, "1"]])
        found = client.get_project_storage_footprint(MATCHER, 7, at=DAY)
        self.assertEqual(found["volumes"], [[DAY, 1.0]])
        self.assertIn("[7d:1d]", prom._get.call_args.args[1]["query"])


class TrendTests(base.TestCase):
    def answers(self, by_name):
        def answer(_path, params):
            query = params["query"]
            kind = "count" if query.startswith("(count(") else "gb"
            metric = (
                "snapshots"
                if "ceilometer_volume_snapshot_size" in query
                else "volumes"
            )
            return _matrix(by_name.get(f"{metric}_{kind}", []))

        return answer

    def test_combines_volumes_and_snapshots(self):
        client, prom = _make_client()
        prom._get.side_effect = self.answers(
            {
                "volumes_gb": [[1, "10"], [2, "12"], [3, "12"]],
                "volumes_count": [[1, "1"], [2, "2"], [3, "2"]],
                # snapshots only from t=3
                "snapshots_gb": [[3, "5"]],
                "snapshots_count": [[3, "1"]],
            }
        )
        found = client.get_project_storage_trend(MATCHER, 15, at=3)
        self.assertEqual(found["gigabytes"], [[1, 10.0], [2, 12.0], [3, 17.0]])
        self.assertEqual(found["volumes"], [[1, 1.0], [2, 2.0], [3, 2.0]])
        # missing snapshots count as 0 while volumes were reported
        self.assertEqual(found["snapshots"], [[1, 0], [2, 0], [3, 1.0]])
        self.assertEqual(prom._get.call_count, 4)
        for call in prom._get.call_args_list:
            self.assertIn(MATCHER, call.args[1]["query"])
            self.assertIn("[15d:6h]", call.args[1]["query"])

    def test_no_history(self):
        client, prom = _make_client()
        prom._get.side_effect = self.answers({})
        self.assertEqual(
            queries.get_trend(client, MATCHER, 15),
            {"gigabytes": [], "volumes": [], "snapshots": []},
        )

    def test_rejects_a_bad_matcher_before_querying(self):
        client, prom = _make_client()
        self.assertRaises(
            ValueError, queries.get_trend, client, 'project=~".+"', 15
        )
        prom._get.assert_not_called()


class FakeDatasourceTests(base.TestCase):
    """The sample project agrees across the panel."""

    def setUp(self):
        super().setUp()
        self.client = fake.get_client()
        self.now = 1_795_000_000.0

    def test_footprint_ends_at_the_api_totals(self):
        found = self.client.get_project_storage_footprint(
            MATCHER, 30, at=self.now
        )
        self.assertEqual(len(found["volumes"]), 30)
        self.assertEqual(len(found["snapshots"]), 30)
        # points on the daily subquery grid
        grid = ps.day_grid(30, self.now)
        self.assertEqual([t for t, _v in found["volumes"]], grid)
        # growing towards today's 550 GB of volumes and 290 GB of snapshots
        values = [v for _t, v in found["volumes"]]
        self.assertEqual(values, sorted(values))
        self.assertLess(values[-1], 550)
        self.assertGreater(values[-1], 545)

    def test_footprint_respects_the_days_asked(self):
        found = self.client.get_project_storage_footprint(
            MATCHER, 15, at=self.now
        )
        self.assertEqual(len(found["volumes"]), 15)

    def test_trend(self):
        found = self.client.get_project_storage_trend(MATCHER, 30, at=self.now)
        self.assertEqual(len(found["gigabytes"]), 30 * 4)
        self.assertEqual({v for _t, v in found["volumes"]}, {5})
        self.assertEqual({v for _t, v in found["snapshots"]}, {3})
        block = fake_project_storage.block_storage(self.now)
        rows = ps.quota_rows(block, T)
        forecasts = ps.quota_forecasts(rows, found, T)
        # the mockup sample: about six weeks to a full gigabytes quota
        self.assertEqual(forecasts["gigabytes"]["state"], ps.FORECAST_DAYS)
        self.assertTrue(35 <= forecasts["gigabytes"]["days"] <= 50)
        self.assertEqual(forecasts["volumes"]["state"], ps.FORECAST_FLAT)

    def test_inventory_sample(self):
        block = fake_project_storage.block_storage(self.now)
        use = ps.usage(block)
        self.assertEqual(use["gigabytes"], 840)
        self.assertEqual((use["volumes"], use["unattached"]), (5, 2))
        self.assertEqual(use["snapshots"], 3)
        servers = fake_project_storage.servers()
        images = fake_project_storage.images(self.now)
        users = ps.image_users(servers, block["volumes"])
        by_name = {i["name"]: users.get(i["id"], 0) for i in images}
        self.assertEqual(
            by_name,
            {
                "web-golden-2026-10": 6,
                "web-golden-2026-06": 0,
                "batch-worker": 2,
            },
        )
        # attachments point at the sample instances
        names = {s["id"]: s["name"] for s in servers}
        attached = {
            v["name"]: names[v["server_ids"][0]]
            for v in block["volumes"]
            if v["server_ids"]
        }
        self.assertEqual(
            attached,
            {
                "db-data": "db-01",
                "web-assets": "web-01",
                "scratch": "batch-01",
            },
        )

    def test_ignores_the_matcher(self):
        with mock.patch.object(
            fake_project_storage, "_totals", wraps=fake_project_storage._totals
        ) as totals:
            self.client.get_project_storage_trend("anything", 3)
        totals.assert_called_once()
