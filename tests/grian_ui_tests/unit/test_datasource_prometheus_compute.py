#
# SPDX-License-Identifier: Apache-2.0

from grian_ui.datasource import prometheus
from grian_ui_tests.unit import base
from grian_ui_tests.unit.test_datasource_prometheus import _make_client
from grian_ui_tests.unit.test_datasource_prometheus import _make_metric


def _host_metric(host, value, **labels):
    labels = dict(labels, vm_instance=f"compute.{host}")
    return _make_metric(value, labels)


def _matrix(series):
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": labels, "values": values}
                for labels, values in series
            ],
        },
    }


class HostSelectorTests(base.TestCase):
    def test_escapes_dots_for_promql(self):
        selector = prometheus._host_selector(["node-1.example.com"])
        self.assertEqual(
            selector,
            'vm_instance=~"compute\\\\.(node\\\\-1\\\\.example\\\\.com)"',
        )

    def test_multiple_hosts(self):
        selector = prometheus._host_selector(["a", "b"])
        self.assertIn("(a|b)", selector)

    def test_rejects_trailing_newline(self):
        self.assertIsNone(prometheus._host_selector(["cmp-1\n"]))

    def test_rejects_injection(self):
        self.assertIsNone(prometheus._host_selector(['a"} or vector(1) #']))
        self.assertIsNone(prometheus._host_selector(["a|b"]))
        self.assertIsNone(prometheus._host_selector([""]))
        self.assertIsNone(prometheus._host_selector([]))

    def test_drops_invalid_and_keeps_valid(self):
        selector = prometheus._host_selector(["good", "bad name"])
        self.assertIn("(good)", selector)
        self.assertNotIn("bad", selector)


class ComputeHostSnapshotTests(base.TestCase):
    def test_snapshot(self):
        client, prom = _make_client()

        def query(query):
            if query.startswith("max by (vm_instance) (max_over_time"):
                return [
                    _host_metric("gone", "1700000000"),
                    # a host both fresh and "stale" must not be duplicated
                    _host_metric("cmp-1", "1700000001"),
                ]
            if "iowait" in query:
                return [_host_metric("cmp-1", "12.34")]
            return [
                _host_metric("cmp-1", "80"),
                _host_metric("cmp-2", "NaN"),
                _make_metric("5", {}),  # no host label: ignored
            ]

        prom.query.side_effect = query
        snapshot = client.get_compute_host_snapshot()

        self.assertEqual(
            sorted(snapshot["hosts"], key=lambda h: h["name"]),
            [
                {"name": "cmp-1", "cpu_avg": 80.0, "iowait_avg": 12.34},
                {"name": "cmp-2", "cpu_avg": None, "iowait_avg": None},
            ],
        )
        self.assertEqual(
            snapshot["stale"], [{"name": "gone", "last_seen": 1700000000.0}]
        )
        self.assertEqual(prom.query.call_count, 3)

    def test_snapshot_queries_are_fleet_wide(self):
        client, prom = _make_client()
        prom.query.return_value = []
        client.get_compute_host_snapshot()
        queries = [c.kwargs["query"] for c in prom.query.call_args_list]
        for query in queries:
            self.assertTrue(query.startswith("max by (vm_instance)"))
        # only compute host series, like the snapshot's host parsing
        selector = 'ceilometer_compute_node_cpu_percent{vm_instance=~"compute\\\\..+"}'
        self.assertIn(f"avg_over_time({selector}[15m])", queries[0])
        for query in queries:
            self.assertNotIn("_percent[", query)
        self.assertIn("unless on (vm_instance)", queries[2])
        # averages only for hosts that reported in STALE_AFTER: a host
        # silent for 10-15 minutes still has a 15 minute average
        fresh = (
            "and on (vm_instance) max by (vm_instance) (last_over_time("
            'ceilometer_compute_node_cpu_percent{vm_instance=~"compute\\\\..+"}[10m]))'
        )
        self.assertIn(fresh, queries[0])
        self.assertIn(fresh, queries[1])

    def test_snapshot_empty(self):
        client, prom = _make_client()
        prom.query.return_value = []
        self.assertEqual(
            client.get_compute_host_snapshot(), {"hosts": [], "stale": []}
        )


class HostLabelTests(base.TestCase):
    def test_only_compute_prefixed_labels_are_hosts(self):
        self.assertEqual(
            prometheus._host_from_labels({"vm_instance": "compute.cmp-1"}),
            "cmp-1",
        )
        for labels in (
            {"vm_instance": "cmp-1"},
            {"vm_instance": "compute."},
            {},
        ):
            self.assertIsNone(prometheus._host_from_labels(labels))


class StaleHostQueriesTests(base.TestCase):
    def test_snapshot_only_looks_back_briefly(self):
        client, prom = _make_client()
        prom.query.return_value = []
        client.get_compute_host_snapshot()
        queries = [c.kwargs["query"] for c in prom.query.call_args_list]
        self.assertTrue(any("[30m:1m]" in q for q in queries))
        self.assertFalse(any("[24h:" in q for q in queries))

    def test_long_stale_list(self):
        client, prom = _make_client()
        prom.query.return_value = [
            _host_metric("gone", "1700000000"),
            _host_metric("bad", "NaN"),
        ]
        self.assertEqual(
            client.get_compute_stale_hosts(),
            [{"name": "gone", "last_seen": 1700000000.0}],
        )
        query = prom.query.call_args.kwargs["query"]
        self.assertIn("[24h:5m]", query)
        # hosts that reported within STALE_AFTER are not stale
        fresh = (
            "unless on (vm_instance) max by (vm_instance) (last_over_time("
            'ceilometer_compute_node_cpu_percent{vm_instance=~"compute\\\\..+"}'
            "[10m]))"
        )
        self.assertTrue(query.endswith(fresh), query)


class ComputeHostDetailsTests(base.TestCase):
    def test_details(self):
        client, prom = _make_client()

        def query(query):
            if query.startswith("max by (vm_instance) (max_over_time"):
                return [_host_metric("cmp-1", "1700000123.5")]
            return [
                _host_metric("cmp-1", "40.04", field="cpu"),
                _host_metric("cmp-1", "30", field="user"),
                _host_metric("cmp-1", "2100", field="frequency"),
                _host_metric("cmp-1", "1", field="bogus"),
                _host_metric("cmp-1", "1"),  # no field label
            ]

        prom.query.side_effect = query
        details = client.get_compute_host_details(["cmp-1"])
        self.assertEqual(details["cmp-1"]["cpu"], 40.04)
        self.assertEqual(details["cmp-1"]["user"], 30.0)
        self.assertEqual(details["cmp-1"]["frequency"], 2100.0)
        self.assertIsNone(details["cmp-1"]["kernel"])
        self.assertNotIn("bogus", details["cmp-1"])
        self.assertEqual(details["cmp-1"]["last_seen"], 1700000123.5)

    def test_details_use_the_status_freshness_window(self):
        client, prom = _make_client()
        prom.query.return_value = []
        client.get_compute_host_details(["cmp-1"])
        values_query, last_seen_query = [
            c.kwargs["query"] for c in prom.query.call_args_list
        ]
        # one union query, last sample within 10m for each metric
        self.assertEqual(values_query.count("last_over_time("), 6)
        self.assertEqual(values_query.count("[10m]"), 6)
        self.assertIn('"field", "iowait"', values_query)
        self.assertIn("[10m:15s]", last_seen_query)

    def test_details_skip_invalid_names(self):
        client, prom = _make_client()
        self.assertEqual(client.get_compute_host_details(["bad name"]), {})
        prom.query.assert_not_called()


class ComputeTrendTests(base.TestCase):
    def test_host_trends(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix(
            [
                (
                    {"vm_instance": "compute.cmp-1"},
                    [
                        [1700000000, "10"],
                        [1700000300, "NaN"],
                        [1700000600, "12.25"],
                    ],
                )
            ]
        )
        trends = client.get_compute_host_trends(["cmp-1"], "6h")
        self.assertEqual(
            trends, {"cmp-1": [[1700000000.0, 10.0], [1700000600.0, 12.2]]}
        )
        query = prom._get.call_args.args[1]["query"]
        self.assertTrue(query.endswith("[6h:5m]"))
        self.assertIn('vm_instance=~"compute\\\\.(cmp\\\\-1)"', query)

    def test_trends_are_evaluated_at_the_grid_time(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([])
        client.get_compute_host_trends(["cmp-1"], "6h", at=1700000123.0)
        self.assertEqual(prom._get.call_args.args[1]["time"], "1700000123.000")
        client.get_compute_fleet_trend("6h", at=1700000456.0)
        self.assertEqual(prom._get.call_args.args[1]["time"], "1700000456.000")

    def test_host_trends_invalid_names(self):
        client, prom = _make_client()
        self.assertEqual(client.get_compute_host_trends(["a b"], "6h"), {})
        prom._get.assert_not_called()

    def test_fleet_trend(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix(
            [
                ({"stat": "p50"}, [[1, "20"]]),
                ({"stat": "p95"}, [[1, "70"]]),
                ({"stat": "max"}, [[1, "95"]]),
                ({"stat": "other"}, [[1, "1"]]),
            ]
        )
        trend = client.get_compute_fleet_trend("24h")
        self.assertEqual(
            trend,
            {"p50": [[1.0, 20.0]], "p95": [[1.0, 70.0]], "max": [[1.0, 95.0]]},
        )
        query = prom._get.call_args.args[1]["query"]
        self.assertTrue(query.endswith("[24h:15m]"))
        # percentiles over one value per host, not over raw series
        self.assertIn(
            "quantile(0.95, max by (vm_instance) (avg_over_time(", query
        )
        self.assertIn("max(max by (vm_instance) (avg_over_time(", query)

    def test_fleet_trend_selects_compute_hosts_only(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([])
        client.get_compute_fleet_trend("1h")
        query = prom._get.call_args.args[1]["query"]
        self.assertIn('{vm_instance=~"compute\\\\..+"}[1m]', query)

    def test_fleet_trend_unknown_range_uses_default(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([])
        client.get_compute_fleet_trend("bogus")
        self.assertTrue(
            prom._get.call_args.args[1]["query"].endswith("[6h:5m]")
        )
