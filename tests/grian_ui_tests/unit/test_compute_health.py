#
# SPDX-License-Identifier: Apache-2.0

from unittest import mock

from grian_ui import compute_health
from grian_ui import config
from grian_ui_tests.unit import base


THRESHOLDS = dict(config.DEFAULT_COMPUTE_HOST_THRESHOLDS)


def _host(name, cpu, iowait=0.0):
    return {"name": name, "cpu_avg": cpu, "iowait_avg": iowait}


class QuantileTests(base.TestCase):
    def test_empty(self):
        self.assertIsNone(compute_health.quantile([], 0.5))

    def test_single_value(self):
        self.assertEqual(compute_health.quantile([42], 0.95), 42)

    def test_interpolates_like_promql(self):
        values = list(range(1, 101))
        self.assertAlmostEqual(compute_health.quantile(values, 0.5), 50.5)
        self.assertAlmostEqual(compute_health.quantile(values, 0.95), 95.05)
        self.assertEqual(compute_health.quantile(values, 1.0), 100)
        self.assertEqual(compute_health.quantile(values, 0.0), 1)
        # the fleet chart (Prometheus quantile()) says 50% for two hosts
        self.assertEqual(compute_health.quantile([10, 90], 0.5), 50)

    def test_unsorted_input(self):
        self.assertEqual(compute_health.quantile([9, 1, 5], 0.5), 5)


class EvaluateHostTests(base.TestCase):
    def test_healthy(self):
        status = compute_health.evaluate_host(_host("h", 40, 2), THRESHOLDS)
        self.assertEqual(status["key"], compute_health.STATUS_HEALTHY)
        self.assertEqual(status["severity"], 0)
        self.assertEqual(status["reasons"], [])

    def test_cpu_warning_boundary(self):
        status = compute_health.evaluate_host(_host("h", 75), THRESHOLDS)
        self.assertEqual(status["key"], compute_health.STATUS_WARNING)
        self.assertEqual(status["reasons"], ["CPU 75%"])

    def test_cpu_critical(self):
        status = compute_health.evaluate_host(_host("h", 93.5), THRESHOLDS)
        self.assertEqual(status["key"], compute_health.STATUS_CRITICAL)
        self.assertEqual(status["reasons"], ["CPU 93.5%"])

    def test_iowait_warning(self):
        status = compute_health.evaluate_host(_host("h", 30, 12), THRESHOLDS)
        self.assertEqual(status["key"], compute_health.STATUS_WARNING)
        self.assertEqual(status["reasons"], ["I/O wait 12%"])

    def test_critical_wins_and_keeps_warning_reason(self):
        status = compute_health.evaluate_host(_host("h", 80, 25), THRESHOLDS)
        self.assertEqual(status["key"], compute_health.STATUS_CRITICAL)
        self.assertEqual(status["reasons"], ["I/O wait 25%", "CPU 80%"])

    def test_missing_cpu_is_unknown(self):
        status = compute_health.evaluate_host(
            {"name": "h", "cpu_avg": None, "iowait_avg": None}, THRESHOLDS
        )
        self.assertEqual(status["key"], compute_health.STATUS_UNKNOWN)
        self.assertEqual(status["severity"], 1)
        self.assertEqual(status["reasons"], ["no valid CPU data"])

    def test_missing_cpu_still_reports_iowait_problem(self):
        status = compute_health.evaluate_host(
            {"name": "h", "cpu_avg": None, "iowait_avg": 35}, THRESHOLDS
        )
        self.assertEqual(status["key"], compute_health.STATUS_CRITICAL)
        self.assertEqual(
            status["reasons"], ["I/O wait 35%", "no valid CPU data"]
        )

    def test_unrounded_value_below_threshold(self):
        # 89.96 must not be rounded up to the 90% critical threshold
        status = compute_health.evaluate_host(_host("h", 89.96), THRESHOLDS)
        self.assertEqual(status["key"], compute_health.STATUS_WARNING)

    def test_custom_thresholds(self):
        thresholds = dict(THRESHOLDS, cpu_warning=50)
        status = compute_health.evaluate_host(_host("h", 55), thresholds)
        self.assertEqual(status["key"], compute_health.STATUS_WARNING)


class BandTests(base.TestCase):
    def test_default_bands(self):
        bands = compute_health.build_bands(THRESHOLDS)
        self.assertEqual(
            [b["label"] for b in bands],
            ["0–25%", "25–50%", "50–75%", "75–90%", "≥ 90%"],
        )
        self.assertEqual(
            [b["level"] for b in bands],
            ["healthy", "healthy", "healthy", "warning", "critical"],
        )

    def test_bands_follow_thresholds(self):
        bands = compute_health.build_bands(
            dict(THRESHOLDS, cpu_warning=80, cpu_critical=95)
        )
        self.assertEqual([b["low"] for b in bands], [0, 25, 50, 80, 95])

    def test_duplicate_edges_are_merged(self):
        bands = compute_health.build_bands(
            dict(THRESHOLDS, cpu_warning=50, cpu_critical=90)
        )
        self.assertEqual([b["low"] for b in bands], [0, 25, 50, 90])

    def test_highest_threshold_gets_its_own_band(self):
        bands = compute_health.build_bands(
            dict(THRESHOLDS, cpu_warning=95, cpu_critical=99)
        )
        self.assertEqual(bands[-1]["low"], 99)
        self.assertEqual(bands[-1]["level"], "critical")
        self.assertEqual(compute_health.band_index(100, bands), len(bands) - 1)

    def test_band_index(self):
        bands = compute_health.build_bands(THRESHOLDS)
        self.assertEqual(compute_health.band_index(0, bands), 0)
        self.assertEqual(compute_health.band_index(25, bands), 1)
        self.assertEqual(compute_health.band_index(89.9, bands), 3)
        self.assertEqual(compute_health.band_index(90, bands), 4)
        self.assertEqual(compute_health.band_index(100, bands), 4)


class SummarizeTests(base.TestCase):
    def _snapshot(self):
        return {
            "hosts": [
                _host("ok-1", 20),
                _host("ok-2", 40),
                _host("warn-cpu", 80),
                _host("warn-io", 30, 15),
                _host("crit-cpu", 95),
                _host("crit-io", 50, 30),
            ],
            # unsorted on purpose: the order must come from the sort
            "stale": [
                {"name": "stale-old", "last_seen": 1000.0},
                {"name": "stale-new", "last_seen": 2000.0},
            ],
        }

    def test_counts(self):
        summary = compute_health.summarize(self._snapshot(), THRESHOLDS)
        self.assertEqual(summary["reporting"], 6)
        self.assertEqual(summary["stale"], 2)
        self.assertEqual(summary["known"], 8)
        self.assertEqual(summary["critical"], 2)
        self.assertEqual(summary["warning"], 2)
        self.assertEqual(summary["attention_total"], 6)

    def test_attention_order(self):
        summary = compute_health.summarize(self._snapshot(), THRESHOLDS)
        names = [row["name"] for row in summary["attention"]]
        self.assertEqual(
            names,
            [
                "crit-cpu",
                "crit-io",
                "warn-cpu",
                "warn-io",
                # the host that stopped reporting most recently first
                "stale-new",
                "stale-old",
            ],
        )

    def test_bigger_problem_first_within_a_severity(self):
        snapshot = {
            "hosts": [
                _host("warn-low", 76),
                _host("warn-high", 85),
                _host("warn-io", 50, 15),
            ],
            "stale": [],
        }
        summary = compute_health.summarize(snapshot, THRESHOLDS)
        self.assertEqual(
            [r["name"] for r in summary["attention"]],
            ["warn-high", "warn-low", "warn-io"],
        )

    def test_stale_before_unknown(self):
        snapshot = {
            "hosts": [{"name": "unknown", "cpu_avg": None, "iowait_avg": 5.0}],
            "stale": [{"name": "stale", "last_seen": 1000.0}],
        }
        summary = compute_health.summarize(snapshot, THRESHOLDS)
        self.assertEqual(
            [r["name"] for r in summary["attention"]], ["stale", "unknown"]
        )

    def test_attention_is_capped(self):
        snapshot = {
            "hosts": [_host(f"h{i}", 95) for i in range(50)],
            "stale": [],
        }
        summary = compute_health.summarize(snapshot, THRESHOLDS, limit=10)
        self.assertEqual(len(summary["attention"]), 10)
        self.assertEqual(summary["attention_total"], 50)

    def test_fleet_quantiles_and_max_iowait(self):
        summary = compute_health.summarize(self._snapshot(), THRESHOLDS)
        # interpolated like PromQL: 20, 30, 40, 50, 80, 95
        self.assertEqual(summary["cpu_median"], 45)
        self.assertAlmostEqual(summary["cpu_p95"], 91.25)
        self.assertEqual(
            summary["max_iowait"], {"name": "crit-io", "value": 30}
        )

    def test_band_counts(self):
        summary = compute_health.summarize(self._snapshot(), THRESHOLDS)
        self.assertEqual(
            [b["count"] for b in summary["bands"]], [1, 2, 1, 1, 1]
        )
        self.assertEqual(
            sum(b["count"] for b in summary["bands"]), summary["reporting"]
        )

    def test_unknown_hosts_and_band_total(self):
        snapshot = self._snapshot()
        snapshot["hosts"].append(
            {"name": "nan", "cpu_avg": None, "iowait_avg": None}
        )
        summary = compute_health.summarize(snapshot, THRESHOLDS)
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["reporting"], 7)
        self.assertEqual(summary["banded"], 6)
        self.assertIn("nan", [r["name"] for r in summary["attention"]])

    def test_all_stale_single_host_is_not_blamed_on_pipeline(self):
        summary = compute_health.summarize(
            {"hosts": [], "stale": [{"name": "a", "last_seen": 1.0}]},
            THRESHOLDS,
        )
        self.assertTrue(summary["all_stale"])
        self.assertFalse(summary["pipeline_down"])

    def test_all_stale_far_apart_is_not_blamed_on_pipeline(self):
        stale = [
            {"name": f"h{i}", "last_seen": 1000.0 + i * 3600} for i in range(4)
        ]
        summary = compute_health.summarize(
            {"hosts": [], "stale": stale}, THRESHOLDS
        )
        self.assertTrue(summary["all_stale"])
        self.assertFalse(summary["pipeline_down"])

    def test_pipeline_down_despite_a_long_gone_host(self):
        # a decommissioned host (20 h ago) must not hide a pipeline outage
        now = 1_800_000_000
        stale = [{"name": "old", "last_seen": now - 20 * 3600}] + [
            {"name": f"h{i}", "last_seen": now - 700 - i * 60}
            for i in range(3)
        ]
        summary = compute_health.summarize(
            {"hosts": [], "stale": stale}, THRESHOLDS
        )
        self.assertTrue(summary["pipeline_down"])

    def test_hosts_stopped_together_are_counted(self):
        now = 1_800_000_000
        stale = [{"name": "old", "last_seen": now - 20 * 3600}] + [
            {"name": f"h{i}", "last_seen": now - 700 - i * 60}
            for i in range(3)
        ]
        summary = compute_health.summarize(
            {"hosts": [], "stale": stale}, THRESHOLDS
        )
        self.assertEqual(summary["pipeline_hosts"], 3)
        self.assertEqual(summary["stale"], 4)

    def test_no_pipeline_outage_while_a_host_reports(self):
        now = 1_800_000_000
        stale = [
            {"name": f"h{i}", "last_seen": now - 700 - i * 60}
            for i in range(3)
        ]
        summary = compute_health.summarize(
            {"hosts": [_host("up", 10)], "stale": stale}, THRESHOLDS
        )
        self.assertEqual(summary["pipeline_hosts"], 0)
        self.assertFalse(summary["pipeline_down"])

    def test_two_hosts_together_are_not_enough(self):
        now = 1_800_000_000
        stale = [{"name": "old", "last_seen": now - 20 * 3600}] + [
            {"name": f"h{i}", "last_seen": now - 700 - i * 60}
            for i in range(2)
        ]
        summary = compute_health.summarize(
            {"hosts": [], "stale": stale}, THRESHOLDS
        )
        self.assertFalse(summary["pipeline_down"])

    def test_pipeline_down(self):
        stale = [
            {"name": f"h{i}", "last_seen": 1000.0 + i * 60} for i in range(4)
        ]
        summary = compute_health.summarize(
            {"hosts": [], "stale": stale}, THRESHOLDS
        )
        self.assertTrue(summary["pipeline_down"])
        self.assertFalse(
            compute_health.summarize(self._snapshot(), THRESHOLDS)[
                "pipeline_down"
            ]
        )

    def test_empty_snapshot(self):
        summary = compute_health.summarize(
            {"hosts": [], "stale": []}, THRESHOLDS
        )
        self.assertEqual(summary["known"], 0)
        self.assertIsNone(summary["cpu_median"])
        self.assertIsNone(summary["max_iowait"])
        self.assertEqual(summary["attention"], [])


class SelectTopTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.snapshot = {
            "hosts": [_host(f"h{i:02d}", i * 4, i % 7) for i in range(25)],
            "stale": [
                {"name": "s1", "last_seen": 300.0},
                {"name": "s2", "last_seen": 100.0},
            ],
        }

    def test_by_cpu(self):
        rows, total = compute_health.select_top(self.snapshot, THRESHOLDS)
        self.assertEqual(total, 25)
        self.assertEqual(len(rows), compute_health.TOP_LIMIT)
        self.assertEqual(rows[0]["name"], "h24")
        cpus = [r["cpu_avg"] for r in rows]
        self.assertEqual(cpus, sorted(cpus, reverse=True))
        self.assertEqual(rows[0]["status"]["key"], "critical")

    def test_by_iowait(self):
        rows, _total = compute_health.select_top(
            self.snapshot, THRESHOLDS, by=compute_health.TOP_BY_IOWAIT
        )
        values = [r["iowait_avg"] for r in rows]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(values[0], 6)

    def test_by_stale_most_recent_first(self):
        rows, total = compute_health.select_top(
            self.snapshot, THRESHOLDS, by=compute_health.TOP_BY_STALE
        )
        self.assertEqual(total, 2)
        self.assertEqual([r["name"] for r in rows], ["s1", "s2"])
        self.assertEqual(rows[0]["status"]["key"], "stale")

    def test_by_band(self):
        rows, total = compute_health.select_top(
            self.snapshot, THRESHOLDS, by=compute_health.TOP_BY_BAND, band=4
        )
        # cpu >= 90: h23 (92) and h24 (96)
        self.assertEqual(total, 2)
        self.assertEqual([r["name"] for r in rows], ["h24", "h23"])

    def test_by_band_invalid_index(self):
        for band in (None, -1, 99):
            rows, total = compute_health.select_top(
                self.snapshot,
                THRESHOLDS,
                by=compute_health.TOP_BY_BAND,
                band=band,
            )
            self.assertEqual((rows, total), ([], 0))

    def test_custom_limit(self):
        rows, total = compute_health.select_top(
            self.snapshot, THRESHOLDS, limit=3
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(total, 25)


class FindHostTests(base.TestCase):
    snapshot = {
        "hosts": [_host("compute-01", 10)],
        "stale": [{"name": "compute-02", "last_seen": 1.0}],
    }

    def test_reporting_host(self):
        host = compute_health.find_host(self.snapshot, " compute-01 ")
        self.assertEqual(host["cpu_avg"], 10)
        self.assertNotIn("stale", host)

    def test_stale_host(self):
        host = compute_health.find_host(self.snapshot, "compute-02")
        self.assertTrue(host["stale"])

    def test_unknown_host(self):
        self.assertIsNone(compute_health.find_host(self.snapshot, "nope"))
        self.assertIsNone(compute_health.find_host(self.snapshot, None))
        self.assertIsNone(compute_health.find_host(self.snapshot, ""))

    def test_name_in_another_case(self):
        host = compute_health.find_host(self.snapshot, "Compute-01")
        self.assertEqual(host["name"], "compute-01")
        host = compute_health.find_host(self.snapshot, "COMPUTE-02")
        self.assertEqual(host["name"], "compute-02")
        self.assertTrue(host["stale"])

    def test_exact_name_wins_over_other_cases(self):
        snapshot = {
            "hosts": [_host("cmp", 10), _host("CMP", 20)],
            "stale": [],
        }
        self.assertEqual(
            compute_health.find_host(snapshot, "CMP")["cpu_avg"], 20
        )
        # ambiguous: two hosts differ in case alone
        self.assertIsNone(compute_health.find_host(snapshot, "Cmp"))
        self.assertEqual(
            compute_health.names_like(snapshot, " Cmp "), ["CMP", "cmp"]
        )
        self.assertEqual(compute_health.names_like(snapshot, "x"), [])


class SuggestHostsTests(base.TestCase):
    snapshot = {
        "hosts": [
            _host("node-b", 20),
            _host("node-a", 95),  # critical
            _host("compute-node-1", 30),
            _host("NODE-C", 80),  # warning
        ],
        "stale": [{"name": "zombie", "last_seen": 1.0}],
    }

    def names(self, query, **kwargs):
        return [
            r["name"]
            for r in compute_health.suggest_hosts(
                self.snapshot, THRESHOLDS, query, **kwargs
            )
        ]

    def test_prefix_before_substring_case_insensitive(self):
        self.assertEqual(
            self.names("node"),
            ["NODE-C", "node-a", "node-b", "compute-node-1"],
        )

    def test_no_match(self):
        self.assertEqual(self.names("nope"), [])

    def test_empty_query_lists_recently_silent_hosts_first(self):
        snapshot = {
            "hosts": [],
            "stale": [
                {"name": "old", "last_seen": 1000.0},
                {"name": "new", "last_seen": 2000.0},
            ],
        }
        rows = compute_health.suggest_hosts(snapshot, THRESHOLDS, "")
        self.assertEqual([r["name"] for r in rows], ["new", "old"])

    def test_empty_query_attention_first(self):
        self.assertEqual(
            self.names(""),
            ["node-a", "NODE-C", "zombie", "compute-node-1", "node-b"],
        )
        self.assertEqual(self.names(None)[0], "node-a")

    def test_limit(self):
        snapshot = {
            "hosts": [_host(f"h{i:04d}", 10) for i in range(500)],
            "stale": [],
        }
        result = compute_health.suggest_hosts(snapshot, THRESHOLDS, "h0")
        self.assertEqual(len(result), compute_health.SUGGESTION_LIMIT)
        self.assertEqual(result[0]["name"], "h0000")

    def test_includes_status(self):
        rows = compute_health.suggest_hosts(self.snapshot, THRESHOLDS, "zom")
        self.assertEqual(rows[0]["status"]["key"], "stale")

    def test_typing_evaluates_status_only_for_returned_rows(self):
        snapshot = {
            "hosts": [_host(f"h{i:04d}", 10) for i in range(500)],
            "stale": [],
        }
        with mock.patch.object(
            compute_health, "evaluate_host", wraps=compute_health.evaluate_host
        ) as evaluate:
            compute_health.suggest_hosts(snapshot, THRESHOLDS, "h0")
        self.assertEqual(evaluate.call_count, compute_health.SUGGESTION_LIMIT)
