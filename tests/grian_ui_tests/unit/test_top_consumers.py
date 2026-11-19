#
# SPDX-License-Identifier: Apache-2.0

from django.test import override_settings

from grian_ui import config
from grian_ui import timeseries
from grian_ui import top_consumers as tc
from grian_ui.datasource import fake
from grian_ui.datasource import fake_top_consumers
from grian_ui_tests.unit import base


THRESHOLDS = {"usage_idle": 10, "usage_busy": 80}


def _project(pid, **values):
    row = {f: None for f in tc.SNAPSHOT_FIELDS}
    row.update(values, id=pid)
    return row


SNAPSHOT = {
    "projects": [
        _project(
            "p-admin",
            instances=2,
            cpu=3.55,
            vcpus=5,
            memory=87.3,
            memory_alloc=512,
            disk_read=0,
            disk_write=0,
            net_in=10,
            net_out=3,
        ),
        _project(
            "p-demo",
            instances=2,
            cpu=4.22,
            vcpus=5,
            memory=92,
            memory_alloc=768,
            disk_read=None,
            disk_write=9e8,
            net_in=10,
            net_out=3,
        ),
        # running, but no CPU sample yet
        _project("p-new", instances=1, vcpus=1, memory_alloc=256),
    ]
}
NAMES = {"p-admin": "admin", "p-demo": "demo", "p-idle": "idle", "p-x": "x"}


class ResourceValueTests(base.TestCase):
    def test_sums_the_fields_of_a_resource(self):
        row = SNAPSHOT["projects"][1]
        self.assertEqual(tc.resource_value(row, tc.RESOURCE_DISK), 9e8)
        self.assertEqual(tc.resource_value(row, tc.RESOURCE_NETWORK), 13)
        self.assertEqual(tc.resource_value(row, tc.RESOURCE_CPU), 4.22)

    def test_none_when_nothing_reported(self):
        row = SNAPSHOT["projects"][2]
        self.assertIsNone(tc.resource_value(row, tc.RESOURCE_CPU))
        self.assertIsNone(tc.resource_value(row, tc.RESOURCE_DISK))


class TotalsTests(base.TestCase):
    def test_totals(self):
        totals = tc.totals(SNAPSHOT)
        self.assertEqual(totals["projects"], 3)
        self.assertEqual(totals["instances"], 5)
        self.assertAlmostEqual(totals["cpu"], 7.77)
        self.assertEqual(totals["vcpus"], 11)
        self.assertEqual(totals["memory_alloc"], 1536)
        self.assertEqual(totals["disk"], 9e8)
        self.assertEqual(totals["network"], 26)

    def test_empty(self):
        totals = tc.totals({"projects": []})
        self.assertEqual(totals["projects"], 0)
        self.assertIsNone(totals["cpu"])
        self.assertIsNone(totals["instances"])


class RatioTests(base.TestCase):
    def test_ratio(self):
        self.assertEqual(tc.ratio_percent(4, 5), 80.0)
        self.assertIsNone(tc.ratio_percent(None, 5))
        self.assertIsNone(tc.ratio_percent(1, 0))
        self.assertIsNone(tc.ratio_percent(1, None))

    def test_levels(self):
        self.assertEqual(tc.usage_level(80, THRESHOLDS), tc.LEVEL_BUSY)
        self.assertEqual(tc.usage_level(79.4, THRESHOLDS), "")
        self.assertEqual(tc.usage_level(10, THRESHOLDS), "")
        self.assertEqual(tc.usage_level(9.4, THRESHOLDS), tc.LEVEL_IDLE)
        self.assertEqual(tc.usage_level(None, THRESHOLDS), "")

    def test_levels_follow_the_shown_value(self):
        # shown as "80%": busy at an 80% threshold
        self.assertEqual(tc.percent_shown(79.8), 80)
        self.assertEqual(tc.usage_level(79.8, THRESHOLDS), tc.LEVEL_BUSY)
        self.assertEqual(tc.percent_shown(79.5), 80)
        self.assertEqual(tc.usage_level(79.5, THRESHOLDS), tc.LEVEL_BUSY)
        self.assertEqual(tc.percent_shown(79.49), 79)
        self.assertEqual(tc.usage_level(79.49, THRESHOLDS), "")
        # shown as "10%": not idle at a 10% threshold
        self.assertEqual(tc.percent_shown(9.6), 10)
        self.assertEqual(tc.usage_level(9.6, THRESHOLDS), "")
        self.assertEqual(tc.percent_shown(9.5), 10)
        self.assertEqual(tc.usage_level(9.5, THRESHOLDS), "")
        self.assertEqual(tc.percent_shown(9.49), 9)
        self.assertEqual(tc.usage_level(9.49, THRESHOLDS), tc.LEVEL_IDLE)
        # half up like floatformat:0, not banker's rounding
        self.assertEqual(tc.percent_shown(0.5), 1)
        self.assertEqual(tc.percent_shown(2.5), 3)
        self.assertIsNone(tc.percent_shown(None))

    def test_rows_show_the_value_the_level_uses(self):
        snapshot = {"projects": [_project("p", cpu=3.99, vcpus=5)]}
        rows, _count, _total = tc.top_projects(
            snapshot, tc.RESOURCE_CPU, {}, THRESHOLDS
        )
        self.assertEqual(rows[0]["ratio_shown"], 80)
        self.assertEqual(rows[0]["level"], tc.LEVEL_BUSY)


class TrendWindowTests(base.TestCase):
    def test_windows(self):
        self.assertEqual(tc.trend_window("1h"), ("1h", "1m", "10m"))
        self.assertEqual(tc.trend_window("6h"), ("6h", "5m", "10m"))
        self.assertEqual(tc.trend_window("24h"), ("24h", "15m", "15m"))
        self.assertEqual(tc.trend_window("7d"), ("7d", "1h", "1h"))

    def test_ranges(self):
        self.assertEqual(tc.TREND_RANGES, ("1h", "6h", "24h", "7d"))
        self.assertEqual(tc.DEFAULT_TREND_RANGE, "24h")
        # every range keeps the chart bounded
        for key in tc.TREND_RANGES:
            self.assertLessEqual(len(timeseries.range_grid(key)), 170)


class TopProjectsTests(base.TestCase):
    def test_sorted_with_share_and_ratio(self):
        rows, count, total = tc.top_projects(
            SNAPSHOT, tc.RESOURCE_CPU, NAMES, THRESHOLDS
        )
        self.assertEqual(count, 3)
        self.assertAlmostEqual(total, 7.77)
        self.assertEqual(
            [r["label"] for r in rows], ["demo", "admin", "p-new"]
        )
        demo = rows[0]
        self.assertAlmostEqual(demo["share"], 100 * 4.22 / 7.77)
        self.assertAlmostEqual(demo["ratio"], 84.4)
        self.assertEqual(demo["level"], tc.LEVEL_BUSY)
        self.assertEqual(demo["value_text"], "4.22 cores")
        self.assertEqual(demo["allocated_text"], "5 vCPUs")
        self.assertEqual(demo["instances"], 2)
        # no sample: shown as missing, sorted last, no share
        self.assertIsNone(rows[2]["value"])
        self.assertEqual(rows[2]["share"], 0.0)
        self.assertEqual(rows[2]["value_text"], "–")
        # unknown project name: the ID
        self.assertIsNone(rows[2]["name"])

    def test_resources_without_allocation(self):
        rows, _count, _total = tc.top_projects(
            SNAPSHOT, tc.RESOURCE_DISK, NAMES, THRESHOLDS
        )
        self.assertEqual(rows[0]["label"], "demo")
        self.assertIsNone(rows[0]["allocated"])
        self.assertIsNone(rows[0]["ratio"])
        self.assertEqual(rows[0]["share"], 100.0)

    def test_memory(self):
        rows, _count, _total = tc.top_projects(
            SNAPSHOT, tc.RESOURCE_MEMORY, NAMES, THRESHOLDS
        )
        self.assertEqual(rows[0]["value_text"], "92 MiB")
        self.assertEqual(rows[0]["allocated_text"], "768 MiB")
        self.assertEqual(rows[0]["level"], "")

    def test_limit_and_stable_ties(self):
        snapshot = {
            "projects": [
                _project(f"p{i:02d}", cpu=1.0) for i in reversed(range(15))
            ]
        }
        rows, count, _total = tc.top_projects(
            snapshot, tc.RESOURCE_CPU, {}, THRESHOLDS, limit=10
        )
        self.assertEqual(count, 15)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["label"], "p00")


class InstanceRowsTests(base.TestCase):
    def test_rows(self):
        instances = [
            {
                "id": "i-1",
                "name": "vm-1",
                "project_id": "p-admin",
                "value": 1.0,
                "vcpus": 4,
                "memory_alloc": 256,
            },
            {
                "id": "i-2",
                "name": None,
                "project_id": "p-gone",
                "value": 3.0,
                "vcpus": 1,
                "memory_alloc": None,
            },
        ]
        rows = tc.instance_rows(
            instances, tc.RESOURCE_CPU, 8.0, NAMES, THRESHOLDS
        )
        self.assertEqual([r["label"] for r in rows], ["i-2", "vm-1"])
        self.assertEqual(rows[0]["project_label"], "p-gone")
        self.assertEqual(rows[1]["project_label"], "admin")
        self.assertEqual(rows[0]["share"], 37.5)
        self.assertEqual(rows[0]["ratio"], 300.0)
        self.assertEqual(rows[1]["level"], "")

        rows = tc.instance_rows(
            instances, tc.RESOURCE_MEMORY, None, NAMES, THRESHOLDS
        )
        self.assertEqual(rows[0]["share"], 0.0)


class IdleProjectsTests(base.TestCase):
    def test_idle_projects_by_name(self):
        names, count = tc.idle_projects(SNAPSHOT, NAMES)
        self.assertEqual(names, ["idle", "x"])
        self.assertEqual(count, 2)

    def test_bounded(self):
        names = {f"p{i}": f"name-{i:02d}" for i in range(25)}
        shown, count = tc.idle_projects({"projects": []}, names, limit=10)
        self.assertEqual(len(shown), 10)
        self.assertEqual(count, 25)
        self.assertEqual(shown[0], "name-00")


class FindProjectTests(base.TestCase):
    def test_find(self):
        self.assertEqual(tc.find_project(SNAPSHOT, "p-demo")["cpu"], 4.22)
        self.assertIsNone(tc.find_project(SNAPSHOT, "nope"))


class StackWithOtherTests(base.TestCase):
    def test_other_is_total_minus_the_drawn_series(self):
        series = {"a": [5, 5, None], "b": [2, None, 1]}
        stacked = tc.stack_with_other(series, [10, 8, None], ["a", "b"])
        self.assertEqual(
            stacked,
            [
                ("a", [5, 5, None]),
                ("b", [2, None, 1]),
                (tc.OTHER_KEY, [3, 3, None]),
            ],
        )

    def test_no_other_when_the_series_are_everything(self):
        stacked = tc.stack_with_other({"a": [1.0, 2.0]}, [1.0, 2.0], ["a"])
        self.assertEqual(stacked, [("a", [1.0, 2.0])])

    def test_rounding_and_negative_rest_are_zero(self):
        stacked = tc.stack_with_other(
            {"a": [1.0000000001, 3.0]}, [1.0, 2.0], ["a"]
        )
        self.assertEqual(stacked, [("a", [1.0000000001, 3.0])])

    def test_missing_key_is_all_none(self):
        stacked = tc.stack_with_other({}, [1.0, 2.0], ["a"])
        self.assertEqual(
            stacked, [("a", [None, None]), (tc.OTHER_KEY, [1.0, 2.0])]
        )


class TrendSummaryTests(base.TestCase):
    def test_summary(self):
        self.assertEqual(
            tc.trend_summary([None, 2, 1, 3, None]),
            {"latest": 3, "min": 1, "max": 3},
        )
        self.assertIsNone(tc.trend_summary([None, None]))


class FormatTests(base.TestCase):
    def test_bytes(self):
        self.assertEqual(tc.format_bytes(0), "0 B")
        self.assertEqual(tc.format_bytes(1023), "1023 B")
        self.assertEqual(tc.format_bytes(1536), "1.5 KiB")
        self.assertEqual(tc.format_bytes(10.2 * 1024**2), "10.2 MiB")
        self.assertEqual(tc.format_bytes(150 * 1024**3), "150 GiB")
        self.assertEqual(tc.format_bytes(None), "–")
        self.assertEqual(tc.format_bytes(float("nan")), "–")

    def test_values(self):
        self.assertEqual(
            tc.format_value(tc.RESOURCE_CPU, 3.5496), "3.55 cores"
        )
        self.assertEqual(tc.format_value(tc.RESOURCE_CPU, 180), "180 cores")
        self.assertEqual(tc.format_value(tc.RESOURCE_CPU, 10.25), "10.2 cores")
        self.assertEqual(tc.format_value(tc.RESOURCE_MEMORY, 1536), "1.5 GiB")
        self.assertEqual(tc.format_value(tc.RESOURCE_DISK, 2048), "2 KiB/s")
        self.assertEqual(tc.format_value(tc.RESOURCE_NETWORK, 13), "13 B/s")
        self.assertEqual(tc.format_value(tc.RESOURCE_NETWORK, None), "–")

    def test_allocated(self):
        self.assertEqual(tc.format_allocated(tc.RESOURCE_CPU, 5.0), "5 vCPUs")
        self.assertEqual(
            tc.format_allocated(tc.RESOURCE_MEMORY, 512), "512 MiB"
        )
        self.assertEqual(tc.format_allocated(tc.RESOURCE_DISK, 5), "–")
        self.assertEqual(tc.format_allocated(tc.RESOURCE_CPU, None), "–")


class ThresholdsConfigTests(base.TestCase):
    def test_defaults(self):
        self.assertEqual(
            config.get_top_consumers_thresholds(),
            {"usage_idle": 10, "usage_busy": 80},
        )

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "top_consumers_thresholds": {"usage_busy": 90},
        }
    )
    def test_override(self):
        self.assertEqual(
            config.get_top_consumers_thresholds()["usage_busy"], 90
        )

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "top_consumers_thresholds": {"usage_idle": 50, "usage_busy": 40},
        }
    )
    def test_idle_above_busy(self):
        with self.assertRaisesRegex(config.ConfigurationError, "usage_idle"):
            config.get_top_consumers_thresholds()

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "top_consumers_thresholds": {"busy": 40},
        }
    )
    def test_unknown_key(self):
        with self.assertRaises(config.ConfigurationError):
            config.get_top_consumers_thresholds()

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "top_consumers_thresholds": {"usage_busy": -1},
        }
    )
    def test_negative(self):
        with self.assertRaises(config.ConfigurationError):
            config.get_top_consumers_thresholds()


class FakeTopConsumersTests(base.TestCase):
    def test_trend_follows_the_range(self):
        trend = fake.get_client().get_top_consumers_trend(
            tc.RESOURCE_CPU,
            tc.BY_PROJECT,
            [],
            range_key="7d",
            at=1_800_000_000,
        )
        self.assertEqual(
            [p[0] for p in trend["total"]],
            timeseries.range_grid("7d", 1_800_000_000),
        )

    def setUp(self):
        super().setUp()
        self.client = fake.get_client()

    def test_projects_agree_with_instances(self):
        snapshot = self.client.get_top_consumers_projects()
        self.assertEqual(len(snapshot["projects"]), 12)
        for project in snapshot["projects"]:
            instances = self.client.get_top_consumers_instances(
                tc.RESOURCE_CPU, project_id=project["id"], limit=1000
            )
            self.assertEqual(len(instances), project["instances"])
            self.assertAlmostEqual(
                sum(i["value"] for i in instances), project["cpu"]
            )

    def test_names_include_idle_projects(self):
        snapshot = self.client.get_top_consumers_projects()
        names = fake_top_consumers.FAKE_PROJECT_NAMES
        idle, count = tc.idle_projects(snapshot, names)
        self.assertEqual(idle, ["decommissioned", "service"])
        self.assertEqual(count, 2)

    def test_instances_ranked_and_bounded(self):
        rows = self.client.get_top_consumers_instances(tc.RESOURCE_DISK)
        self.assertEqual(len(rows), tc.TOP_LIMIT)
        values = [r["value"] for r in rows]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertTrue(rows[0]["name"].startswith("backup-"))

    def test_trend_total_is_the_sum_of_every_project(self):
        snapshot = self.client.get_top_consumers_projects()
        keys = [p["id"] for p in snapshot["projects"]]
        trend = self.client.get_top_consumers_trend(
            tc.RESOURCE_CPU, tc.BY_PROJECT, keys, at=1_800_000_000
        )
        grid = timeseries.range_grid(tc.DEFAULT_TREND_RANGE, 1_800_000_000)
        self.assertEqual([p[0] for p in trend["total"]], grid)
        for i in (0, len(grid) - 1):
            self.assertAlmostEqual(
                sum(s[i][1] for s in trend["series"].values()),
                trend["total"][i][1],
            )

    def test_trend_by_instance_of_a_project(self):
        project = self.client.get_top_consumers_projects()["projects"][0]
        rows = self.client.get_top_consumers_instances(
            tc.RESOURCE_MEMORY, project_id=project["id"], limit=2
        )
        trend = self.client.get_top_consumers_trend(
            tc.RESOURCE_MEMORY,
            tc.BY_INSTANCE,
            [r["id"] for r in rows],
            project_id=project["id"],
        )
        self.assertEqual(set(trend["series"]), {r["id"] for r in rows})
