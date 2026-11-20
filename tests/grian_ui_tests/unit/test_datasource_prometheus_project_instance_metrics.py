#
# SPDX-License-Identifier: Apache-2.0

import re

from grian_ui import project_instance_metrics as pim
from grian_ui.datasource import prometheus_project_instance_metrics as queries
from grian_ui_tests.unit import base
from grian_ui_tests.unit.test_datasource_prometheus import _make_client


ID = "c726b640-2a18-41e3-9fac-af2730c12410"
OTHER_ID = "d414fbd9-66a6-485c-bec7-8191c2fa4221"
PROJECT = "b8753cd7d01b4455b774b46991f36f83"
MATCHER = f'project="{PROJECT}"'
SELECTOR = re.compile(r"([A-Za-z_:][A-Za-z0-9_:]*)?\{([^{}]*)\}")
POLLED = 'resource_name=~".+:instance-.+"'


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


def _selectors(query):
    return SELECTOR.findall(query)


class QueryTests(base.TestCase):
    """What every query sends to Aetos."""

    def queries(self, range_key="24h", window="10m"):
        for group in pim.GROUPS:
            yield (
                group,
                queries.group_query(MATCHER, group, ID, range_key, window),
            )

    def test_every_selector_names_its_metric(self):
        for group, query in self.queries():
            selectors = _selectors(query)
            self.assertTrue(selectors, group)
            for metric, _labels in selectors:
                with self.subTest(group=group, metric=metric):
                    # Aetos rejects {__name__=~...} for project users
                    self.assertTrue(metric.startswith("ceilometer_"))
            self.assertNotIn("__name__", query)

    def test_every_selector_has_the_project(self):
        for group, query in self.queries():
            for metric, labels in _selectors(query):
                with self.subTest(group=group, metric=metric):
                    self.assertEqual(labels.split(",")[0], MATCHER)
                    self.assertEqual(labels.count("project="), 1)

    def test_every_selector_has_the_instance_series_filter(self):
        for group, query in self.queries():
            for metric, labels in _selectors(query):
                with self.subTest(group=group, metric=metric):
                    if metric.startswith("ceilometer_disk_device_"):
                        self.assertIn(f'resource=~"{ID}-.+"', labels)
                        self.assertIn(POLLED, labels)
                    elif metric.startswith("ceilometer_network_"):
                        # polled only; resource_name is "<name>:<vNIC>"
                        self.assertIn(f'resource=~".+-{ID}-.+"', labels)
                        self.assertNotIn("resource_name", labels)
                    else:
                        self.assertIn(f'resource="{ID}"', labels)
                        self.assertIn(POLLED, labels)
                    self.assertNotIn(OTHER_ID, labels)

    def test_one_subquery_per_group_with_every_series(self):
        for group, query in self.queries():
            self.assertTrue(query.endswith(")[24h:15m]"), query)
            tags = re.findall(r'"series", "([a-z_]+)"', query)
            self.assertEqual(tuple(tags), pim.SERIES[group])
            self.assertEqual(query.count(" or label_replace("), len(tags) - 1)

    def test_metrics_and_units(self):
        found = dict(self.queries())
        cpu = found["cpu"]
        self.assertIn("sum(rate(ceilometer_cpu{", cpu)
        self.assertIn("sum(rate(ceilometer_cpu_delay{", cpu)
        self.assertEqual(cpu.count("/ 1e9"), 2)
        self.assertIn("max(last_over_time(ceilometer_vcpus{", cpu)
        memory = found["memory"]
        self.assertIn("max(last_over_time(ceilometer_memory_usage{", memory)
        self.assertIn("max(last_over_time(ceilometer_memory{", memory)
        self.assertIn("ceilometer_memory_swap_in{", memory)
        self.assertIn("ceilometer_memory_swap_out{", memory)
        disk = found["disk"]
        for metric in (
            "read_bytes",
            "write_bytes",
            "write_latency",
            "write_requests",
        ):
            self.assertIn(f"rate(ceilometer_disk_device_{metric}{{", disk)
        # latency is in ns: ns per request / 1e6 = ms per request
        self.assertIn("/ 1e6", disk)
        network = found["network"]
        self.assertIn("rate(ceilometer_network_incoming_bytes{", network)
        self.assertIn("rate(ceilometer_network_outgoing_bytes{", network)

    def test_swap_is_reported_when_either_series_exists(self):
        sel = queries._selectors(MATCHER, ID)["instance"]
        swap_in = f"sum(rate(ceilometer_memory_swap_in{{{sel}}}[10m]))"
        swap_out = f"sum(rate(ceilometer_memory_swap_out{{{sel}}}[10m]))"
        # "in + out" alone returns nothing when only one is reported
        self.assertIn(
            f'label_replace(({swap_in} or {swap_out} * 0) + '
            f'({swap_out} or {swap_in} * 0), "series", "swap", "", "")',
            queries.group_query(MATCHER, "memory", ID, "1h", "10m"),
        )

    def test_rate_window_and_step_per_range(self):
        for range_key, suffix, window in (
            ("1h", "[1h:1m]", "[10m]"),
            ("6h", "[6h:5m]", "[10m]"),
            ("24h", "[24h:15m]", "[15m]"),
            ("7d", "[7d:1h]", "[1h]"),
        ):
            query = queries.group_query(MATCHER, "cpu", ID, range_key, "10m")
            with self.subTest(range_key=range_key):
                self.assertTrue(query.endswith(suffix))
                self.assertIn(window, query)
        query = queries.group_query(MATCHER, "cpu", ID, "1h", "5m")
        self.assertIn("[5m]", query)

    def test_invalid_input_is_never_queried(self):
        for instance_id in (ID.upper(), f'{ID}"}} or vector(1) #', ""):
            with self.assertRaises(ValueError):
                queries.group_query(MATCHER, "cpu", instance_id, "1h", "10m")
        for matcher in (
            "",
            None,
            'project=~".*"',
            'project="a"} or vector(1) #"',
            f"{MATCHER},x",
        ):
            with self.assertRaises(ValueError):
                queries.group_query(matcher, "cpu", ID, "1h", "10m")
        with self.assertRaises(ValueError):
            queries.group_query(MATCHER, "gpu", ID, "1h", "10m")


class GetInstanceMetricsTests(base.TestCase):
    def test_series_by_label(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix(
            [
                (
                    {"series": "cpu"},
                    [[1700000000, "0.63541"], [1700000060, "NaN"]],
                ),
                ({"series": "vcpus"}, [[1700000000, "1"]]),
                ({"series": "unexpected"}, [[1700000000, "9"]]),
            ]
        )
        series = client.get_project_instance_metrics(
            MATCHER, "cpu", ID, "1h", "10m", at=1700000090
        )
        self.assertEqual(
            series,
            {
                "cpu": [[1700000000.0, 0.6354]],
                "cpu_wait": [],
                "vcpus": [[1700000000.0, 1.0]],
            },
        )
        path, params = prom._get.call_args.args
        self.assertEqual(path, "query")
        self.assertEqual(params["time"], "1700000090.000")
        self.assertEqual(
            params["query"],
            queries.group_query(MATCHER, "cpu", ID, "1h", "10m"),
        )
        prom._get.assert_called_once()

    def test_empty_result(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([])
        series = client.get_project_instance_metrics(
            MATCHER, "network", ID, "6h", "10m"
        )
        self.assertEqual(series, {"net_in": [], "net_out": []})
        self.assertNotIn("time", prom._get.call_args.args[1])

    def test_errors_propagate(self):
        # the views cache the failure and show the error state
        client, prom = _make_client()
        prom._get.side_effect = RuntimeError("Aetos is down")
        with self.assertRaises(RuntimeError):
            client.get_project_instance_metrics(
                MATCHER, "disk", ID, "1h", "10m"
            )

    def test_invalid_id_is_never_sent(self):
        client, prom = _make_client()
        with self.assertRaises(ValueError):
            client.get_project_instance_metrics(
                MATCHER, "disk", "../etc", "1h", "10m"
            )
        prom._get.assert_not_called()
