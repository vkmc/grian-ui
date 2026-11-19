#
# SPDX-License-Identifier: Apache-2.0

from unittest import mock

from django.test import override_settings

from grian_ui.datasource import api
from grian_ui_tests.unit import base


def _make_metric(value, labels=None):
    """Create a mock Prometheus metric result."""
    metric = mock.MagicMock()
    metric.value = value
    metric.labels = labels or {}
    return metric


def _make_client(query_map=None):
    """Create a PrometheusTelemetryClient with a mocked prom client."""
    with (
        mock.patch("grian_ui.datasource.prometheus.config") as mock_config,
        mock.patch("grian_ui.datasource.prometheus.v3"),
        mock.patch("grian_ui.datasource.prometheus.session"),
        mock.patch(
            "grian_ui.datasource.prometheus.obs_client_utils"
        ) as mock_obs,
    ):
        mock_config.get_openstack_keystone_url.return_value = (
            "http://keystone:5000/v3"
        )
        mock_config.get_openstack_endpoint_type.return_value = "publicURL"

        request = mock.MagicMock()
        request.user.token.id = "test-token"
        request.user.project_id = "test-project"
        request.user.domain_id = "test-domain"
        request.user.services_region = "RegionOne"

        from grian_ui.datasource import prometheus

        client = prometheus.get_client(request)

        prom_client = mock_obs.get_prom_client_from_keystone.return_value
        if query_map:
            prom_client.query.side_effect = lambda query: query_map.get(
                query, []
            )

        return client, prom_client


class PrometheusTelemetryClientInitTests(base.TestCase):
    @mock.patch("grian_ui.datasource.prometheus.obs_client_utils")
    @mock.patch("grian_ui.datasource.prometheus.session")
    @mock.patch("grian_ui.datasource.prometheus.v3")
    @mock.patch("grian_ui.datasource.prometheus.config")
    def test_init_creates_client(
        self, mock_config, mock_v3, mock_session, mock_obs
    ):
        mock_config.get_openstack_keystone_url.return_value = (
            "http://keystone:5000/v3"
        )
        mock_config.get_openstack_endpoint_type.return_value = "publicURL"

        request = mock.MagicMock()
        request.user.token.id = "test-token"
        request.user.project_id = "test-project"
        request.user.domain_id = "test-domain"
        request.user.services_region = "RegionOne"

        from grian_ui.datasource import prometheus

        client = prometheus.PrometheusTelemetryClient(request)

        mock_v3.Token.assert_called_once_with(  # nosec B106
            auth_url="http://keystone:5000/v3",
            token="test-token",
            project_id="test-project",
            domain_id="test-domain",
        )
        mock_obs.get_prom_client_from_keystone.assert_called_once()
        self.assertIsInstance(client, api.DatasourceClient)


class GetVolumesCountTests(base.TestCase):
    def test_returns_count(self):
        client, prom = _make_client()
        prom.query.return_value = [_make_metric(42)]

        result = client.get_volumes_count()

        self.assertEqual(result, 42)
        prom.query.assert_called_once_with(
            query="count(last_over_time(ceilometer_volume_size[10m]))"
        )

    def test_empty_result_returns_zero(self):
        client, prom = _make_client()
        prom.query.return_value = []

        self.assertEqual(client.get_volumes_count(), 0)

    def test_none_result_returns_zero(self):
        client, prom = _make_client()
        prom.query.return_value = None

        self.assertEqual(client.get_volumes_count(), 0)


class GetSnapshotsCountTests(base.TestCase):
    def test_returns_count(self):
        client, prom = _make_client()
        prom.query.return_value = [_make_metric(15)]

        result = client.get_snapshots_count()

        self.assertEqual(result, 15)
        prom.query.assert_called_once_with(
            query="count(last_over_time(ceilometer_volume_snapshot_size[10m]))"
        )

    def test_empty_result_returns_zero(self):
        client, prom = _make_client()
        prom.query.return_value = []

        self.assertEqual(client.get_snapshots_count(), 0)


def _pool_query(field, window="24h"):
    return (
        "max by (resource) (last_over_time("
        f"ceilometer_volume_provider_pool_capacity_{field}[{window}]))"
    )


def _predict_query(field):
    return (
        "max by (resource) (predict_linear("
        f"ceilometer_volume_provider_pool_capacity_{field}[7d], 30 * 86400))"
    )


class GetStoragePoolsTests(base.TestCase):
    def _query_map(self, last_seen):
        from grian_ui.datasource import prometheus

        def pool(value):
            return [_make_metric(value, {"resource": "pool-01"})]

        return {
            _pool_query("total"): pool(5120.0),
            _pool_query("allocated"): pool(4096.0),
            _pool_query("free"): pool(1024.0),
            _pool_query("provisioned"): pool(6000.0),
            _pool_query("virtual_free"): pool(9000.0),
            prometheus.pool_last_seen_query(): pool(last_seen),
        }

    @mock.patch("grian_ui.datasource.prometheus.time.time", return_value=1e6)
    def test_returns_pools(self, _time):
        client, prom = _make_client(self._query_map(1e6 - 60))

        result = client.get_storage_pools()

        self.assertEqual(
            result,
            [
                {
                    "name": "pool-01",
                    "total": 5120.0,
                    "allocated": 4096.0,
                    "free": 1024.0,
                    "provisioned": 6000.0,
                    "virtual_free": 9000.0,
                    "last_seen": 1e6 - 60,
                    "stale": False,
                }
            ],
        )

    def test_one_query_per_metric(self):
        client, prom = _make_client(self._query_map(0))
        client.get_storage_pools()
        # five metrics and the last report, whatever the number of pools
        self.assertEqual(prom.query.call_count, 6)
        for call in prom.query.call_args_list:
            self.assertNotIn('resource="', call.kwargs["query"])

    @mock.patch("grian_ui.datasource.prometheus.time.time", return_value=1e6)
    def test_pool_silent_for_ten_minutes_is_stale(self, _time):
        client, _ = _make_client(self._query_map(1e6 - 601))
        self.assertTrue(client.get_storage_pools()[0]["stale"])

    @override_settings(GRIAN_PLUGIN={"storage_stale_minutes": 30})
    @mock.patch("grian_ui.datasource.prometheus.time.time", return_value=1e6)
    def test_stale_minutes_setting(self, _time):
        from grian_ui.datasource import prometheus

        client, _ = _make_client(self._query_map(1e6 - 1200))
        self.assertFalse(client.get_storage_pools()[0]["stale"])
        client, _ = _make_client(self._query_map(1e6 - 1801))
        self.assertTrue(client.get_storage_pools()[0]["stale"])
        self.assertIn("[35m:30s]", prometheus.pool_last_seen_query())
        self.assertEqual(prometheus.stale_window(), "30m")

    def test_last_seen_query(self):
        from grian_ui.datasource import prometheus

        query = prometheus.pool_last_seen_query()
        self.assertIn(
            "timestamp(ceilometer_volume_provider_pool_capacity_total)", query
        )
        self.assertIn("[15m:30s]", query)
        self.assertIn("[24h:5m]", query)

    def test_empty_result_returns_empty_list(self):
        client, prom = _make_client()
        prom.query.return_value = None

        self.assertEqual(client.get_storage_pools(), [])

    def test_errors_are_raised(self):
        client, prom = _make_client()
        prom.query.side_effect = Exception("timeout")

        self.assertRaises(Exception, client.get_storage_pools)

    def test_missing_sub_metrics(self):
        query_map = {
            _pool_query("total"): [
                _make_metric(5120.0, {"resource": "pool-01"})
            ],
            # a pool without a total is ignored
            _pool_query("free"): [_make_metric(1.0, {"resource": "other"})],
        }
        client, _ = _make_client(query_map)

        result = client.get_storage_pools()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["allocated"], 0.0)
        self.assertEqual(result[0]["free"], 0.0)
        self.assertEqual(result[0]["provisioned"], 0.0)
        self.assertIsNone(result[0]["virtual_free"])
        self.assertIsNone(result[0]["last_seen"])
        self.assertTrue(result[0]["stale"])


class GetCapacityPredictionsTests(base.TestCase):
    def _query_map(self, free, free_30d, vfree, vfree_30d, history=8 * 86400):
        from grian_ui.datasource import prometheus

        def pool(value):
            if value is None:
                return []
            return [_make_metric(value, {"resource": "pool-01"})]

        return {
            _pool_query("free", "10m"): pool(free),
            _pool_query("virtual_free", "10m"): pool(vfree),
            _predict_query("free"): pool(free_30d),
            _predict_query("virtual_free"): pool(vfree_30d),
            prometheus.pool_history_query(): pool(history),
        }

    def test_virtual_capacity_runs_out_first(self):
        # thin pool: free stays flat, virtual free shrinks 100 GiB/day
        client, prom = _make_client(
            self._query_map(1000.0, 1000.0, 1500.0, -1500.0)
        )

        result = client.get_capacity_predictions()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "pool-01")
        self.assertIsNone(result[0]["days_physical"])
        self.assertEqual(result[0]["days_virtual"], 15.0)
        self.assertEqual(result[0]["days_until_full"], 15.0)
        self.assertEqual(result[0]["limiting"], "virtual")
        self.assertTrue(result[0]["will_run_out"])
        self.assertEqual(result[0]["history_days"], 7.0)
        self.assertEqual(prom.query.call_count, 5)

    def test_physical_capacity_runs_out_first(self):
        client, _ = _make_client(
            self._query_map(1000.0, -2000.0, 5000.0, 2000.0)
        )

        result = client.get_capacity_predictions()[0]

        self.assertEqual(result["days_physical"], 10.0)
        self.assertEqual(result["days_virtual"], 50.0)
        self.assertEqual(result["limiting"], "physical")
        self.assertEqual(result["days_until_full"], 10.0)

    def test_not_shrinking(self):
        client, _ = _make_client(
            self._query_map(2000.0, 2500.0, 3000.0, 3000.0)
        )

        result = client.get_capacity_predictions()[0]

        self.assertFalse(result["will_run_out"])
        self.assertIsNone(result["days_until_full"])
        self.assertIsNone(result["limiting"])

    def test_pool_without_virtual_free(self):
        client, _ = _make_client(self._query_map(1000.0, 700.0, None, None))

        result = client.get_capacity_predictions()[0]

        self.assertEqual(result["days_physical"], 100.0)
        self.assertIsNone(result["days_virtual"])
        self.assertIsNone(result["current_virtual_free"])
        self.assertEqual(result["limiting"], "physical")

    def test_insufficient_history(self):
        client, _ = _make_client(
            self._query_map(
                1000.0, -2000.0, 5000.0, 2000.0, history=1.25 * 86400
            )
        )

        result = client.get_capacity_predictions()[0]

        self.assertFalse(result["enough_history"])
        self.assertEqual(result["history_days"], 1.2)
        self.assertFalse(result["will_run_out"])
        self.assertIsNone(result["days_until_full"])

    def test_empty_result_returns_empty_list(self):
        client, prom = _make_client()
        prom.query.return_value = None

        self.assertEqual(client.get_capacity_predictions(), [])

    def test_errors_are_raised(self):
        client, prom = _make_client()
        prom.query.side_effect = Exception("timeout")

        self.assertRaises(Exception, client.get_capacity_predictions)

    def test_days_until_zero(self):
        from grian_ui.datasource import prometheus

        # decline_per_day = (1500 - (-1500)) / 30 = 100
        self.assertEqual(prometheus.days_until_zero(1500.0, -1500.0), 15.0)
        self.assertEqual(prometheus.days_until_zero(0.0, -30.0), 0.0)
        self.assertIsNone(prometheus.days_until_zero(10.0, 10.0))
        self.assertIsNone(prometheus.days_until_zero(None, 1.0))
        self.assertIsNone(prometheus.days_until_zero(1.0, None))


class NumberTests(base.TestCase):
    def test_number(self):
        from grian_ui.datasource import prometheus

        self.assertEqual(prometheus.number("42.04"), 42.04)
        self.assertEqual(prometheus.number("42.04", 1), 42.0)
        self.assertEqual(prometheus.number(3), 3.0)
        self.assertIsNone(prometheus.number("NaN"))
        self.assertIsNone(prometheus.number("+Inf"))
        self.assertIsNone(prometheus.number("-Inf"))
        self.assertIsNone(prometheus.number(None))
        self.assertIsNone(prometheus.number("abc"))


class QueryMatrixTests(base.TestCase):
    def _matrix(self, series):
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

    def test_decodes_series_and_drops_non_finite_values(self):
        client, prom = _make_client()
        prom._get.return_value = self._matrix(
            [
                ({"a": "1"}, [[100, "1.04"], [160, "NaN"], [220, "2"]]),
                ({"a": "2"}, []),
            ]
        )
        result = client.query_matrix("up[1h:1m]")
        prom._get.assert_called_once_with("query", {"query": "up[1h:1m]"})
        self.assertEqual(
            result,
            [({"a": "1"}, [[100.0, 1.04], [220.0, 2.0]]), ({"a": "2"}, [])],
        )

    def test_evaluation_time(self):
        client, prom = _make_client()
        prom._get.return_value = self._matrix([])
        client.query_matrix("up[1h:1m]", time=1700000000.25)
        prom._get.assert_called_once_with(
            "query", {"query": "up[1h:1m]", "time": "1700000000.250"}
        )

    def test_ndigits(self):
        client, prom = _make_client()
        prom._get.return_value = self._matrix([({}, [[1, "1.23456"]])])
        self.assertEqual(
            client.query_matrix("x", ndigits=3), [({}, [[1.0, 1.235]])]
        )

    def test_ignores_vector_results(self):
        client, prom = _make_client()
        prom._get.return_value = {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        }
        with self.assertLogs("grian_ui.datasource.prometheus", "WARNING"):
            self.assertEqual(client.query_matrix("up"), [])


class SessionTimeoutTests(base.TestCase):
    def test_aetos_requests_time_out(self):
        from grian_ui.datasource import prometheus

        with (
            mock.patch("grian_ui.datasource.prometheus.session") as sess,
            mock.patch("grian_ui.datasource.prometheus.config"),
            mock.patch("grian_ui.datasource.prometheus.v3"),
            mock.patch("grian_ui.datasource.prometheus.obs_client_utils"),
        ):
            prometheus.PrometheusTelemetryClient(mock.MagicMock())
        sess.Session.assert_called_once()
        self.assertEqual(
            sess.Session.call_args.kwargs["timeout"], prometheus.QUERY_TIMEOUT
        )
