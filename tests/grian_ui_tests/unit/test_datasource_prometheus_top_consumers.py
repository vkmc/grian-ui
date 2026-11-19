#
# SPDX-License-Identifier: Apache-2.0

import re

from grian_ui import top_consumers as tc
from grian_ui.datasource import prometheus_top_consumers as queries
from grian_ui_tests.unit import base
from grian_ui_tests.unit.test_datasource_prometheus import _make_client
from grian_ui_tests.unit.test_datasource_prometheus import _make_metric


ID1 = "f6fb292b-1740-404b-81f7-c32413db8f65"
ID2 = "d414fbd9-66a6-485c-bec7-8191c2fa4221"
P1 = "7bb2692e0d994decacab9a372f166676"
P2 = "b8753cd7d01b4455b774b46991f36f83"


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
        match = re.fullmatch(queries._INSTANCE_FROM_RESOURCE, resource)
        return match.group(2) if match else None

    def test_instance(self):
        self.assertEqual(self.extract(ID1), ID1)

    def test_disk(self):
        self.assertEqual(self.extract(f"{ID1}-vda"), ID1)

    def test_vnic_with_uuid_instance_name(self):
        # DevStack: libvirt name "instance-<uuid>", then the uuid again
        resource = f"instance-{ID2}-{ID1}-tap87745239-00"
        self.assertEqual(self.extract(resource), ID1)

    def test_vnic_with_default_instance_name(self):
        self.assertEqual(
            self.extract(f"instance-0000000a-{ID1}-tap87745239-00"), ID1
        )


class ValidationTests(base.TestCase):
    def test_instance_ids(self):
        self.assertTrue(queries.is_instance_id(ID1))
        self.assertFalse(queries.is_instance_id(ID1.upper()))
        self.assertFalse(queries.is_instance_id(f"{ID1}|.*"))
        self.assertFalse(queries.is_instance_id(f"{ID1}\n"))
        self.assertFalse(queries.is_instance_id(None))

    def test_project_ids(self):
        self.assertTrue(queries.is_project_id(P1))
        self.assertTrue(queries.is_project_id("ldap_project-1"))
        self.assertFalse(queries.is_project_id('a"} or vector(1) #'))
        self.assertFalse(queries.is_project_id("a|b"))
        self.assertFalse(queries.is_project_id(".*"))
        self.assertFalse(queries.is_project_id(f"{P1}\n"))
        self.assertFalse(queries.is_project_id(""))
        self.assertFalse(queries.is_project_id("x" * 65))

    def test_matchers_drop_invalid_values(self):
        self.assertEqual(
            queries._project_matcher([P1, "bad id"]), f'project=~"{P1}"'
        )
        self.assertIsNone(queries._project_matcher(["a|b"]))
        self.assertIsNone(queries._instance_matcher(["nope"]))
        self.assertEqual(
            queries._instance_matcher([ID1, ID2]),
            f'resource=~"(.*-)?({ID1}|{ID2})(-.+)?"',
        )

    def test_instance_name(self):
        self.assertEqual(
            queries.instance_name(f"grian-vm:instance-{ID1}"), "grian-vm"
        )
        self.assertEqual(queries.instance_name(f"a:b:instance-{ID1}"), "a:b")
        self.assertIsNone(queries.instance_name("plain"))
        self.assertIsNone(queries.instance_name(f":instance-{ID1}"))
        self.assertIsNone(queries.instance_name(None))


class ResourceExprTests(base.TestCase):
    def test_cpu_uses_polled_samples_and_cores(self):
        expr = queries.resource_expr(tc.RESOURCE_CPU, "project", "10m")
        self.assertIn(
            'rate(ceilometer_cpu{resource_name=~".+:instance-.+"}[10m])', expr
        )
        self.assertTrue(expr.startswith("sum by (project) ("))
        self.assertTrue(expr.endswith(" / 1e+09"))
        # one value per instance first (duplicated series)
        self.assertIn("max by (project, resource) (rate(", expr)

    def test_counters_are_kept_apart_before_the_sum(self):
        expr = queries.resource_expr(tc.RESOURCE_DISK, "project", "10m")
        self.assertIn('"meter", "read"', expr)
        self.assertIn('"meter", "write"', expr)
        self.assertIn(" or ", expr)

    def test_network_has_no_polled_filter(self):
        expr = queries.resource_expr(tc.RESOURCE_NETWORK, "", "5m")
        self.assertIn("rate(ceilometer_network_incoming_bytes[5m])", expr)
        self.assertIn("rate(ceilometer_network_outgoing_bytes[5m])", expr)
        self.assertTrue(expr.startswith("sum ("))

    def test_memory_is_a_gauge(self):
        expr = queries.resource_expr(
            tc.RESOURCE_MEMORY, "project", "10m", 'project="x"'
        )
        self.assertIn(
            "last_over_time(ceilometer_memory_usage{"
            'resource_name=~".+:instance-.+",project="x"}[10m])',
            expr,
        )
        self.assertNotIn("1e+09", expr)

    def test_instance_grouping_extracts_the_uuid(self):
        for resource in tc.RESOURCES:
            expr = queries.resource_expr(
                resource, f"project, {queries.INSTANCE_LABEL}", "10m"
            )
            self.assertIn(
                f'"{queries.INSTANCE_LABEL}", "$2", "resource"', expr
            )


class GetProjectsTests(base.TestCase):
    def test_snapshot(self):
        client, prom = _make_client()
        prom.query.return_value = [
            _make_metric("2", {"project": P1, "field": "instances"}),
            _make_metric("3.5", {"project": P1, "field": "cpu"}),
            _make_metric("5", {"project": P1, "field": "vcpus"}),
            _make_metric("NaN", {"project": P1, "field": "memory"}),
            _make_metric("9e8", {"project": P2, "field": "disk_write"}),
            _make_metric("1", {"project": P2, "field": "bogus"}),
            _make_metric("1", {"field": "cpu"}),
        ]
        snapshot = client.get_top_consumers_projects()
        rows = {p["id"]: p for p in snapshot["projects"]}
        self.assertEqual(set(rows), {P1, P2})
        self.assertEqual(rows[P1]["instances"], 2)
        self.assertIsInstance(rows[P1]["instances"], int)
        self.assertEqual(rows[P1]["cpu"], 3.5)
        self.assertIsNone(rows[P1]["memory"])
        self.assertIsNone(rows[P2]["instances"])
        self.assertEqual(rows[P2]["disk_write"], 9e8)
        self.assertNotIn("bogus", rows[P2])

    def test_one_query_with_every_field(self):
        client, prom = _make_client()
        prom.query.return_value = []
        self.assertEqual(client.get_top_consumers_projects(), {"projects": []})
        self.assertEqual(prom.query.call_count, 1)
        query = prom.query.call_args.kwargs["query"]
        for field in tc.SNAPSHOT_FIELDS:
            self.assertIn(f'"field", "{field}"', query)
        self.assertIn("ceilometer_disk_device_read_bytes", query)
        self.assertIn("ceilometer_network_outgoing_bytes", query)
        self.assertIn("[10m]", query)


class GetInstancesTests(base.TestCase):
    def test_top_instances_with_names(self):
        client, prom = _make_client()
        label = queries.INSTANCE_LABEL

        def query(query):
            if query.startswith("topk("):
                return [
                    _make_metric("3.4", {label: ID1, "project": P1}),
                    _make_metric("1.0", {label: ID2, "project": P2}),
                    _make_metric("9", {label: "not-a-uuid", "project": P2}),
                ]
            return [
                _make_metric(
                    "4",
                    {
                        "resource": ID1,
                        "resource_name": f"vm-1:instance-{ID1}",
                        "field": "vcpus",
                    },
                ),
                _make_metric(
                    "256",
                    {
                        "resource": ID1,
                        "resource_name": f"vm-1:instance-{ID1}",
                        "field": "memory_alloc",
                    },
                ),
                _make_metric("1", {"resource": "other", "field": "vcpus"}),
            ]

        prom.query.side_effect = query
        rows = client.get_top_consumers_instances(tc.RESOURCE_CPU)
        self.assertEqual(prom.query.call_count, 2)
        self.assertEqual(
            rows,
            [
                {
                    "id": ID1,
                    "name": "vm-1",
                    "project_id": P1,
                    "value": 3.4,
                    "vcpus": 4.0,
                    "memory_alloc": 256.0,
                },
                {
                    "id": ID2,
                    "name": None,
                    "project_id": P2,
                    "value": 1.0,
                    "vcpus": None,
                    "memory_alloc": None,
                },
            ],
        )
        queries_run = [c.kwargs["query"] for c in prom.query.call_args_list]
        self.assertTrue(
            queries_run[0].startswith("topk(10, sum by (project, ")
        )
        # names only for the listed instances
        self.assertIn(f"({ID1}|{ID2})", queries_run[1])
        self.assertNotIn("not-a-uuid", queries_run[1])

    def test_topk_only_ranks_series_of_an_instance(self):
        # a series without an instance UUID in its resource label would
        # take a topk() slot and then be dropped (fewer rows than limit)
        client, prom = _make_client()
        prom.query.return_value = []
        matcher = f'resource=~"{queries._INSTANCE_FROM_RESOURCE}"'
        for project_id in (None, P1):
            client.get_top_consumers_instances(
                tc.RESOURCE_DISK, project_id=project_id
            )
            query = prom.query.call_args.kwargs["query"]
            # read and write meters
            self.assertEqual(query.count(matcher), 2)
            if project_id:
                self.assertIn(f'project=~"{P1}",{matcher}', query)

    def test_project_filter(self):
        client, prom = _make_client()
        prom.query.return_value = []
        self.assertEqual(
            client.get_top_consumers_instances(
                tc.RESOURCE_NETWORK, project_id=P1, limit=5
            ),
            [],
        )
        query = prom.query.call_args.kwargs["query"]
        self.assertTrue(query.startswith("topk(5, "))
        self.assertIn(f'project=~"{P1}"', query)
        # nothing found: no second query
        self.assertEqual(prom.query.call_count, 1)

    def test_invalid_project_never_queries(self):
        client, prom = _make_client()
        rows = client.get_top_consumers_instances(
            tc.RESOURCE_CPU, project_id='x"} or vector(1)'
        )
        self.assertEqual(rows, [])
        prom.query.assert_not_called()


class GetTrendTests(base.TestCase):
    def test_trend_with_total(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix(
            [
                ({"project": P1}, [[1700000000, "1.5"], [1700000900, "NaN"]]),
                ({"project": tc.TOTAL_KEY}, [[1700000000, "4"]]),
            ]
        )
        trend = client.get_top_consumers_trend(
            tc.RESOURCE_CPU, tc.BY_PROJECT, [P1, "bad id"], at=1700000950
        )
        self.assertEqual(
            trend,
            {
                "series": {P1: [[1700000000.0, 1.5]]},
                "total": [[1700000000.0, 4.0]],
            },
        )
        path, params = prom._get.call_args.args
        self.assertEqual(path, "query")
        self.assertEqual(params["time"], "1700000950.000")
        query = params["query"]
        self.assertTrue(query.endswith("[24h:15m]"))
        self.assertIn(f'project=~"{P1}"', query)
        self.assertNotIn("bad id", query)
        self.assertIn(f'"project", "{tc.TOTAL_KEY}"', query)
        # rates over the chart step
        self.assertIn("[15m]", query)

    def test_ranges(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([])
        for range_key, suffix, rate in (
            ("1h", "[1h:1m]", "[10m]"),
            ("6h", "[6h:5m]", "[10m]"),
            ("7d", "[7d:1h]", "[1h]"),
        ):
            client.get_top_consumers_trend(
                tc.RESOURCE_CPU, tc.BY_PROJECT, [P1], range_key=range_key
            )
            query = prom._get.call_args.args[1]["query"]
            self.assertTrue(query.endswith(suffix), query)
            self.assertIn(f"{rate})", query)

    def test_by_instance_of_a_project(self):
        client, prom = _make_client()
        label = queries.INSTANCE_LABEL
        prom._get.return_value = _matrix(
            [
                ({label: ID1, "project": P1}, [[1700000000, "10"]]),
                ({label: tc.TOTAL_KEY}, [[1700000000, "12"]]),
            ]
        )
        trend = client.get_top_consumers_trend(
            tc.RESOURCE_DISK, tc.BY_INSTANCE, [ID1], project_id=P1
        )
        self.assertEqual(trend["series"], {ID1: [[1700000000.0, 10.0]]})
        self.assertEqual(trend["total"], [[1700000000.0, 12.0]])
        query = prom._get.call_args.args[1]["query"]
        self.assertIn(
            f'project=~"{P1}",resource=~"(.*-)?({ID1})(-.+)?"', query
        )
        self.assertIn(f'"{label}", "{tc.TOTAL_KEY}"', query)

    def test_without_valid_keys_only_the_total(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([])
        client.get_top_consumers_trend(tc.RESOURCE_CPU, tc.BY_PROJECT, [])
        query = prom._get.call_args.args[1]["query"]
        self.assertTrue(query.startswith("(label_replace(sum ("))

    def test_invalid_project_never_queries(self):
        client, prom = _make_client()
        trend = client.get_top_consumers_trend(
            tc.RESOURCE_CPU, tc.BY_INSTANCE, [ID1], project_id="a|b"
        )
        self.assertEqual(trend, {"series": {}, "total": []})
        prom._get.assert_not_called()
