#
# SPDX-License-Identifier: Apache-2.0

import datetime

from django.test import override_settings

from grian_ui import config
from grian_ui import project_usage as pu
from grian_ui_tests.unit import base


UTC = datetime.timezone.utc
# the real demo project snapshot: 2026-11-20 15:40 UTC
NOW = datetime.datetime(2026, 11, 20, 15, 40, tzinfo=UTC)
THRESHOLDS = config.DEFAULT_PROJECT_USAGE_THRESHOLDS
ID1 = "d414fbd9-66a6-485c-bec7-8191c2fa4221"
ID2 = "c726b640-2a18-41e3-9fac-af2730c12410"
ID3 = "6a0a1f43-6b0c-4f1e-9a43-3c2f1d1e8b11"


def _at(*args):
    return datetime.datetime(*args, tzinfo=UTC)


def _usage(**values):
    usage = dict.fromkeys(pu.SUM_FIELDS, 0.0)
    usage.update(name=None, vcpus=None, memory_mib=None)
    usage.update(values)
    return usage


class ParsePeriodTests(base.TestCase):
    def test_valid(self):
        self.assertEqual(pu.parse_period("2026-11", NOW), (2026, 11))
        self.assertEqual(pu.parse_period("2025-01", NOW), (2025, 1))

    def test_invalid_or_future(self):
        for key in (
            None,
            "",
            "2026-12",  # after the current month
            "2027-01",
            "2026-13",
            "2026-00",
            "2026-1",
            "2026/11",
            "26-11-x",
            "１２３４-11",  # non-ASCII digits
            "1969-12",
            ["2026-11"],
        ):
            with self.subTest(key=key):
                self.assertIsNone(pu.parse_period(key, NOW))


class PeriodInfoTests(base.TestCase):
    def test_current_month_beyond_retention(self):
        # 15 days kept: 11/5 15:40 onwards, so the first full day is 11/6
        p = pu.period_info(2026, 11, NOW, 15)
        self.assertTrue(p["is_current"])
        self.assertTrue(p["available"])
        self.assertFalse(p["complete"])
        self.assertTrue(p["incomplete"])
        self.assertEqual(p["start"], _at(2026, 11, 6))
        self.assertEqual(p["end"], NOW)
        self.assertEqual(p["month_end"], _at(2026, 12, 1))
        self.assertEqual(p["days_total"], 20)
        self.assertEqual(p["days_covered"], 15)
        self.assertEqual(p["label"], "November 2026 (to date)")
        self.assertIsNone(p["locked_reason"])

    def test_mockup_sample(self):
        # mockup: 11/18 20:20, 15 days kept -> 11/4 00:00, 15 of 18 days
        now = _at(2026, 11, 18, 20, 20)
        p = pu.period_info(2026, 11, now, 15)
        self.assertEqual(p["start"], _at(2026, 11, 4))
        self.assertEqual((p["days_covered"], p["days_total"]), (15, 18))
        self.assertEqual(
            pu.format_range(p["start"], p["end"]), "11/4 00:00 – 11/18 20:20"
        )

    def test_current_month_within_retention(self):
        now = _at(2026, 11, 10, 8)
        p = pu.period_info(2026, 11, now, 15)
        self.assertTrue(p["complete"])
        self.assertFalse(p["incomplete"])
        self.assertEqual(p["start"], _at(2026, 11, 1))
        self.assertEqual((p["days_covered"], p["days_total"]), (10, 10))

    def test_previous_month_locked(self):
        p = pu.period_info(2026, 10, NOW, 15)
        self.assertFalse(p["available"])
        self.assertFalse(p["complete"])
        self.assertFalse(p["incomplete"])
        self.assertEqual(p["days_covered"], 0)
        self.assertEqual(p["label"], "October 2026")
        self.assertEqual(
            p["locked_reason"],
            "Not available: older than the 15 days of metrics this cloud "
            "keeps",
        )

    def test_previous_month_partly_kept(self):
        now = _at(2026, 12, 3, 12)
        p = pu.period_info(2026, 11, now, 15)
        self.assertTrue(p["available"])
        self.assertFalse(p["is_current"])
        self.assertEqual(p["start"], _at(2026, 11, 19))
        self.assertEqual(p["end"], _at(2026, 12, 1))
        self.assertEqual((p["days_covered"], p["days_total"]), (12, 30))
        self.assertEqual(
            pu.format_range(p["start"], p["end"]), "11/19 00:00 – 11/30 24:00"
        )

    def test_long_retention_complete_past_month(self):
        p = pu.period_info(2026, 10, NOW, 90)
        self.assertTrue(p["complete"])
        self.assertEqual((p["days_covered"], p["days_total"]), (31, 31))

    def test_year_boundary(self):
        now = _at(2027, 1, 5)
        p = pu.period_info(2026, 12, now, 60)
        self.assertEqual(p["month_end"], _at(2027, 1, 1))
        self.assertTrue(p["complete"])


class ListPeriodsTests(base.TestCase):
    def test_current_plus_one_locked(self):
        periods = pu.list_periods(NOW, 15)
        self.assertEqual([p["key"] for p in periods], ["2026-11", "2026-10"])
        self.assertEqual([p["available"] for p in periods], [True, False])

    def test_early_in_the_month(self):
        periods = pu.list_periods(_at(2026, 12, 3), 15)
        self.assertEqual(
            [p["key"] for p in periods], ["2026-12", "2026-11", "2026-10"]
        )
        self.assertEqual(
            [p["available"] for p in periods], [True, True, False]
        )

    def test_bounded(self):
        periods = pu.list_periods(NOW, 10000)
        self.assertEqual(len(periods), pu.MAX_MONTHS)
        self.assertTrue(all(p["available"] for p in periods))
        self.assertEqual(periods[-1]["key"], "2025-12")


class DayStatesTests(base.TestCase):
    def test_not_kept_days(self):
        p = pu.period_info(2026, 11, NOW, 15)
        states = pu.day_states(p)
        self.assertEqual(len(states), 20)
        self.assertEqual(states[0], (datetime.date(2026, 11, 1), "not_kept"))
        self.assertEqual(states[4][1], "not_kept")
        self.assertEqual(states[5], (datetime.date(2026, 11, 6), "kept"))
        self.assertEqual(states[-1], (datetime.date(2026, 11, 20), "kept"))
        self.assertEqual(
            pu.missing_bands(states, 15),
            [{"from": 0, "to": 4, "label": "not kept: older than 15 days"}],
        )

    def test_complete_month_has_no_band(self):
        p = pu.period_info(2026, 11, _at(2026, 11, 3, 1), 15)
        self.assertEqual(pu.missing_bands(pu.day_states(p), 15), [])


class InstanceRowsTests(base.TestCase):
    report = {
        "instances": {
            ID1: _usage(
                name="grian-ih-cpu-2",
                vcpus=4.0,
                memory_mib=256.0,
                hours=53.75,
                vcpu_hours=215.0,
                cpu_hours=189.92,
                ram_gb_hours_held=13.44,
                ram_gb_hours_used=2.36,
                net_in=726583.0,
                net_out=220186.0,
            ),
            ID2: _usage(
                name="grian-ih-disk",
                vcpus=1.0,
                memory_mib=512.0,
                hours=53.75,
                vcpu_hours=53.75,
                cpu_hours=1.0,
                ram_gb_hours_held=26.88,
                ram_gb_hours_used=2.48,
            ),
            # deleted since
            ID3: _usage(
                name="old",
                vcpus=2.0,
                memory_mib=2048.0,
                hours=1.0,
                vcpu_hours=2.0,
                cpu_hours=float("nan"),
            ),
            # a series without any usage
            "7a0a1f43-6b0c-4f1e-9a43-3c2f1d1e8b11": _usage(),
        },
        "volume_gb_hours": 24.0,
        "snapshot_gb_hours": None,
    }
    servers = [
        {"id": ID1, "name": "cpu-2", "status": "ACTIVE", "flavor": "c4"},
        {"id": ID2, "name": "disk", "status": "SHUTOFF", "flavor": None},
        {"id": "e1", "name": "broken", "status": "ERROR", "flavor": "t"},
        {"id": "s1", "name": "Alpha", "status": "SHUTOFF", "flavor": "t"},
    ]

    def test_rows_with_servers(self):
        rows = pu.instance_rows(self.report, self.servers, THRESHOLDS)
        self.assertEqual([r["id"] for r in rows], [ID1, ID2, ID3])
        first, second, deleted = rows
        self.assertEqual(first["name"], "cpu-2")
        self.assertEqual(first["flavor"], "c4")
        self.assertEqual(first["status"], "ACTIVE")
        self.assertFalse(first["deleted"])
        self.assertAlmostEqual(first["cpu_ratio"], 100 * 189.92 / 215)
        self.assertFalse(first["cpu_low"])
        # no flavor from Nova: the size from the metrics
        self.assertEqual(second["flavor"], "1 vCPU, 512 MiB")
        self.assertTrue(second["cpu_low"])
        self.assertEqual(second["status"], "SHUTOFF")
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["name"], "old")
        self.assertEqual(deleted["flavor"], "2 vCPU, 2 GiB")
        # non-finite values count as 0
        self.assertEqual(deleted["cpu_hours"], 0.0)

    def test_rows_without_servers(self):
        rows = pu.instance_rows(self.report, None, THRESHOLDS)
        self.assertEqual(rows[0]["name"], "grian-ih-cpu-2")
        self.assertIsNone(rows[0]["deleted"])
        self.assertIsNone(rows[0]["status"])

    def test_name_falls_back_to_id(self):
        report = {"instances": {ID1: _usage(hours=1.0)}}
        rows = pu.instance_rows(report, None, THRESHOLDS)
        self.assertEqual(rows[0]["name"], ID1)
        self.assertIsNone(rows[0]["flavor"])
        self.assertIsNone(rows[0]["cpu_ratio"])
        self.assertFalse(rows[0]["cpu_low"])

    def test_low_is_judged_as_shown(self):
        report = {
            "instances": {
                ID1: _usage(vcpu_hours=1000.0, cpu_hours=95.0),  # "10%"
                ID2: _usage(vcpu_hours=1000.0, cpu_hours=94.9),  # "9%"
            }
        }
        rows = {
            r["id"]: r["cpu_low"]
            for r in pu.instance_rows(report, None, THRESHOLDS)
        }
        self.assertEqual(rows, {ID1: False, ID2: True})

    def test_threshold(self):
        rows = pu.instance_rows(self.report, self.servers, {"cpu_use_low": 95})
        self.assertTrue(rows[0]["cpu_low"])

    def test_not_listed(self):
        rows = pu.instance_rows(self.report, self.servers, THRESHOLDS)
        self.assertEqual(
            pu.not_listed(self.servers, rows),
            [
                {"id": "s1", "name": "Alpha", "status": "SHUTOFF"},
                {"id": "e1", "name": "broken", "status": "ERROR"},
            ],
        )
        self.assertEqual(pu.not_listed(None, rows), [])

    def test_totals(self):
        rows = pu.instance_rows(self.report, self.servers, THRESHOLDS)
        totals = pu.totals(rows, self.report)
        self.assertEqual(totals["instances"], 3)
        self.assertAlmostEqual(totals["vcpu_hours"], 270.75)
        self.assertAlmostEqual(totals["hours"], 108.5)
        self.assertAlmostEqual(totals["cpu_ratio"], 100 * 190.92 / 270.75)
        self.assertEqual(totals["net_total"], 726583.0 + 220186.0)
        self.assertEqual(totals["volume_gb_hours"], 24.0)
        self.assertEqual(totals["snapshot_gb_hours"], 0.0)
        self.assertEqual(totals["storage_gb_hours"], 24.0)

    def test_empty(self):
        totals = pu.totals([], {})
        self.assertEqual(totals["instances"], 0)
        self.assertIsNone(totals["cpu_ratio"])
        self.assertIsNone(totals["ram_ratio"])


class DailyTests(base.TestCase):
    def test_daily(self):
        p = pu.period_info(2026, 11, NOW, 15)
        d18 = _at(2026, 11, 18, 9).timestamp()
        d19 = _at(2026, 11, 19, 23).timestamp()
        # before the kept range: ignored (not kept)
        d3 = _at(2026, 11, 3, 1).timestamp()
        report = {
            "hourly": {
                "vcpu_hours": [[d3, 9.0], [d18, 5.0], [d18 + 3600, 5.0]],
                "cpu_hours": [[d18, 4.123], [d19, 1.0]],
                "net_bytes": [[d19, 1000.4]],
            }
        }
        d = pu.daily(report, p)
        self.assertEqual(len(d["labels"]), 20)
        self.assertEqual(d["labels"][0], "11/1")
        self.assertEqual(d["labels"][17], "11/18")
        cpu = d["cpu"]
        self.assertEqual(cpu["held"][:5], [None] * 5)
        self.assertEqual(cpu["held"][5], 0.0)
        self.assertEqual(cpu["held"][17], 10.0)
        self.assertEqual(cpu["used"][17], 4.12)
        self.assertEqual(cpu["used"][18], 1.0)
        self.assertIsNone(d["net"]["held"])
        self.assertEqual(d["net"]["used"][18], 1000)
        self.assertEqual(d["mem"]["used"][17], 0.0)
        self.assertEqual(d["bands"][0]["to"], 4)

    def test_first_data(self):
        self.assertIsNone(pu.first_data({}))
        report = {
            "hourly": {
                "vcpu_hours": [[100, 0.0], [200, 1.0]],
                "net_bytes": [[150, 5.0]],
            }
        }
        self.assertEqual(pu.first_data(report), 150)


class CsvTests(base.TestCase):
    def test_rows(self):
        p = pu.period_info(2026, 11, NOW, 15)
        report = {
            "instances": {
                ID1: _usage(
                    name="=cmd|' /C calc'!A0",
                    hours=1.234,
                    vcpu_hours=4.936,
                    net_in=10.6,
                    net_out=2.2,
                )
            }
        }
        rows = pu.instance_rows(report, None, THRESHOLDS)
        records = pu.csv_rows(rows, p)
        self.assertEqual(records[0], list(pu.CSV_COLUMNS))
        self.assertEqual(
            records[1],
            [
                "2026-11-06T00:00:00Z",
                "2026-11-20T15:40:00Z",
                "'=cmd|' /C calc'!A0",
                ID1,
                "",
                "1.23",
                "4.94",
                "0.00",
                "0.00",
                "0.00",
                "11",
                "2",
            ],
        )

    def test_filename(self):
        p = pu.period_info(2026, 11, NOW, 15)
        self.assertEqual(
            pu.csv_filename("demo", p),
            "usage-demo-2026-11-06_to_2026-11-20.csv",
        )
        self.assertEqual(
            pu.csv_filename('a"b/c\r\nd é', p),
            "usage-a_b_c__d__-2026-11-06_to_2026-11-20.csv",
        )
        self.assertTrue(pu.csv_filename(None, p).startswith("usage-project-"))
        past = pu.period_info(2026, 10, NOW, 60)
        self.assertEqual(
            pu.csv_filename("x", past), "usage-x-2026-10-01_to_2026-10-31.csv"
        )


class FormatTests(base.TestCase):
    def test_amount(self):
        self.assertEqual(pu.format_amount(39.04), "39.0")
        self.assertEqual(pu.format_amount(0.05), "0.1")
        self.assertEqual(pu.format_amount(1234.5), "1,235")
        self.assertEqual(pu.format_amount(None), "–")

    def test_bytes(self):
        self.assertEqual(pu.format_bytes(512), "512 B")
        self.assertEqual(pu.format_bytes(1536), "1.5 KiB")
        self.assertEqual(pu.format_bytes(2 * 1024**3), "2 GiB")
        self.assertEqual(pu.format_bytes(200 * 1024**2), "200 MiB")
        self.assertEqual(pu.format_bytes(None), "–")

    def test_percent_and_ratio(self):
        self.assertEqual(pu.format_percent(82.5), "83")
        self.assertIsNone(pu.format_percent(None))
        self.assertIsNone(pu.ratio_percent(1, 0))
        self.assertEqual(pu.ratio_percent(1, 4), 25.0)

    def test_moments(self):
        self.assertEqual(
            pu.format_moment(_at(2026, 11, 4, 9, 5)), "11/4 09:05"
        )
        self.assertEqual(
            pu.format_range(_at(2026, 10, 1), _at(2026, 11, 1)),
            "10/1 00:00 – 10/31 24:00",
        )

    def test_flavor_from_metrics(self):
        self.assertIsNone(pu.flavor_from_metrics(None, None))
        self.assertEqual(pu.flavor_from_metrics(4, None), "4 vCPU")
        self.assertEqual(pu.flavor_from_metrics(None, 1024), "1 GiB")


class ConfigTests(base.TestCase):
    def test_thresholds_default(self):
        self.assertEqual(
            config.get_project_usage_thresholds(), {"cpu_use_low": 10}
        )

    @override_settings(
        GRIAN_PLUGIN={"project_usage_thresholds": {"cpu_use_low": 25}}
    )
    def test_thresholds_override(self):
        self.assertEqual(
            config.get_project_usage_thresholds(), {"cpu_use_low": 25}
        )

    def test_thresholds_invalid(self):
        for value in ({"cpu_use_low": 100}, {"other": 1}, [1]):
            with (
                self.subTest(value=value),
                override_settings(
                    GRIAN_PLUGIN={"project_usage_thresholds": value}
                ),
                self.assertRaises(config.ConfigurationError),
            ):
                config.get_project_usage_thresholds()
