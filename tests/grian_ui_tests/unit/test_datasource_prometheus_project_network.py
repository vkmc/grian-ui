#
# SPDX-License-Identifier: Apache-2.0

import re

from grian_ui.datasource import prometheus_project_network as ppn
from grian_ui_tests.unit import base
from grian_ui_tests.unit.test_datasource_prometheus import _make_client
from grian_ui_tests.unit.test_datasource_prometheus import _make_metric


MATCHER = 'project="b8753cd7d01b4455b774b46991f36f83"'
RESOURCE = (
    "instance-c726b640-2a18-41e3-9fac-af2730c12410-"
    "c726b640-2a18-41e3-9fac-af2730c12410-tap01132844-f8"
)
LABELS = {
    "resource": RESOURCE,
    "resource_name": "grian-ih-disk:tap01132844-f8",
}
#: A selector: a metric name followed by its label matchers.
_SELECTOR = re.compile(r"([a-z_]+)\{([^}]*)\}")


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


def _queries():
    return {
        "interfaces": ppn.interfaces_query(MATCHER),
        "traffic 1h": ppn.traffic_trend_query(MATCHER, "1h"),
        "traffic 24h": ppn.traffic_trend_query(MATCHER, "24h"),
        "interface trend": ppn.interface_trend_query(MATCHER, "6h"),
    }


class SelectorTests(base.TestCase):
    def test_every_selector_names_its_metric_and_project(self):
        for name, query in _queries().items():
            selectors = _SELECTOR.findall(query)
            self.assertTrue(selectors, name)
            for metric, matchers in selectors:
                self.assertIn(metric, ppn.METRICS, name)
                self.assertEqual(matchers, MATCHER, name)
            # no selector without a metric name (Aetos rejects them)
            self.assertNotIn("__name__", query, name)
            self.assertEqual(query.count("{"), len(selectors), name)
            # every metric occurrence is a selector with the matcher
            for metric in ppn.METRICS:
                self.assertEqual(
                    len(re.findall(rf"\b{metric}\b(?!_)", query)),
                    len(re.findall(rf"\b{metric}\{{", query)),
                    (name, metric),
                )

    def test_no_instance_filter_on_interface_metrics(self):
        # resource_name is "<instance>:tap<port>": the instance metrics'
        # resource_name=~".+:instance-.+" filter would match nothing
        for name, query in _queries().items():
            self.assertNotIn("resource_name=~", query, name)


class QueryTests(base.TestCase):
    def test_interfaces_query(self):
        query = ppn.interfaces_query(MATCHER)
        self.assertIn(
            "max by (resource, resource_name) "
            f"(rate(ceilometer_network_incoming_packets{{{MATCHER}}}[15m]))",
            query,
        )
        self.assertIn(
            f"rate(ceilometer_network_incoming_packets{{{MATCHER}}}[15m] "
            "offset 1d)",
            query,
        )
        self.assertIn(
            "avg_over_time(rate(ceilometer_network_incoming_packets"
            f"{{{MATCHER}}}[15m])[6h:5m])",
            query,
        )
        for metric in ppn.LOSS_METRICS:
            self.assertIn(f"rate({metric}{{{MATCHER}}}[10m])", query)
        for field in ppn._FIELDS:
            self.assertIn(f'"field", "{field}"', query)

    def test_trend_windows(self):
        # rate() over the larger of the step and the rate window
        self.assertIn("[10m]", ppn.traffic_trend_query(MATCHER, "1h"))
        self.assertTrue(
            ppn.traffic_trend_query(MATCHER, "1h").endswith(")[1h:1m]")
        )
        self.assertIn("[15m]", ppn.traffic_trend_query(MATCHER, "24h"))
        self.assertTrue(
            ppn.interface_trend_query(MATCHER, "6h").endswith(")[6h:5m]")
        )


class ParseTests(base.TestCase):
    def test_keys_and_names(self):
        self.assertEqual(ppn.interface_key(LABELS), "01132844-f8")
        self.assertEqual(
            ppn.interface_key(
                {"resource": "x", "resource_name": "vm:vhu0123abcd-ef"}
            ),
            "0123abcd-ef",
        )
        self.assertEqual(ppn.interface_key({"resource": "eth0"}), "eth0")
        self.assertEqual(ppn.instance_name(LABELS), "grian-ih-disk")
        self.assertEqual(ppn.instance_name({"resource_name": "x"}), "")

    def test_usual_is_yesterday_else_six_hours(self):
        other = dict(LABELS, resource="instance-x-x-tapb4493520-e4")

        def row(labels, field, value):
            return dict(labels, field=field), value

        result = ppn.parse_interfaces(
            [
                row(LABELS, "in_pps", "0.018"),
                row(LABELS, "usual_1d", "0.036"),
                row(LABELS, "usual_6h", "0.042"),
                row(LABELS, "lost_pps", "0"),
                row(LABELS, "in_bps", "5.1"),
                row(LABELS, "out_bps", "NaN"),
                row(other, "in_pps", "1"),
                row(other, "usual_6h", "2"),
                row(other, "unknown", "3"),
            ]
        )
        first, second = result["interfaces"]
        self.assertEqual(
            first,
            {
                "key": "01132844-f8",
                "name": "grian-ih-disk",
                "in_pps": 0.018,
                "usual_pps": 0.036,
                "usual_basis": "1d",
                "in_bps": 5.1,
                "out_bps": None,
                "lost_pps": 0.0,
            },
        )
        self.assertEqual(second["usual_pps"], 2.0)
        self.assertEqual(second["usual_basis"], "6h")
        self.assertIsNone(second["lost_pps"])

    def test_no_usual(self):
        result = ppn.parse_interfaces([(dict(LABELS, field="in_pps"), "1")])
        self.assertIsNone(result["interfaces"][0]["usual_pps"])
        self.assertIsNone(result["interfaces"][0]["usual_basis"])


class ClientTests(base.TestCase):
    def test_get_interfaces(self):
        client, prom = _make_client()
        prom.query.return_value = [
            _make_metric("0.5", dict(LABELS, field="in_pps")),
            _make_metric("1", dict(LABELS, field="usual_1d")),
        ]
        result = client.get_project_network_interfaces(MATCHER)
        prom.query.assert_called_once_with(query=ppn.interfaces_query(MATCHER))
        self.assertEqual(result["interfaces"][0]["in_pps"], 0.5)

    def test_get_interfaces_empty(self):
        client, prom = _make_client()
        prom.query.return_value = None
        self.assertEqual(
            client.get_project_network_interfaces(MATCHER), {"interfaces": []}
        )

    def test_get_traffic_trend(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix(
            [
                ({"direction": "in"}, [[60, "10.04"], [120, "NaN"]]),
                ({"direction": "out"}, [[60, "3"]]),
                ({"direction": "other"}, [[60, "3"]]),
            ]
        )
        trend = client.get_project_network_traffic_trend(MATCHER, "1h", at=120)
        self.assertEqual(trend, {"in": [[60.0, 10.0]], "out": [[60.0, 3.0]]})
        params = prom._get.call_args[0][1]
        self.assertEqual(
            params["query"], ppn.traffic_trend_query(MATCHER, "1h")
        )
        self.assertEqual(params["time"], "120.000")

    def test_get_interface_trend(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix(
            [(LABELS, [[60, "0.0361"]]), ({"resource": ""}, [[60, "1"]])]
        )
        trend = client.get_project_network_interface_trend(MATCHER, "6h")
        self.assertEqual(trend, {"01132844-f8": [[60.0, 0.036]]})
