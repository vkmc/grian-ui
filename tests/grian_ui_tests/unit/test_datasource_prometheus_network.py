#
# SPDX-License-Identifier: Apache-2.0

from django.test import override_settings

from grian_ui.datasource import prometheus_network as pn
from grian_ui_tests.unit import base
from grian_ui_tests.unit.test_datasource_prometheus import _make_client
from grian_ui_tests.unit.test_datasource_prometheus import _make_metric


RESOURCE = (
    "instance-24c2ecb1-e3f4-48f7-9bc7-aa4a1f8ef51d-"
    "24c2ecb1-e3f4-48f7-9bc7-aa4a1f8ef51d-tap87745239-00"
)


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


class InterfaceKeyTests(base.TestCase):
    def test_tap_name(self):
        self.assertEqual(
            pn.interface_key({"resource": RESOURCE}), "87745239-00"
        )

    def test_vhost_user(self):
        self.assertEqual(
            pn.interface_key({"resource": "vm-1-vhu0123abcd-ef"}),
            "0123abcd-ef",
        )

    def test_other_names_keep_the_resource(self):
        self.assertEqual(pn.interface_key({"resource": "eth0"}), "eth0")
        # resource_name is dropped by the queries' max by (resource, ...)
        self.assertEqual(
            pn.interface_key(
                {"resource": "eth0", "resource_name": "vm:tap0123abcd-ef"}
            ),
            "eth0",
        )
        self.assertEqual(pn.interface_key({}), "")


class QueryTests(base.TestCase):
    def test_interfaces_query(self):
        query = pn.interfaces_query("10m")
        self.assertIn(
            "max by (resource, project) "
            "(rate(ceilometer_network_incoming_packets[10m]))",
            query,
        )
        self.assertIn(
            "rate(ceilometer_network_incoming_packets[10m] offset 7d)", query
        )
        for metric in pn.LOSS_METRICS:
            self.assertIn(f"rate({metric}[10m])", query)
        # Aetos rejects __name__ regular expressions
        self.assertNotIn("__name__", query)
        for field in ("in_pps", "usual_pps", "lost_pps"):
            self.assertIn(f'"field", "{field}"', query)

    def test_traffic_query(self):
        query = pn.traffic_query("5m")
        self.assertIn(
            "sum(max by (resource, project) "
            "(rate(ceilometer_network_incoming_bytes[5m])))",
            query,
        )
        self.assertIn("ceilometer_network_outgoing_bytes[5m]", query)

    def test_trend_query_uses_at_least_the_step(self):
        self.assertTrue(pn.traffic_trend_query("6h").endswith("[6h:5m]"))
        self.assertIn("[10m]", pn.traffic_trend_query("6h"))
        # 24h steps every 15 minutes: rate over 15m, not 10m
        query = pn.traffic_trend_query("24h")
        self.assertIn("[15m]", query)
        self.assertNotIn("[10m]", query)
        self.assertTrue(query.endswith("[24h:15m]"))

    @override_settings(GRIAN_PLUGIN={"network_health_rate_window": "1h"})
    def test_configured_window(self):
        self.assertIn("[1h]", pn.interfaces_query("1h"))
        self.assertIn("[1h]", pn.traffic_trend_query("24h"))


class GetInterfacesTests(base.TestCase):
    def test_parses_fields(self):
        client, prom = _make_client()
        labels = {"resource": RESOURCE, "project": "p1"}

        def query(query):
            if "outgoing_bytes" in query and "packets" not in query:
                return [
                    _make_metric("100.5", {"field": "in_bps"}),
                    _make_metric("NaN", {"field": "out_bps"}),
                ]
            return [
                _make_metric("0.5", dict(labels, field="in_pps")),
                _make_metric("2", dict(labels, field="usual_pps")),
                _make_metric("0", dict(labels, field="lost_pps")),
                _make_metric("9", dict(labels, field="other")),
                _make_metric("1", {"resource": "eth0", "field": "in_pps"}),
                _make_metric("1", {"field": "in_pps"}),
            ]

        prom.query.side_effect = query
        result = client.get_network_interfaces()
        self.assertEqual(prom.query.call_count, 2)
        self.assertEqual(
            result["interfaces"],
            [
                {
                    "key": "87745239-00",
                    "project": "p1",
                    "in_pps": 0.5,
                    "usual_pps": 2.0,
                    "lost_pps": 0.0,
                },
                {
                    "key": "eth0",
                    "project": None,
                    "in_pps": 1.0,
                    "usual_pps": None,
                    "lost_pps": None,
                },
            ],
        )
        self.assertEqual(result["traffic"], {"in_bps": 100.5, "out_bps": None})

    def test_empty(self):
        client, prom = _make_client()
        prom.query.return_value = []
        self.assertEqual(
            client.get_network_interfaces(),
            {"interfaces": [], "traffic": {"in_bps": None, "out_bps": None}},
        )


class GetTrafficTrendTests(base.TestCase):
    def test_parses_directions(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix(
            [
                ({"direction": "in"}, [[60, "10.04"], [120, "NaN"]]),
                ({"direction": "out"}, [[60, "3"]]),
                ({"direction": "other"}, [[60, "1"]]),
            ]
        )
        trend = client.get_network_traffic_trend("6h", at=150.0)
        self.assertEqual(trend, {"in": [[60.0, 10.0]], "out": [[60.0, 3.0]]})
        params = prom._get.call_args[0][1]
        self.assertEqual(params["time"], "150.000")
        self.assertTrue(params["query"].endswith("[6h:5m]"))
