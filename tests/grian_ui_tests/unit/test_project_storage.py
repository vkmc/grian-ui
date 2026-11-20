#
# SPDX-License-Identifier: Apache-2.0

from unittest import mock

from grian_ui import config
from grian_ui import project_storage as ps
from grian_ui_tests.unit import base


DAY = ps.DAY_SECONDS
NOW = 1_795_000_000.0
T = dict(config.DEFAULT_PROJECT_STORAGE_THRESHOLDS)


def _volume(name, size, status="available", servers=(), age=10, idle=None):
    return {
        "id": f"vol-{name}",
        "name": name,
        "size": size,
        "status": status,
        "server_ids": list(servers),
        "created": NOW - age * DAY,
        "updated": NOW - (age if idle is None else idle) * DAY,
        "image_id": None,
    }


def _snapshot(name, size, volume, age):
    return {
        "id": f"snap-{name}",
        "name": name,
        "size": size,
        "status": "available",
        "volume_id": f"vol-{volume}",
        "created": NOW - age * DAY,
    }


def _image(name, gb, age, visibility="private"):
    return {
        "id": f"img-{name}",
        "name": name,
        "size": int(gb * ps.GIB),
        "created": NOW - age * DAY,
        "visibility": visibility,
    }


class ThresholdsConfigTests(base.TestCase):
    @mock.patch("grian_ui.config.settings")
    def test_defaults_are_the_mockup_values(self, mock_settings):
        mock_settings.GRIAN_PLUGIN = {"datasource": "fake"}
        self.assertEqual(
            config.get_project_storage_thresholds(),
            {
                "quota_warning": 75,
                "quota_critical": 90,
                "unattached_days": 7,
                "unattached_review_days": 30,
                "snapshot_age_days": 30,
                "image_age_days": 30,
                "forecast_critical_days": 14,
                "forecast_warning_days": 30,
            },
        )

    @mock.patch("grian_ui.config.settings")
    def test_override(self, mock_settings):
        mock_settings.GRIAN_PLUGIN = {
            "project_storage_thresholds": {"unattached_days": 14}
        }
        self.assertEqual(
            config.get_project_storage_thresholds()["unattached_days"], 14
        )

    @mock.patch("grian_ui.config.settings")
    def test_invalid(self, mock_settings):
        for overrides, message in (
            ({"nope": 1}, "Unknown key"),
            ({"quota_warning": 95}, "must not be greater"),
            ({"unattached_days": 40}, r"\['unattached_days'\] must not be"),
            ({"forecast_critical_days": 60}, r"\['forecast_critical_days'\]"),
            ({"image_age_days": -1}, "must be a number"),
        ):
            mock_settings.GRIAN_PLUGIN = {
                "project_storage_thresholds": overrides
            }
            with self.subTest(overrides=overrides):
                self.assertRaisesRegex(
                    config.ConfigurationError,
                    message,
                    config.get_project_storage_thresholds,
                )


class RangeTests(base.TestCase):
    def test_footprint_range(self):
        self.assertEqual(ps.footprint_range("7d"), ("7d", 7))
        self.assertEqual(ps.footprint_range("90d"), ("90d", 90))
        self.assertEqual(ps.footprint_range("1y"), ("30d", 30))
        self.assertEqual(ps.footprint_range(None), ("30d", 30))

    def test_history_days_never_beyond_retention(self):
        self.assertEqual(ps.history_days(30, 15), 15)
        self.assertEqual(ps.history_days(7, 15), 7)
        self.assertEqual(ps.history_days(90, 400), 90)

    def test_day_grid_ends_at_the_last_midnight(self):
        grid = ps.day_grid(3, now=10 * DAY + 3600)
        self.assertEqual(grid, [8 * DAY, 9 * DAY, 10 * DAY])
        self.assertEqual(ps.day_grid(1, now=10 * DAY), [10 * DAY])


class FootprintChartTests(base.TestCase):
    def test_stacked_series_with_gaps(self):
        now = 100 * DAY + 600
        data = {
            "volumes": [[98 * DAY, 10.0], [99 * DAY, 12.0], [100 * DAY, 12.0]],
            # snapshots only from the last day
            "snapshots": [[100 * DAY + 0.4, 5.0]],
        }
        chart = ps.footprint_chart(data, "7d", 15, now)
        self.assertEqual(chart["range"], "7d")
        self.assertEqual(len(chart["timestamps"]), 7)
        self.assertEqual(chart["timestamps"][-1], 100 * DAY)
        self.assertEqual(chart["volumes"], [None] * 4 + [10.0, 12.0, 12.0])
        # a day with volumes but no snapshots counts the snapshots as 0
        self.assertEqual(chart["snapshots"], [None] * 4 + [0, 0, 5.0])
        self.assertEqual(chart["first_ts"], 98 * DAY)
        self.assertTrue(chart["has_data"])
        self.assertFalse(chart["beyond_retention"])
        self.assertEqual(
            chart["latest"], {"volumes": 12.0, "snapshots": 5.0, "total": 17.0}
        )

    def test_beyond_retention(self):
        chart = ps.footprint_chart({}, "90d", 15, NOW)
        self.assertTrue(chart["beyond_retention"])
        self.assertEqual(chart["history_days"], 15)
        self.assertEqual(len(chart["timestamps"]), 90)
        self.assertFalse(chart["has_data"])
        self.assertIsNone(chart["first_ts"])
        self.assertIsNone(chart["latest"])

    def test_invalid_range_falls_back(self):
        chart = ps.footprint_chart({}, "bogus", 15, NOW)
        self.assertEqual(chart["range"], "30d")
        self.assertEqual(len(chart["timestamps"]), 30)


class QuotaTests(base.TestCase):
    def test_percent(self):
        self.assertEqual(ps.quota_percent(840, 1000), 84)
        self.assertEqual(ps.quota_percent(0, 1000), 0)
        self.assertIsNone(ps.quota_percent(5, None))
        self.assertIsNone(ps.quota_percent(5, float("inf")))
        # a zero quota is full
        self.assertEqual(ps.quota_percent(0, 0), 100)

    def test_level(self):
        self.assertEqual(ps.quota_level(74, T), ps.LEVEL_OK)
        self.assertEqual(ps.quota_level(75, T), ps.LEVEL_WARNING)
        self.assertEqual(ps.quota_level(90, T), ps.LEVEL_CRITICAL)
        self.assertEqual(ps.quota_level(None, T), ps.LEVEL_OK)

    def block(self, **limits):
        return {
            "limits": dict(
                {
                    "gigabytes": 1000,
                    "volumes": 10,
                    "snapshots": 10,
                    "backups": 10,
                    "backups_used": 2,
                },
                **limits,
            ),
            "volumes": [
                _volume("a", 100, "in-use", ["s1"]),
                _volume("b", 300),
                _volume("err", 20, "error"),
            ],
            "snapshots": [_snapshot("s", 50, "a", 5)],
            "backup_enabled": False,
        }

    def test_usage(self):
        use = ps.usage(self.block())
        self.assertEqual(use["attached_gb"], 100)
        # error volumes hold quota too, shown as unattached
        self.assertEqual(use["unattached_gb"], 320)
        self.assertEqual(use["snapshots_gb"], 50)
        self.assertEqual(use["gigabytes"], 470)
        self.assertEqual((use["attached"], use["unattached"]), (1, 2))

    def test_rows(self):
        rows = {r["key"]: r for r in ps.quota_rows(self.block(), T)}
        self.assertEqual(
            list(rows), ["gigabytes", "volumes", "snapshots", "backups"]
        )
        gb = rows["gigabytes"]
        self.assertEqual(
            (gb["used"], gb["limit"], gb["percent"]), (470, 1000, 47)
        )
        self.assertEqual(gb["unit"], "GB")
        self.assertEqual(
            [(s["key"], s["width"]) for s in gb["segments"]],
            [("attached", 10.0), ("unattached", 32.0), ("snapshots", 5.0)],
        )
        self.assertEqual(rows["volumes"]["used"], 3)
        self.assertEqual(rows["snapshots"]["percent"], 10)
        # backups are not counted without a backup service
        self.assertFalse(rows["backups"]["enabled"])
        self.assertEqual(rows["backups"]["used"], 0)

    def test_backups_enabled(self):
        block = dict(self.block(), backup_enabled=True)
        backups = ps.quota_rows(block, T)[3]
        self.assertTrue(backups["enabled"])
        self.assertEqual(backups["used"], 2)

    def test_unlimited_and_over_quota(self):
        rows = ps.quota_rows(self.block(gigabytes=None, volumes=2), T)
        self.assertIsNone(rows[0]["percent"])
        self.assertEqual(rows[0]["level"], ps.LEVEL_OK)
        self.assertTrue(all(s["width"] == 0 for s in rows[0]["segments"]))
        # over quota (after a quota decrease): the bar is full, not wider
        self.assertEqual(rows[1]["percent"], 150)
        self.assertEqual(rows[1]["level"], ps.LEVEL_CRITICAL)
        self.assertEqual(sum(s["width"] for s in rows[1]["segments"]), 100)

    def test_empty_segments_are_dropped(self):
        block = {"limits": {"gigabytes": 1000}, "volumes": [], "snapshots": []}
        rows = ps.quota_rows(block, T)
        self.assertEqual(rows[0]["segments"], [])
        self.assertEqual(rows[0]["percent"], 0)


class ForecastTests(base.TestCase):
    def points(self, start, per_day, days=10):
        return [
            [NOW - (days - i) * DAY, start + per_day * i]
            for i in range(days + 1)
        ]

    def test_slope(self):
        self.assertAlmostEqual(ps.slope_per_day(self.points(0, 3.5)), 3.5)
        self.assertIsNone(ps.slope_per_day([[1, 2]]))
        self.assertIsNone(ps.slope_per_day([[1, 2], [1, 3]]))
        self.assertIsNone(ps.slope_per_day([]))

    def test_days(self):
        fc = ps.forecast(self.points(500, 10), 800, 1000, T)
        self.assertEqual(fc["state"], ps.FORECAST_DAYS)
        self.assertEqual(fc["days"], 20)
        self.assertEqual(fc["level"], ps.LEVEL_WARNING)
        self.assertEqual(fc["history_days"], 10)

    def test_levels(self):
        fc = ps.forecast(self.points(0, 10), 900, 1000, T)
        self.assertEqual((fc["days"], fc["level"]), (10, ps.LEVEL_CRITICAL))
        fc = ps.forecast(self.points(0, 1), 900, 1000, T)
        self.assertEqual((fc["days"], fc["level"]), (100, ps.LEVEL_OK))

    def test_flat_or_falling(self):
        self.assertEqual(
            ps.forecast(self.points(5, 0), 5, 10, T)["state"], ps.FORECAST_FLAT
        )
        self.assertEqual(
            ps.forecast(self.points(9, -1), 5, 10, T)["state"],
            ps.FORECAST_FLAT,
        )

    def test_far(self):
        fc = ps.forecast(self.points(0, 0.1), 10, 1000, T)
        self.assertEqual(fc["state"], ps.FORECAST_FAR)

    def test_not_enough_history(self):
        fc = ps.forecast(self.points(0, 10, days=2), 10, 1000, T)
        self.assertEqual(fc["state"], ps.FORECAST_NO_HISTORY)
        self.assertEqual(fc["history_days"], 2)
        self.assertEqual(
            ps.forecast([], 10, 1000, T)["state"], ps.FORECAST_NO_HISTORY
        )

    def test_full_and_unlimited(self):
        fc = ps.forecast([], 10, 10, T)
        self.assertEqual(
            (fc["state"], fc["level"]), (ps.FORECAST_FULL, ps.LEVEL_CRITICAL)
        )
        self.assertEqual(
            ps.forecast(self.points(0, 1), 10, None, T)["state"],
            ps.FORECAST_UNLIMITED,
        )

    def test_quota_forecasts(self):
        rows = ps.quota_rows(
            {
                "limits": {"gigabytes": 1000, "volumes": 10, "snapshots": 10},
                "volumes": [_volume("a", 500)],
                "snapshots": [],
            },
            T,
        )
        trend = {
            "gigabytes": self.points(400, 10),
            "volumes": self.points(1, 0),
        }
        forecasts = ps.quota_forecasts(rows, trend, T)
        self.assertEqual(
            sorted(forecasts), ["gigabytes", "snapshots", "volumes"]
        )
        self.assertEqual(forecasts["gigabytes"]["days"], 50)
        self.assertEqual(forecasts["volumes"]["state"], ps.FORECAST_FLAT)
        self.assertEqual(
            forecasts["snapshots"]["state"], ps.FORECAST_NO_HISTORY
        )


class CleanUpTests(base.TestCase):
    def candidates(
        self, volumes=(), snapshots=(), images=None, users=None, thresholds=T
    ):
        return ps.clean_up_candidates(
            list(volumes), list(snapshots), images, users, thresholds, NOW
        )

    def test_unattached_volumes(self):
        found = self.candidates(
            [
                _volume("new", 10, idle=6),
                _volume("week", 20, idle=7),
                _volume("month", 30, idle=30),
                _volume("used", 40, "in-use", ["s1"], idle=90),
                _volume("err", 50, "error", idle=90),
            ]
        )
        self.assertEqual(
            [(c["name"], c["severity"]) for c in found],
            [("month", ps.SEVERITY_REVIEW), ("week", ps.SEVERITY_CONSIDER)],
        )
        self.assertEqual(found[0]["reason"], "unattached for 30 days")
        self.assertTrue(
            all(c["in_quota"] and c["kind"] == "volume" for c in found)
        )

    def test_available_volume_with_attachment_is_not_a_candidate(self):
        # multi-attach volumes can report "available" while attached
        self.assertEqual(
            self.candidates([_volume("multi", 10, servers=["s1"], idle=60)]),
            [],
        )

    def test_unknown_update_time_uses_creation(self):
        volume = dict(_volume("v", 10, age=40), updated=None)
        self.assertEqual(
            self.candidates([volume])[0]["reason"], "unattached for 40 days"
        )
        volume = dict(volume, created=None)
        self.assertEqual(self.candidates([volume]), [])

    def test_snapshots(self):
        volumes = [
            _volume("db", 100, "in-use", ["s1"]),
            _volume("web", 50, "in-use", ["s2"]),
        ]
        snapshots = [
            _snapshot("db-new", 100, "db", 1),
            _snapshot("db-old", 100, "db", 78),
            _snapshot("db-young", 100, "db", 29),
            _snapshot("web-base", 50, "web", 120),
        ]
        found = {c["name"]: c for c in self.candidates(volumes, snapshots)}
        self.assertEqual(sorted(found), ["db-old", "web-base"])
        self.assertEqual(found["db-old"]["severity"], ps.SEVERITY_CONSIDER)
        self.assertEqual(
            found["db-old"]["reason"],
            "78 days old; a newer snapshot of db exists",
        )
        self.assertEqual(found["web-base"]["severity"], ps.SEVERITY_KEEP)
        self.assertEqual(
            found["web-base"]["reason"],
            "120 days old; the only snapshot of web",
        )
        self.assertEqual(
            found["web-base"]["title"], "Only copy of this volume's data"
        )

    def test_newest_of_several_old_snapshots_is_kept(self):
        snapshots = [_snapshot("a", 10, "v", 40), _snapshot("b", 10, "v", 60)]
        found = {
            c["name"]: c
            for c in self.candidates([_volume("v", 10, "in-use")], snapshots)
        }
        self.assertEqual(found["a"]["severity"], ps.SEVERITY_KEEP)
        self.assertEqual(
            found["a"]["reason"], "40 days old; the newest snapshot of v"
        )
        self.assertEqual(found["b"]["severity"], ps.SEVERITY_CONSIDER)

    def test_images(self):
        images = [
            _image("unused-old", 2.0, 155),
            _image("unused-new", 3.0, 10),
            _image("used", 2.1, 40),
        ]
        found = self.candidates(images=images, users={"img-used": 6})
        self.assertEqual([c["name"] for c in found], ["unused-old"])
        self.assertEqual(found[0]["gb"], 2.0)
        self.assertFalse(found[0]["in_quota"])
        self.assertEqual(
            found[0]["reason"], "not used by any instance, 155 days old"
        )

    def test_shared_images_are_never_candidates(self):
        # other member projects may boot from them: their instances are
        # not visible, so "no instance uses it" cannot be known
        images = [
            _image("shared", 2.0, 155, "shared"),
            _image("community", 2.0, 155, "community"),
            _image("public", 2.0, 155, "public"),
            _image("unknown", 2.0, 155, None),
            _image("private", 2.0, 155),
        ]
        found = self.candidates(images=images, users={})
        self.assertEqual([c["name"] for c in found], ["private"])

    def test_images_not_checked_without_both_apis(self):
        images = [_image("old", 2.0, 155)]
        self.assertEqual(self.candidates(images=images, users=None), [])
        self.assertEqual(self.candidates(images=None, users={}), [])

    def test_thresholds(self):
        thresholds = dict(T, unattached_days=1, unattached_review_days=5)
        found = self.candidates(
            [_volume("v", 10, idle=5)], thresholds=thresholds
        )
        self.assertEqual(found[0]["severity"], ps.SEVERITY_REVIEW)

    def test_order_and_reclaimable(self):
        volumes = [
            _volume("big", 300, idle=41),
            _volume("small", 60, idle=12),
            _volume("db", 120, "in-use", ["s1"]),
            _volume("web", 50, "in-use", ["s2"]),
        ]
        snapshots = [
            _snapshot("db-new", 120, "db", 1),
            _snapshot("db-old", 120, "db", 78),
            _snapshot("web-base", 50, "web", 120),
        ]
        images = [_image("golden", 2.0, 155)]
        found = self.candidates(volumes, snapshots, images, {})
        self.assertEqual(
            [c["name"] for c in found],
            ["big", "db-old", "small", "golden", "web-base"],
        )
        # the mockup sample: 480 GB in 3 candidates (images and "keep?"
        # snapshots do not count)
        self.assertEqual(ps.reclaimable(found), (480, 3))
        self.assertEqual(ps.reclaimable([]), (0, 0))


class InventoryRowsTests(base.TestCase):
    def test_days_since(self):
        self.assertEqual(ps.days_since(NOW - 2.5 * DAY, NOW), 2)
        self.assertEqual(ps.days_since(NOW + DAY, NOW), 0)
        self.assertIsNone(ps.days_since(None, NOW))

    def test_display_name(self):
        self.assertEqual(ps.display_name({"name": "x", "id": "abc"}), "x")
        self.assertEqual(
            ps.display_name({"name": "", "id": "0123456789"}), "01234567"
        )

    def test_image_users(self):
        servers = [
            {"id": "s1", "name": "a", "image_id": "i1"},
            {"id": "s2", "name": "b", "image_id": "i1"},
            # booted from a volume created from i2
            {"id": "s3", "name": "c", "image_id": None},
        ]
        volumes = [
            dict(_volume("boot", 10, "in-use", ["s3"]), image_id="i2"),
            dict(_volume("data", 10, "in-use", ["s1"]), image_id="i1"),
        ]
        self.assertEqual(ps.image_users(servers, volumes), {"i1": 2, "i2": 1})
        self.assertEqual(ps.image_users([], []), {})

    def test_volume_rows(self):
        block = {
            "volumes": [
                _volume("small", 10, "in-use", ["s1", "s2"], age=5),
                _volume("big", 300, idle=41, age=95),
            ],
            "snapshots": [_snapshot("snap", 10, "small", 3)],
        }
        rows, total = ps.volume_rows(block, {"s1": "web-01"}, NOW)
        self.assertEqual(total, 3)
        self.assertEqual([r["name"] for r in rows], ["big", "small", "snap"])
        self.assertEqual(rows[0]["idle_days"], 41)
        self.assertEqual(rows[0]["age_days"], 95)
        self.assertFalse(rows[0]["attached"])
        self.assertEqual(
            rows[1]["servers"],
            [{"id": "s1", "name": "web-01"}, {"id": "s2", "name": "s2"}],
        )
        self.assertIsNone(rows[1]["idle_days"])
        self.assertEqual(rows[2]["volume_name"], "small")

    def test_volume_rows_are_bounded(self):
        block = {
            "volumes": [_volume(f"v{i}", i) for i in range(30)],
            "snapshots": [],
        }
        rows, total = ps.volume_rows(block, None, NOW)
        self.assertEqual((len(rows), total), (ps.VOLUMES_LIMIT, 30))
        self.assertEqual(rows[0]["name"], "v29")
        # without the Compute API, no names
        rows, _total = ps.volume_rows(
            {
                "volumes": [_volume("a", 1, "in-use", ["0123456789"])],
                "snapshots": [],
            },
            None,
            NOW,
        )
        self.assertEqual(rows[0]["servers"][0]["name"], "01234567")

    def test_image_rows(self):
        images = [_image("a", 2.1, 40), _image("b", 3.4, 70)]
        rows, total, total_gb = ps.image_rows(images, {"img-a": 6}, NOW)
        self.assertEqual([r["name"] for r in rows], ["b", "a"])
        self.assertEqual([r["users"] for r in rows], [0, 6])
        self.assertEqual((total, total_gb), (2, 5.5))
        rows, _t, _g = ps.image_rows(images, None, NOW)
        self.assertEqual([r["users"] for r in rows], [None, None])

    def test_image_rows_shared(self):
        images = [
            _image("a", 1, 40, "shared"),
            _image("b", 2, 40, "shared"),
            _image("c", 3, 40),
        ]
        rows, _t, _g = ps.image_rows(images, {"img-b": 1}, NOW)
        self.assertEqual(
            [(r["name"], r["users"], r["shared"]) for r in rows],
            [("c", 0, False), ("b", 1, True), ("a", 0, True)],
        )

    def test_image_rows_are_bounded(self):
        images = [_image(f"i{i}", 1, 1) for i in range(30)]
        rows, total, _gb = ps.image_rows(images, {}, NOW)
        self.assertEqual((len(rows), total), (ps.IMAGES_LIMIT, 30))
