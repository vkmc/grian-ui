#
# SPDX-License-Identifier: Apache-2.0

from unittest import mock

from django.test import override_settings

from grian_ui.datasource import prometheus_storage
from grian_ui_tests.unit import base


def _metric(value, **labels):
    metric = mock.MagicMock()
    metric.value = value
    metric.labels = labels
    return metric


def _client(results=(), matrix=()):
    client = mock.MagicMock()
    client.client.query.return_value = list(results)
    client.query_matrix.return_value = list(matrix)
    return client


class ServicesTests(base.TestCase):
    def test_current_status(self):
        client = _client(
            [
                _metric("1", resource="cinder-volume@h@lvm", window="now"),
                _metric("1", resource="cinder-volume@h@lvm", window="day"),
                _metric("0", resource="cinder-volume@h@nfs", window="now"),
                _metric("0", resource="cinder-volume@h@nfs", window="day"),
                _metric("1", resource="cinder-scheduler@h", window="day"),
                _metric("1", window="now"),
            ]
        )
        services = prometheus_storage.get_services(client)
        self.assertEqual(
            [(s["binary"], s["host"], s["up"]) for s in services],
            [
                ("cinder-scheduler", "h", None),
                ("cinder-volume", "h@lvm", True),
                ("cinder-volume", "h@nfs", False),
            ],
        )
        query = client.client.query.call_args.kwargs["query"]
        self.assertEqual(client.client.query.call_count, 1)
        self.assertIn(
            "max by (resource) (last_over_time("
            "ceilometer_volume_service_health[10m]))",
            query,
        )
        self.assertIn("[24h]", query)

    @override_settings(GRIAN_PLUGIN={"storage_stale_minutes": 30})
    def test_staleness_window_setting(self):
        client = _client()
        prometheus_storage.get_services(client)
        query = client.client.query.call_args.kwargs["query"]
        self.assertIn("ceilometer_volume_service_health[30m]", query)
        prometheus_storage.get_capacity_trend(client, "24h")
        self.assertIn(
            "capacity_total[30m]", client.query_matrix.call_args.args[0]
        )

    def test_history(self):
        client = _client(
            matrix=[
                ({"resource": "cinder-volume@h@b"}, [[1.0, 1.0]]),
                ({}, []),
            ]
        )
        history = prometheus_storage.get_service_history(client, at=123.0)
        self.assertEqual(history, {"cinder-volume@h@b": [[1.0, 1.0]]})
        client.query_matrix.assert_called_once_with(
            "min by (resource) (min_over_time("
            "ceilometer_volume_service_health[15m]))[24h:15m]",
            time=123.0,
        )

    def test_split_service(self):
        self.assertEqual(
            prometheus_storage.split_service("cinder-volume@h@b"),
            ("cinder-volume", "h@b"),
        )
        self.assertEqual(prometheus_storage.split_service(None), ("", ""))


class CapacityTrendTests(base.TestCase):
    def test_whole_cloud(self):
        client = _client(
            matrix=[
                ({"field": "allocated"}, [[1.0, 2.0]]),
                ({"field": "total"}, [[1.0, 9.0]]),
            ]
        )
        trend = prometheus_storage.get_capacity_trend(client, "14d", at=5.0)
        self.assertEqual(
            trend, {"allocated": [[1.0, 2.0]], "total": [[1.0, 9.0]]}
        )
        query = client.query_matrix.call_args.args[0]
        self.assertTrue(query.endswith(")[14d:2h]"))
        self.assertIn(
            'label_replace(sum(max by (resource) (last_over_time('
            'ceilometer_volume_provider_pool_capacity_provisioned[10m]))), '
            '"field", "provisioned", "", "")',
            query,
        )
        self.assertEqual(client.query_matrix.call_args.kwargs["time"], 5.0)

    def test_pools(self):
        client = _client(matrix=[({"resource": "h@b#p"}, [[1.0, 50.0]])])
        trend = prometheus_storage.get_capacity_trend(
            client, "24h", names=["h@b#p", "a.b"]
        )
        self.assertEqual(trend, {"h@b#p": [[1.0, 50.0]]})
        query = client.query_matrix.call_args.args[0]
        # regular expression escaped for RE2, then for the PromQL string
        self.assertIn('resource=~"h@b\\\\#p|a\\\\.b"', query)
        self.assertIn("100 * max by (resource)", query)
        self.assertTrue(query.endswith("[24h:15m]"))

    def test_pool_name_with_a_quote(self):
        client = _client(matrix=[])
        prometheus_storage.get_capacity_trend(client, "24h", names=['h@b#"p'])
        query = client.query_matrix.call_args.args[0]
        # the quote is escaped for the PromQL string
        self.assertIn('resource=~"h@b\\\\#\\"p"', query)


class ImagesTests(base.TestCase):
    def test_totals(self):
        client = _client(
            [
                _metric("2", field="images"),
                _metric("514523136", field="image_bytes"),
                _metric("1", field="image_projects"),
                _metric("7", field="other"),
            ]
        )
        result = prometheus_storage.get_images_and_backups(client)
        self.assertEqual(
            result,
            {
                "images": 2.0,
                "image_bytes": 514523136.0,
                "image_projects": 1.0,
                "images_downloaded": None,
                "backups": None,
                "backup_gib": None,
            },
        )
        query = client.client.query.call_args.kwargs["query"]
        self.assertIn("ceilometer_volume_backup_size[1h]", query)
        self.assertIn("count_over_time(ceilometer_image_download[24h])", query)
        self.assertEqual(client.client.query.call_count, 1)
