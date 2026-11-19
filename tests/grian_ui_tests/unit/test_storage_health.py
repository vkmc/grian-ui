#
# SPDX-License-Identifier: Apache-2.0

import types

from django.test import override_settings

from grian_ui import config
from grian_ui import storage_health
from grian_ui import storage_inventory
from grian_ui_tests.unit import base


NOW = 1_800_000_000.0
DAY = 86400.0
THRESHOLDS = dict(config.DEFAULT_STORAGE_THRESHOLDS)


def _pool(**kwargs):
    pool = {
        "name": "host@lvm#pool",
        "total": 100.0,
        "allocated": 10.0,
        "free": 90.0,
        "provisioned": 20.0,
        "virtual_free": 1980.0,
        "last_seen": NOW,
        "stale": False,
    }
    pool.update(kwargs)
    return pool


def _prediction(days=None, enough=True):
    return {
        "name": "host@lvm#pool",
        "days_until_full": days,
        "limiting": "virtual" if days is not None else None,
        "will_run_out": days is not None,
        "enough_history": enough,
        "history_days": 7.0 if enough else 2.0,
    }


def _volume(vid, status="available", age=DAY, attached=False, **kwargs):
    volume = {
        "id": vid,
        "name": vid,
        "project_id": "p1",
        "status": status,
        "size": 1,
        "host": "host@lvm#pool",
        "attached": attached,
        "created": NOW - age,
        "updated": NOW - age,
    }
    volume.update(kwargs)
    return volume


def _snapshot(sid, volume_id, age, status="available"):
    return {
        "id": sid,
        "name": sid,
        "project_id": "p1",
        "status": status,
        "size": 1,
        "volume_id": volume_id,
        "created": NOW - age,
        "updated": NOW - age,
    }


class StorageThresholdsTests(base.TestCase):
    def test_defaults(self):
        self.assertEqual(
            config.get_storage_thresholds(), config.DEFAULT_STORAGE_THRESHOLDS
        )

    @override_settings(
        GRIAN_PLUGIN={"storage_thresholds": {"physical_warning": 60}}
    )
    def test_override(self):
        self.assertEqual(
            config.get_storage_thresholds()["physical_warning"], 60
        )

    @override_settings(
        GRIAN_PLUGIN={"storage_thresholds": {"virtual_critical": 150}}
    )
    def test_percentages_up_to_100(self):
        self.assertRaisesRegex(
            config.ConfigurationError,
            "percentage up to 100",
            config.get_storage_thresholds,
        )

    @override_settings(
        GRIAN_PLUGIN={"storage_thresholds": {"forecast_critical_days": 60}}
    )
    def test_forecast_critical_within_warning(self):
        self.assertRaisesRegex(
            config.ConfigurationError,
            "forecast_critical_days",
            config.get_storage_thresholds,
        )

    @override_settings(
        GRIAN_PLUGIN={
            "storage_thresholds": {
                "physical_warning": 95,
                "physical_critical": 90,
            }
        }
    )
    def test_warning_above_critical(self):
        self.assertRaises(
            config.ConfigurationError, config.get_storage_thresholds
        )

    @override_settings(GRIAN_PLUGIN={"storage_thresholds": {"nope": 1}})
    def test_unknown_key(self):
        self.assertRaises(
            config.ConfigurationError, config.get_storage_thresholds
        )


class EvaluatePoolTests(base.TestCase):
    def test_healthy_thin_pool(self):
        row = storage_health.evaluate_pool(
            _pool(), _prediction(enough=False), THRESHOLDS
        )
        self.assertEqual(row["limit"], 2000.0)
        self.assertEqual(row["physical_percent"], 10.0)
        self.assertEqual(row["virtual_percent"], 1.0)
        self.assertEqual(row["level"], "ok")
        self.assertEqual(row["status"], "ok")

    def test_physical_use(self):
        row = storage_health.evaluate_pool(
            _pool(allocated=91.0), None, THRESHOLDS
        )
        self.assertEqual(row["physical_level"], "critical")
        self.assertEqual(row["level"], "critical")

    def test_virtual_use(self):
        # provisioned 1800 of a 2000 GiB limit: 90%
        row = storage_health.evaluate_pool(
            _pool(provisioned=1800.0, virtual_free=200.0), None, THRESHOLDS
        )
        self.assertEqual(row["virtual_percent"], 90.0)
        self.assertEqual(row["virtual_level"], "critical")

    def test_without_virtual_free(self):
        row = storage_health.evaluate_pool(
            _pool(virtual_free=None), None, THRESHOLDS
        )
        self.assertIsNone(row["limit"])
        self.assertIsNone(row["virtual_percent"])
        self.assertEqual(row["virtual_level"], "ok")

    def test_forecast(self):
        level = storage_health.evaluate_pool(
            _pool(), _prediction(days=14), THRESHOLDS
        )["level"]
        self.assertEqual(level, "critical")
        level = storage_health.evaluate_pool(
            _pool(), _prediction(days=30), THRESHOLDS
        )["level"]
        self.assertEqual(level, "warning")
        level = storage_health.evaluate_pool(
            _pool(), _prediction(days=31), THRESHOLDS
        )["level"]
        self.assertEqual(level, "ok")

    def test_stale_pool(self):
        row = storage_health.evaluate_pool(
            _pool(stale=True), _prediction(days=5), THRESHOLDS
        )
        # frozen values: no forecast, at least a warning
        self.assertIsNone(row["forecast"])
        self.assertEqual(row["level"], "warning")
        self.assertEqual(row["status"], "stale")

    def test_empty_pool(self):
        row = storage_health.evaluate_pool(
            _pool(total=0.0, allocated=0.0), None, THRESHOLDS
        )
        self.assertIsNone(row["physical_percent"])

    def test_evaluate_pools_matches_predictions_by_name(self):
        rows = storage_health.evaluate_pools(
            [_pool(), _pool(name="other")], [_prediction(days=3)], THRESHOLDS
        )
        self.assertEqual([r["level"] for r in rows], ["critical", "ok"])
        rows = storage_health.evaluate_pools([_pool()], None, THRESHOLDS)
        self.assertIsNone(rows[0]["forecast"])


class SummarizeInventoryTests(base.TestCase):
    def summarize(self, volumes, snapshots=()):
        return storage_health.summarize_inventory(
            volumes, list(snapshots), THRESHOLDS, NOW
        )

    def test_rules(self):
        volumes = [
            _volume("err", "error", age=3600),
            _volume("err-ext", "error_extending", age=7200),
            _volume("stuck", "attaching", age=31 * 60),
            _volume("busy", "creating", age=29 * 60),
            _volume("old", "available", age=30 * DAY),
            _volume("recent", "available", age=3 * DAY),
            _volume("used", "in-use", age=90 * DAY, attached=True),
            _volume("old-attached", "available", age=90 * DAY, attached=True),
        ]
        result = self.summarize(volumes)
        self.assertEqual(
            [(r["id"], r["rule"], r["level"]) for r in result["rows"]],
            [
                ("err-ext", "error", "critical"),
                ("err", "error", "critical"),
                ("stuck", "stuck", "warning"),
                ("old", "unattached", "info"),
            ],
        )
        self.assertEqual(
            result["counts"], {"critical": 2, "warning": 1, "info": 1}
        )
        self.assertEqual(result["volumes_attention"], 3)
        self.assertEqual(result["volumes_total"], 8)
        self.assertEqual(
            result["unattached_soon"], [{"name": "recent", "days": 3.0}]
        )
        row = result["rows"][0]
        self.assertEqual(row["backend"], "host@lvm")
        self.assertEqual(row["for_seconds"], 7200)

    def test_snapshots(self):
        volumes = [_volume("v1", "in-use", attached=True)]
        snapshots = [
            _snapshot("old", "v1", 100 * DAY),
            _snapshot("new", "v1", 1 * DAY),
            _snapshot("old-alone", "v2", 200 * DAY),
            _snapshot("broken", "v1", DAY, status="error_deleting"),
            _snapshot("stuck", "v1", DAY, status="creating"),
        ]
        result = self.summarize(volumes, snapshots)
        self.assertEqual(
            [(r["id"], r["rule"]) for r in result["rows"]],
            [("broken", "error"), ("stuck", "stuck"), ("old", "old_snapshot")],
        )
        self.assertEqual(result["rows"][0]["backend"], "host@lvm")
        self.assertEqual(result["rows"][2]["backend"], "host@lvm")
        self.assertEqual(result["rows"][2]["for_seconds"], 100 * DAY)
        self.assertEqual(result["snapshots_total"], 5)

    def test_rows_are_bounded(self):
        volumes = [_volume(f"v{i}", "error") for i in range(600)]
        result = self.summarize(volumes)
        self.assertEqual(
            len(result["rows"]), storage_health.ATTENTION_STORE_LIMIT
        )
        self.assertEqual(storage_health.ATTENTION_STORE_LIMIT, 500)
        # counts stay exact
        self.assertEqual(result["total"], 600)
        self.assertEqual(result["counts"]["critical"], 600)
        result = storage_health.summarize_inventory(
            volumes, [], THRESHOLDS, NOW, limit=3
        )
        self.assertEqual(len(result["rows"]), 3)

    def test_missing_times(self):
        volumes = [
            _volume("v1", "creating", created=None, updated=None),
            _volume("v2", "available", created=None, updated=None),
        ]
        result = self.summarize(volumes)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["unattached_soon"], [])


class SummarizeTests(base.TestCase):
    def summarize(self, pools, inventory=None, predictions=None):
        rows = storage_health.evaluate_pools(pools, predictions, THRESHOLDS)
        return storage_health.summarize(rows, inventory, THRESHOLDS)

    def inventory(self, volumes):
        return storage_health.summarize_inventory(volumes, [], THRESHOLDS, NOW)

    def test_healthy(self):
        summary = self.summarize(
            [_pool()], self.inventory([]), [_prediction(enough=False)]
        )
        self.assertEqual(summary["verdict"]["key"], "healthy")
        self.assertEqual(summary["verdict"]["level"], "ok")
        self.assertEqual(summary["allocated_level"], "ok")
        self.assertEqual(summary["forecast_history_days"], 2.0)
        self.assertEqual(summary["total"], 100.0)
        self.assertEqual(summary["virtual_percent"], 1.0)

    def test_order(self):
        full = _pool(name="full", allocated=95.0)
        stale = _pool(name="stale", stale=True)
        filling = _pool(name="filling", allocated=80.0)
        errors = self.inventory([_volume("e", "error")])
        stuck = self.inventory([_volume("s", "deleting", age=DAY)])
        cases = [
            ([full, stale, filling], errors, "pools_full"),
            ([stale, filling], errors, "pools_stale"),
            ([filling], errors, "volumes_error"),
            ([filling], stuck, "pools_filling"),
            ([_pool()], stuck, "volumes_stuck"),
            ([_pool()], None, "healthy"),
        ]
        for pools, inventory, key in cases:
            summary = self.summarize(pools, inventory)
            self.assertEqual(summary["verdict"]["key"], key, key)
        summary = self.summarize([full, stale, filling], errors)
        self.assertEqual(summary["verdict"]["names"], ["full"])
        self.assertEqual(summary["pools_stale"], 1)
        # cloud-wide: 185 of 300 GiB allocated
        self.assertEqual(summary["allocated_level"], "ok")
        summary = self.summarize([full, filling])
        self.assertEqual(summary["allocated_level"], "warning")

    def test_names_are_bounded(self):
        pools = [_pool(name=f"p{i}", allocated=99.0) for i in range(8)]
        verdict = self.summarize(pools)["verdict"]
        self.assertEqual(len(verdict["names"]), storage_health.NAME_LIMIT)
        self.assertEqual(verdict["count"], 8)

    def test_no_pools(self):
        summary = self.summarize([])
        self.assertIsNone(summary["physical_percent"])
        self.assertEqual(summary["verdict"]["key"], "healthy")


class HelpersTests(base.TestCase):
    def test_backend_of(self):
        self.assertEqual(storage_health.backend_of("h@b#p"), "h@b")
        self.assertEqual(storage_health.backend_of("h@b"), "h@b")
        self.assertIsNone(storage_health.backend_of(None))

    def test_format_duration(self):
        self.assertEqual(storage_health.format_duration(None), "")
        self.assertEqual(storage_health.format_duration(10), "1 min")
        self.assertEqual(storage_health.format_duration(47 * 60), "47 min")
        self.assertEqual(storage_health.format_duration(3 * 3600), "3 h")
        self.assertEqual(storage_health.format_duration(3 * DAY), "3 d")


class InventoryTests(base.TestCase):
    def test_compact_volume(self):
        volume = types.SimpleNamespace(
            id="v1",
            name="data",
            status="available",
            size=10,
            attachments=[],
            created_at="2026-11-17T14:16:46.000000",
            updated_at="2026-11-17T14:16:47.000000",
        )
        setattr(volume, "os-vol-tenant-attr:tenant_id", "p1")
        setattr(volume, "os-vol-host-attr:host", "h@lvm#lvm")
        compact = storage_inventory.compact_volume(volume)
        self.assertEqual(compact["project_id"], "p1")
        self.assertEqual(compact["host"], "h@lvm#lvm")
        self.assertFalse(compact["attached"])
        self.assertEqual(compact["updated"], 1794925007.0)
        self.assertEqual(compact["created"], 1794925006.0)

    def test_compact_snapshot(self):
        snapshot = types.SimpleNamespace(
            id="s1",
            name=None,
            status="available",
            size=1,
            volume_id="v1",
            created_at="2026-11-17T14:17:10.000000",
            updated_at=None,
        )
        setattr(snapshot, "os-extended-snapshot-attributes:project_id", "p1")
        compact = storage_inventory.compact_snapshot(snapshot)
        self.assertEqual(compact["name"], "")
        self.assertEqual(compact["project_id"], "p1")
        self.assertIsNone(compact["updated"])

    def test_bad_timestamp(self):
        self.assertIsNone(storage_inventory._epoch("yesterday"))
        self.assertIsNone(storage_inventory._epoch(None))

    def test_fake_inventory_is_used_with_the_fake_datasource(self):
        inventory = storage_inventory.get_inventory(None)
        self.assertIsInstance(
            inventory, storage_inventory.FakeStorageInventory
        )
        volumes = inventory.list_volumes()
        ids = {v["id"] for v in volumes}
        for snapshot in inventory.list_snapshots():
            self.assertIn(snapshot["volume_id"], ids)
        names = inventory.project_names()
        for volume in volumes:
            self.assertIn(volume["project_id"], names)

    @override_settings(GRIAN_PLUGIN={"datasource": "prometheus"})
    def test_cinder_inventory_with_prometheus(self):
        inventory = storage_inventory.get_inventory("request")
        self.assertIsInstance(
            inventory, storage_inventory.CinderStorageInventory
        )
