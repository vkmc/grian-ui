#
# SPDX-License-Identifier: Apache-2.0

import re

from grian_ui import project_compute as pc
from grian_ui.datasource import prometheus_project_compute as queries
from grian_ui_tests.unit import base
from grian_ui_tests.unit.test_datasource_prometheus import _make_client
from grian_ui_tests.unit.test_datasource_prometheus import _make_metric


ID1 = "d414fbd9-66a6-485c-bec7-8191c2fa4221"
ID2 = "c726b640-2a18-41e3-9fac-af2730c12410"
PROJECT = "b8753cd7d01b4455b774b46991f36f83"
MATCHER = f'project="{PROJECT}"'

#: ``metric{matchers}``; a ``{`` not preceded by a metric name is a
#: selector without a name, which Aetos rejects for project users.
_SELECTOR = re.compile(r"([A-Za-z_:][A-Za-z0-9_:]*)\{([^}]*)\}")


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


class _QueryTestCase(base.TestCase):
    def assertScoped(self, query):
        """Every selector names a Ceilometer metric, the project and the
        polled samples."""
        selectors = _SELECTOR.findall(query)
        self.assertTrue(selectors, query)
        for name, matchers in selectors:
            self.assertTrue(name.startswith("ceilometer_"), name)
            self.assertIn(MATCHER, matchers.split(","), query)
            self.assertIn(queries.POLLED, matchers, query)
        # every "{" opens a named selector
        self.assertEqual(query.count("{"), len(selectors), query)
        self.assertNotIn("__name__", query)


class ValidationTests(_QueryTestCase):
    def test_instance_ids(self):
        self.assertTrue(queries.is_instance_id(ID1))
        self.assertFalse(queries.is_instance_id(ID1.upper()))
        self.assertFalse(queries.is_instance_id(f"{ID1}|.*"))
        self.assertFalse(queries.is_instance_id(f"{ID1}\n"))
        self.assertFalse(queries.is_instance_id(None))

    def test_selector_refuses_anything_but_a_project_matcher(self):
        self.assertEqual(
            queries.selector("ceilometer_cpu", MATCHER),
            f'ceilometer_cpu{{{queries.POLLED},{MATCHER}}}',
        )
        for bad in (
            "",
            None,
            'project=~".*"',
            'project="a",project="b"',
            'project="x"} or vector(1) #',
            'project="a|b"',
            'project="x"\n',
        ):
            with self.assertRaises(ValueError, msg=bad):
                queries.selector("ceilometer_cpu", bad)

    def test_instance_matcher_drops_invalid_ids(self):
        self.assertEqual(
            queries._instance_matcher([ID2, "nope", ID1, ID1]),
            f'resource=~"{ID2}|{ID1}"',
        )
        self.assertIsNone(queries._instance_matcher(['.*"} or vector(1)']))
        self.assertIsNone(queries._instance_matcher([]))

    def test_instance_name(self):
        self.assertEqual(
            queries.instance_name(f"grian-vm:instance-{ID1}"), "grian-vm"
        )
        self.assertEqual(queries.instance_name(f"a:b:instance-{ID1}"), "a:b")
        self.assertIsNone(queries.instance_name("plain"))
        self.assertIsNone(queries.instance_name(f":instance-{ID1}"))
        self.assertIsNone(queries.instance_name(None))


class EveryQueryIsScopedTests(_QueryTestCase):
    """Each query the panel sends carries the project matcher."""

    def sent(self):
        client, prom = _make_client()
        prom.query.return_value = []
        prom._get.return_value = _matrix([])
        client.get_project_compute_snapshot(MATCHER)
        client.get_project_compute_rightsizing(MATCHER)
        client.get_project_compute_forecast(MATCHER, 15)
        for resource in pc.TREND_RESOURCES:
            for range_key in pc.TREND_RANGES:
                client.get_project_compute_trend(
                    MATCHER, resource, range_key, at=1_795_190_000
                )
        client.get_project_compute_cpu_series(MATCHER, [ID1, ID2])
        client.get_project_compute_instance_trend(MATCHER, ID1)
        instant = [c.kwargs["query"] for c in prom.query.call_args_list]
        ranged = [c.args[1]["query"] for c in prom._get.call_args_list]
        return instant, ranged

    def test_scoped(self):
        instant, ranged = self.sent()
        self.assertEqual(len(instant), 3)
        self.assertEqual(len(ranged), 3 * 4 + 2)
        for query in instant + ranged:
            self.assertScoped(query)

    def test_invalid_matcher_sends_nothing(self):
        client, prom = _make_client()
        for call in (
            lambda: client.get_project_compute_snapshot(""),
            lambda: client.get_project_compute_rightsizing('project=~".+"'),
            lambda: client.get_project_compute_forecast(None, 15),
            lambda: client.get_project_compute_trend("", "cores", "6h"),
        ):
            with self.assertRaises(ValueError):
                call()
        prom.query.assert_not_called()
        prom._get.assert_not_called()


class SnapshotTests(_QueryTestCase):
    def test_query(self):
        query = queries.snapshot_query(MATCHER)
        self.assertScoped(query)
        for field in queries.INSTANCE_FIELDS:
            self.assertIn(f'"field", "{field}"', query)
        for field in queries.DISK_FIELDS:
            self.assertIn(f'"field", "disk:{field}"', query)
        for metric in (
            "ceilometer_cpu",
            "ceilometer_cpu_delay",
            "ceilometer_vcpus",
            "ceilometer_memory",
            "ceilometer_memory_usage",
            "ceilometer_memory_swap_in",
            "ceilometer_memory_swap_out",
            "ceilometer_disk_device_usage",
            "ceilometer_disk_device_capacity",
            "ceilometer_disk_device_write_bytes",
            "ceilometer_disk_device_write_requests",
            "ceilometer_disk_device_write_latency",
        ):
            self.assertIn(f"{metric}{{", query)
        self.assertIn("[10m]", query)
        # one value per resource: a renamed instance has a series per name
        # in the window, which must not count twice
        self.assertNotIn("resource_name)", query)
        self.assertIn(
            "max by (resource) (last_over_time(ceilometer_vcpus{", query
        )
        self.assertIn(
            "max by (resource) (last_over_time(ceilometer_memory{", query
        )
        # the name: the series with the latest sample of each instance
        self.assertIn('"field", "name"', query)
        self.assertIn(
            "topk by (resource) (1, max_over_time(timestamp(ceilometer_vcpus{",
            query,
        )
        self.assertIn("})[10m:1m]))", query)
        # ns per request -> ms
        self.assertIn(" / 1e6", query)

    def test_name_falls_back_to_the_memory_meters(self):
        # an instance with memory samples but no vCPU samples in the window
        # still gets its name: the latest sample of any of the three wins
        query = queries.name_expr(MATCHER)
        self.assertScoped(query)
        self.assertTrue(query.startswith("topk by (resource) (1, "))
        for metric in (
            "ceilometer_vcpus",
            "ceilometer_memory",
            "ceilometer_memory_usage",
        ):
            self.assertIn(f"max_over_time(timestamp({metric}{{", query)
        self.assertEqual(query.count("})[10m:1m]) or "), 2)

    def test_parse(self):
        client, prom = _make_client()
        name = f"grian-ih-cpu-2:instance-{ID1}"
        prom.query.return_value = [
            _make_metric(
                "1763600000",
                {"resource": ID1, "resource_name": name, "field": "name"},
            ),
            _make_metric("4", {"resource": ID1, "field": "vcpus"}),
            _make_metric("256", {"resource": ID1, "field": "memory_alloc"}),
            _make_metric("3.47", {"resource": ID1, "field": "cpu"}),
            _make_metric("0.52", {"resource": ID1, "field": "cpu_wait"}),
            _make_metric("45.0", {"resource": ID1, "field": "memory"}),
            _make_metric("NaN", {"resource": ID1, "field": "swap"}),
            _make_metric(
                "31522816", {"resource": f"{ID1}-vda", "field": "disk:used"}
            ),
            _make_metric(
                "1073741824",
                {"resource": f"{ID1}-vda", "field": "disk:capacity"},
            ),
            _make_metric(
                "2.7", {"resource": f"{ID1}-vdb", "field": "disk:latency"}
            ),
            # ignored: no instance, unknown field, disk field on an instance
            _make_metric("1", {"resource": "bogus", "field": "cpu"}),
            _make_metric("1", {"resource": ID2, "field": "bogus"}),
            _make_metric("1", {"resource": ID2, "field": "disk:used"}),
            _make_metric("1", {"resource": f"{ID2}-vda", "field": "cpu"}),
            _make_metric("1", {"resource": f"{ID2}-vda", "field": "disk:x"}),
        ]
        result = client.get_project_compute_snapshot(MATCHER)
        self.assertEqual(prom.query.call_count, 1)
        instance = result["instances"][ID1]
        self.assertEqual(instance["name"], "grian-ih-cpu-2")
        self.assertEqual(instance["vcpus"], 4.0)
        self.assertEqual(instance["memory_alloc"], 256.0)
        self.assertEqual(instance["cpu"], 3.47)
        self.assertEqual(instance["cpu_wait"], 0.52)
        self.assertIsNone(instance["swap"])
        self.assertEqual(
            instance["disks"]["vda"],
            {
                "used": 31522816.0,
                "capacity": 1073741824.0,
                "write": None,
                "latency": None,
            },
        )
        self.assertEqual(instance["disks"]["vdb"]["latency"], 2.7)
        # ID2 only had ignored samples
        self.assertEqual(result["instances"][ID2]["disks"], {})
        self.assertIsNone(result["instances"][ID2]["name"])

    def test_renamed_instance(self):
        # renamed 3 minutes ago: two vcpus series for one resource in the
        # window; the query reduces them to one value and the newest name
        client, prom = _make_client()
        prom.query.return_value = [
            _make_metric("2", {"resource": ID1, "field": "vcpus"}),
            _make_metric("2048", {"resource": ID1, "field": "memory_alloc"}),
            _make_metric(
                "1763600000",
                {
                    "resource": ID1,
                    "resource_name": f"new-name:instance-{ID1}",
                    "field": "name",
                },
            ),
        ]
        instance = client.get_project_compute_snapshot(MATCHER)["instances"][
            ID1
        ]
        self.assertEqual(instance["name"], "new-name")
        self.assertEqual(instance["vcpus"], 2.0)
        self.assertEqual(instance["memory_alloc"], 2048.0)

    def test_empty(self):
        client, prom = _make_client()
        prom.query.return_value = None
        self.assertEqual(
            client.get_project_compute_snapshot(MATCHER), {"instances": {}}
        )


class RightsizingTests(_QueryTestCase):
    def test_query(self):
        query = queries.rightsizing_query(MATCHER)
        self.assertScoped(query)
        for field in queries.RIGHTSIZING_FIELDS:
            self.assertIn(f'"field", "{field}"', query)
        self.assertIn("avg_over_time((max by (resource) (rate(", query)
        self.assertIn("quantile_over_time(0.95, (", query)
        self.assertIn("[7d:10m]", query)
        self.assertIn("increase(ceilometer_memory_swap_in{", query)
        self.assertIn("increase(ceilometer_memory_swap_out{", query)
        self.assertIn("count_over_time((max by (resource) (", query)
        self.assertIn("[7d:1h]", query)

    def test_parse(self):
        client, prom = _make_client()
        prom.query.return_value = [
            _make_metric("3.52", {"resource": ID1, "field": "cpu_avg"}),
            _make_metric("3.57", {"resource": ID1, "field": "cpu_p95"}),
            _make_metric("45.0", {"resource": ID1, "field": "memory_avg"}),
            _make_metric("0", {"resource": ID1, "field": "swap"}),
            _make_metric("54", {"resource": ID1, "field": "hours"}),
            _make_metric("1", {"resource": f"{ID2}-vda", "field": "hours"}),
            _make_metric("1", {"resource": ID2, "field": "bogus"}),
        ]
        result = client.get_project_compute_rightsizing(MATCHER)
        self.assertEqual(
            result,
            {
                "instances": {
                    ID1: {
                        "cpu_avg": 3.52,
                        "cpu_p95": 3.57,
                        "memory_avg": 45.0,
                        "swap": 0.0,
                        "hours": 54.0,
                    }
                }
            },
        )


class ForecastTests(_QueryTestCase):
    def test_query(self):
        query = queries.forecast_query(MATCHER, 15)
        self.assertScoped(query)
        self.assertEqual(query.count("deriv(("), 3)
        self.assertIn("[15d:1h]", query)
        self.assertNotIn("[30d", query)
        for field in (*pc.QUOTAS, "hours"):
            self.assertIn(f'"field", "{field}"', query)
        self.assertIn("count(max by (resource)", query)
        self.assertNotIn("resource_name)", query)
        self.assertIn(
            "sum(max by (resource) (last_over_time(ceilometer_memory{", query
        )

    def test_parse(self):
        client, prom = _make_client()
        prom.query.return_value = [
            _make_metric("0.0001", {"field": "cores"}),
            _make_metric("NaN", {"field": "ram"}),
            _make_metric("54", {"field": "hours"}),
            _make_metric("1", {"field": "bogus"}),
        ]
        result = client.get_project_compute_forecast(MATCHER, 15)
        self.assertEqual(
            result,
            {
                "slopes": {"instances": None, "cores": 0.0001, "ram": None},
                "hours": 54.0,
                "days": 15,
            },
        )


class TrendTests(_QueryTestCase):
    def test_queries(self):
        self.assertEqual(
            queries.trend_query(MATCHER, pc.QUOTA_CORES, "24h", "15m"),
            "(sum(max by (resource) (last_over_time(ceilometer_vcpus{"
            f'{queries.POLLED},{MATCHER}}}[10m]))))[24h:15m]',
        )
        ram = queries.trend_query(MATCHER, pc.QUOTA_RAM, "6h", "5m")
        self.assertIn("ceilometer_memory{", ram)
        instances = queries.trend_query(
            MATCHER, pc.QUOTA_INSTANCES, "6h", "5m"
        )
        self.assertTrue(instances.startswith("(count(max by (resource)"))

    def test_evaluated_at_the_grid_time(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([({}, [[100, "5"], [200, "6"]])])
        result = client.get_project_compute_trend(
            MATCHER, pc.QUOTA_CORES, "30d", at=1_795_190_000.5
        )
        self.assertEqual(result, {"points": [[100.0, 5.0], [200.0, 6.0]]})
        params = prom._get.call_args.args[1]
        self.assertEqual(params["time"], "1795190000.500")
        self.assertTrue(params["query"].endswith("[30d:8h]"))

    def test_empty(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix([])
        self.assertEqual(
            client.get_project_compute_trend(MATCHER, "ram", "6h"),
            {"points": []},
        )


class CpuSeriesTests(_QueryTestCase):
    def test_query_lists_the_instances(self):
        query = queries.cpu_series_query(MATCHER, [ID2, ID1, "bad"])
        self.assertScoped(query)
        self.assertIn(f'resource=~"{ID2}|{ID1}"', query)
        self.assertTrue(query.endswith("[7d:2h]"))
        self.assertIn("rate(ceilometer_cpu{", query)
        self.assertIsNone(queries.cpu_series_query(MATCHER, ["bad"]))

    def test_parse(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix(
            [
                ({"resource": ID1}, [[100, "3.5"]]),
                ({"resource": "nope"}, [[100, "1"]]),
            ]
        )
        result = client.get_project_compute_cpu_series(MATCHER, [ID1])
        self.assertEqual(result, {"series": {ID1: [[100.0, 3.5]]}})

    def test_no_valid_instance_no_query(self):
        client, prom = _make_client()
        self.assertEqual(
            client.get_project_compute_cpu_series(MATCHER, ["x"]),
            {"series": {}},
        )
        prom._get.assert_not_called()


class InstanceTrendTests(_QueryTestCase):
    def test_query(self):
        query = queries.instance_trend_query(MATCHER, ID1)
        self.assertScoped(query)
        self.assertIn(f'resource=~"{ID1}"', query)
        self.assertIn('"field", "cpu"', query)
        self.assertIn('"field", "memory"', query)
        self.assertTrue(query.endswith("[7d:1h]"))
        self.assertIsNone(queries.instance_trend_query(MATCHER, "../x"))

    def test_parse(self):
        client, prom = _make_client()
        prom._get.return_value = _matrix(
            [
                ({"resource": ID1, "field": "cpu"}, [[100, "3.5"]]),
                ({"resource": ID1, "field": "memory"}, [[100, "45"]]),
                ({"resource": ID1, "field": "other"}, [[100, "1"]]),
            ]
        )
        self.assertEqual(
            client.get_project_compute_instance_trend(MATCHER, ID1),
            {"cpu": [[100.0, 3.5]], "memory": [[100.0, 45.0]]},
        )

    def test_invalid_instance_no_query(self):
        client, prom = _make_client()
        self.assertEqual(
            client.get_project_compute_instance_trend(MATCHER, "x"),
            {"cpu": [], "memory": []},
        )
        prom._get.assert_not_called()
