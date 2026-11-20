#
# SPDX-License-Identifier: Apache-2.0

import datetime
import re

from unittest import mock

from grian_ui import project_usage as pu
from grian_ui.datasource import prometheus_project_usage as queries
from grian_ui_tests.unit import base
from grian_ui_tests.unit.test_datasource_prometheus import _make_client


UTC = datetime.timezone.utc
PROJECT = "b8753cd7d01b4455b774b46991f36f83"
MATCHER = f'project="{PROJECT}"'
ID1 = "d414fbd9-66a6-485c-bec7-8191c2fa4221"
ID2 = "c726b640-2a18-41e3-9fac-af2730c12410"
METRICS = (
    "ceilometer_vcpus",
    "ceilometer_memory",
    "ceilometer_memory_usage",
    "ceilometer_cpu",
    "ceilometer_network_incoming_bytes",
    "ceilometer_network_outgoing_bytes",
    "ceilometer_volume_size",
    "ceilometer_volume_snapshot_size",
)
# every selector: a metric name followed by {...}
SELECTOR = re.compile(r"([a-z_]+)\{([^}]*)\}")


def _selectors(query):
    """(metric, matchers) of every selector, label_replace args excluded."""
    return [
        (m.group(1), m.group(2))
        for m in SELECTOR.finditer(query)
        if not m.group(1).endswith("by")
    ]


def _vector(series):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [1795189014.3, value]}
                for labels, value in series
            ],
        },
    }


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


class InstanceLabelTests(base.TestCase):
    """The instance UUID regex, checked like RE2 (anchored, greedy)."""

    def extract(self, resource):
        match = re.fullmatch(queries.INSTANCE_FROM_RESOURCE, resource)
        return match.group(2) if match else None

    def test_instance(self):
        self.assertEqual(self.extract(ID1), ID1)

    def test_vnic_devstack(self):
        # real demo label: libvirt name "instance-<uuid>", the uuid again
        self.assertEqual(
            self.extract(f"instance-{ID1}-{ID1}-tapb4493520-e4"), ID1
        )

    def test_vnic_default_libvirt_name(self):
        self.assertEqual(
            self.extract(f"instance-0000000a-{ID2}-tap87745239-00"), ID2
        )

    def test_not_an_instance(self):
        self.assertIsNone(self.extract("9b472f4d"))


class QueryTests(base.TestCase):
    def check_selectors(self, query, expected_metrics):
        selectors = _selectors(query)
        self.assertTrue(selectors)
        for metric, matchers in selectors:
            with self.subTest(metric=metric):
                # a metric name (Aetos refuses {__name__=~...} for project
                # users) and the project (Aetos does not filter admins)
                self.assertIn(metric, METRICS)
                self.assertIn(MATCHER, matchers.split(","))
                self.assertNotIn("__name__", matchers)
                if metric in METRICS[:4]:
                    # only the polled instance samples (no double count)
                    self.assertIn(queries.POLLED, matchers)
                elif "network" in metric:
                    self.assertIn(queries.POLLED_VNIC, matchers)
        self.assertEqual({m for m, _ in selectors}, set(expected_metrics))

    def test_totals_query_selectors(self):
        self.check_selectors(queries.totals_query(MATCHER, 86400), METRICS)

    def test_hourly_query_selectors(self):
        self.check_selectors(queries.hourly_query(MATCHER, 86400), METRICS[:6])

    def test_totals_query_shape(self):
        query = queries.totals_query(MATCHER, 1296000)
        # held: subquery grid of 5 minutes, one second short of the range
        self.assertIn("[1295999s:5m]", query)
        self.assertIn(
            "count_over_time((max by (resource) (ceilometer_vcpus{", query
        )
        self.assertIn("increase(ceilometer_cpu{", query)
        self.assertIn("}[1296000s]", query)
        # every series is reduced per resource before summing
        self.assertEqual(
            query.count("max by (resource"), query.count("ceilometer_")
        )
        for field in (
            "running_steps",
            "vcpu_steps",
            "memory_steps",
            "memory_usage_steps",
            "cpu_ns",
            "net_in",
            "net_out",
            "last_vcpus",
            "last_memory",
            "name_seen",
            "volume_steps",
            "snapshot_steps",
        ):
            self.assertIn(f'"field", "{field}"', query)
        # names: the time of the last sample of each name
        self.assertIn('sum by (instance_id, resource_name)', query)
        self.assertIn(
            "max by (resource, resource_name) (max_over_time(timestamp("
            "ceilometer_vcpus{",
            query,
        )
        self.assertIn(")[1295999s:5m]))", query)
        # sizes: the last value of each name series
        self.assertIn(
            "max by (resource, resource_name) (last_over_time("
            "ceilometer_memory{",
            query,
        )

    def test_hourly_query_shape(self):
        query = queries.hourly_query(MATCHER, 7200)
        self.assertTrue(query.startswith("("))
        self.assertTrue(query.endswith(")[7199s:1h]"))
        self.assertIn("[3599s:5m]", query)
        self.assertIn("[3600s]", query)

    def test_matcher_comes_from_the_request(self):
        client = mock.MagicMock()
        client.client._get.return_value = _vector([])
        client.query_matrix.return_value = []
        request = mock.MagicMock()
        request.user.project_id = PROJECT
        start = datetime.datetime(2026, 11, 6, tzinfo=UTC)
        end = datetime.datetime(2026, 11, 20, 15, 40, tzinfo=UTC)
        queries.get_usage(client, request, start, end)
        query = client.client._get.call_args[0][1]["query"]
        self.assertIn(MATCHER, query)
        self.assertIn("[1266000s]", query)
        matrix_query = client.query_matrix.call_args[0][0]
        self.assertIn(MATCHER, matrix_query)

    def test_unsafe_project_is_refused(self):
        client = mock.MagicMock()
        request = mock.MagicMock()
        request.user.project_id = 'x"} or vector(1) #'
        start = datetime.datetime(2026, 11, 6, tzinfo=UTC)
        with self.assertRaises(ValueError):
            queries.get_usage(client, request, start, start)
        client.client._get.assert_not_called()


def _size(field, name, value):
    return (
        {
            "field": field,
            "instance_id": ID1,
            "resource_name": f"{name}:instance-{ID1}",
        },
        value,
    )


class ParseTotalsTests(base.TestCase):
    def test_parse(self):
        steps_per_hour = 3600 / pu.STEP_SECONDS
        results = [
            ({"field": "running_steps", "instance_id": ID1}, "645"),
            ({"field": "vcpu_steps", "instance_id": ID1}, "2580"),
            ({"field": "memory_steps", "instance_id": ID1}, "165120"),
            ({"field": "memory_usage_steps", "instance_id": ID1}, "28979"),
            ({"field": "cpu_ns", "instance_id": ID1}, str(189.92e9 * 3600)),
            ({"field": "net_in", "instance_id": ID1}, "726583"),
            ({"field": "net_out", "instance_id": ID1}, "220186"),
            (
                {
                    "field": "name_seen",
                    "instance_id": ID1,
                    "resource_name": f"grian-ih-cpu-2:instance-{ID1}",
                },
                "1795193988.566",
            ),
            ({"field": "last_vcpus", "instance_id": ID1}, "4"),
            ({"field": "last_memory", "instance_id": ID1}, "256"),
            ({"field": "volume_steps"}, "24"),
            ({"field": "running_steps"}, "1"),  # no instance: ignored
            ({"field": "net_in", "instance_id": ID2}, "NaN"),  # dropped
        ]
        parsed = queries.parse_totals(results)
        self.assertEqual(list(parsed["instances"]), [ID1])
        usage = parsed["instances"][ID1]
        self.assertEqual(usage["name"], "grian-ih-cpu-2")
        self.assertEqual(usage["vcpus"], 4.0)
        self.assertEqual(usage["memory_mib"], 256.0)
        self.assertAlmostEqual(usage["hours"], 645 / steps_per_hour)
        self.assertAlmostEqual(usage["vcpu_hours"], 215.0)
        self.assertAlmostEqual(usage["cpu_hours"], 189.92)
        self.assertAlmostEqual(usage["ram_gb_hours_held"], 13.4375)
        self.assertAlmostEqual(
            usage["ram_gb_hours_used"], 28979 / steps_per_hour / 1024
        )
        self.assertEqual(usage["net_in"], 726583.0)
        self.assertEqual(parsed["volume_gb_hours"], 2.0)
        self.assertIsNone(parsed["snapshot_gb_hours"])

    def test_renamed_instance(self):
        # one series per name: the latest name, sizes not summed, whatever
        # the order of the series
        old = (
            {
                "field": "name_seen",
                "instance_id": ID1,
                "resource_name": f"old-name:instance-{ID1}",
            },
            "1795100000",
        )
        new = (
            {
                "field": "name_seen",
                "instance_id": ID1,
                "resource_name": f"new-name:instance-{ID1}",
            },
            "1795190000",
        )
        sizes = [
            _size("last_vcpus", "old-name", "2"),
            _size("last_vcpus", "new-name", "2"),
            _size("last_memory", "old-name", "512"),
            _size("last_memory", "new-name", "512"),
        ]
        for results in ([old, new] + sizes, sizes + [new, old]):
            usage = queries.parse_totals(results)["instances"][ID1]
            self.assertEqual(usage["name"], "new-name")
            self.assertEqual(usage["vcpus"], 2.0)
            self.assertEqual(usage["memory_mib"], 512.0)

    def test_renamed_and_resized_instance(self):
        # the size of the latest series, not the largest
        def seen(name, timestamp):
            return (
                {
                    "field": "name_seen",
                    "instance_id": ID1,
                    "resource_name": f"{name}:instance-{ID1}",
                },
                timestamp,
            )

        series = [
            seen("big", "1795100000"),
            seen("small", "1795190000"),
            _size("last_vcpus", "big", "8"),
            _size("last_vcpus", "small", "2"),
            _size("last_memory", "big", "16384"),
            _size("last_memory", "small", "4096"),
        ]
        for results in (series, series[::-1]):
            usage = queries.parse_totals(results)["instances"][ID1]
            self.assertEqual(usage["name"], "small")
            self.assertEqual(usage["vcpus"], 2.0)
            self.assertEqual(usage["memory_mib"], 4096.0)

    def test_name_tie_is_deterministic(self):
        def seen(name):
            return (
                {
                    "field": "name_seen",
                    "instance_id": ID1,
                    "resource_name": f"{name}:instance-{ID1}",
                },
                "1795190000",
            )

        for results in ([seen("a"), seen("b")], [seen("b"), seen("a")]):
            usage = queries.parse_totals(results)["instances"][ID1]
            self.assertEqual(usage["name"], "b")

    def test_empty(self):
        self.assertEqual(
            queries.parse_totals([]),
            {
                "instances": {},
                "volume_gb_hours": None,
                "snapshot_gb_hours": None,
            },
        )


class ParseHourlyTests(base.TestCase):
    def test_parse(self):
        start = 1795132800  # 11/20 00:00 UTC
        series = [
            ({"field": "vcpu_steps"}, [[start, 60.0], [start + 3600, 60.0]]),
            ({"field": "cpu_ns"}, [[start + 3600, 3.6e12]]),
            ({"field": "memory_steps"}, [[start + 3600, 12 * 1024.0]]),
            ({"field": "net_in"}, [[start + 3600, 10.0]]),
            ({"field": "net_out"}, [[start + 3600, 5.0], [start + 7200, 1.0]]),
            ({}, [[start, 1.0]]),
        ]
        hourly = queries.parse_hourly(series, start)
        # points end their hour: keyed by the start, earlier hours dropped
        self.assertEqual(hourly["vcpu_hours"], [[start, 5.0]])
        self.assertEqual(hourly["cpu_hours"], [[start, 1.0]])
        self.assertEqual(hourly["ram_gb_hours_held"], [[start, 1.0]])
        self.assertEqual(hourly["ram_gb_hours_used"], [])
        self.assertEqual(
            hourly["net_bytes"], [[start, 15.0], [start + 3600, 1.0]]
        )


class GetUsageTests(base.TestCase):
    def test_get_usage_through_the_client(self):
        client, _prom = _make_client()
        start = datetime.datetime(2026, 11, 20, tzinfo=UTC)
        end = datetime.datetime(2026, 11, 20, 1, 30, tzinfo=UTC)
        client.client._get.side_effect = [
            _vector([({"field": "vcpu_steps", "instance_id": ID1}, "12")]),
            _matrix(
                [
                    (
                        {"field": "vcpu_steps"},
                        [[start.timestamp() + 3600, "12"]],
                    )
                ]
            ),
        ]
        request = mock.MagicMock()
        request.user.project_id = PROJECT
        report = client.get_project_usage(request, start, end)
        self.assertEqual(report["instances"][ID1]["vcpu_hours"], 1.0)
        self.assertEqual(
            report["hourly"]["vcpu_hours"], [[start.timestamp(), 1.0]]
        )
        self.assertEqual(report["start"], start.timestamp())
        self.assertEqual(report["end"], end.timestamp())
        totals_call, hourly_call = client.client._get.call_args_list
        # the totals at the end of the period, the chart to the end of the
        # current hour
        self.assertEqual(totals_call[0][1]["time"], f"{end.timestamp():.3f}")
        self.assertEqual(
            hourly_call[0][1]["time"], f"{end.timestamp() + 1800:.3f}"
        )
        self.assertIn("[7199s:1h]", hourly_call[0][1]["query"])

    def test_unexpected_result_type(self):
        client, _prom = _make_client()
        client.client._get.return_value = _matrix([])
        with self.assertRaises(ValueError):
            queries.query_vector(client, "x", 0)
