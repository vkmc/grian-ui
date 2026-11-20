#
# SPDX-License-Identifier: Apache-2.0

from django.test import override_settings

from grian_ui import config
from grian_ui import project_compute as pc
from grian_ui.datasource import fake_project_compute as fake
from grian_ui_tests.unit import base


THRESHOLDS = dict(config.DEFAULT_PROJECT_COMPUTE_THRESHOLDS)
RS = dict(config.DEFAULT_PROJECT_RIGHTSIZING_THRESHOLDS)
NOW = 1_795_190_000

ID1 = "d414fbd9-66a6-485c-bec7-8191c2fa4221"
ID2 = "c726b640-2a18-41e3-9fac-af2730c12410"
ID3 = "133a85dc-be0f-44e3-8f4f-475b79e704c6"


def _server(sid, status="ACTIVE", vcpus=1, ram=512, name=None, **extra):
    return dict(
        {
            "id": sid,
            "name": name or sid[:8],
            "status": status,
            "flavor": "m1.test",
            "vcpus": vcpus,
            "ram": ram,
            "disk": 1,
            "fault": None,
            "updated": NOW - 7 * 3600,
        },
        **extra,
    )


#: The demo project on 2026-11-20 (Nova and Aetos, read as demo/demo).
DEMO_SERVERS = [
    _server(ID1, vcpus=4, ram=256, name="grian-ih-cpu-2", flavor="grian.cpu4"),
    _server(ID2, vcpus=1, ram=512, name="grian-ih-disk", flavor="m1.tiny"),
    _server(
        ID3,
        "ERROR",
        vcpus=1,
        ram=40960,
        name="grian-ih-error",
        flavor="grian.toobig",
        fault="No valid host was found.",
        updated=NOW - 2 * 86400 - 7 * 3600,
    ),
]
DEMO_LIMITS = {
    "maxTotalInstances": 10,
    "totalInstancesUsed": 3,
    "maxTotalCores": 20,
    "totalCoresUsed": 6,
    "maxTotalRAMSize": 51200,
    "totalRAMUsed": 41728,
}
DEMO_SNAPSHOT = {
    ID1: {
        "name": "grian-ih-cpu-2",
        "vcpus": 4.0,
        "memory_alloc": 256.0,
        "cpu": 3.477,
        "cpu_wait": 0.523,
        "memory": 45.04,
        "swap": 0.0,
        "disks": {
            "vda": {
                "used": 31522816.0,
                "capacity": 1073741824.0,
                "write": 0.0,
                "latency": None,
            }
        },
    },
    ID2: {
        "name": "grian-ih-disk",
        "vcpus": 1.0,
        "memory_alloc": 512.0,
        "cpu": 0.637,
        "cpu_wait": 0.0977,
        "memory": 47.38,
        "swap": 0.0,
        "disks": {
            "vda": {
                "used": 483721216.0,
                "capacity": 1073741824.0,
                "write": 896556432.8,
                "latency": 2.72,
            }
        },
    },
}
DEMO_STATS = {
    ID1: {
        "cpu_avg": 3.524,
        "cpu_p95": 3.572,
        "memory_avg": 45.02,
        "swap": 0.0,
        "hours": 54.0,
    },
    ID2: {
        "cpu_avg": 0.638,
        "cpu_p95": 0.646,
        "memory_avg": 47.29,
        "swap": 0.0,
        "hours": 54.0,
    },
}


class FormatTests(base.TestCase):
    def test_bytes(self):
        self.assertEqual(pc.format_bytes(None), "–")
        self.assertEqual(pc.format_bytes(512), "512 B")
        self.assertEqual(pc.format_bytes(1536), "1.5 KiB")
        self.assertEqual(pc.format_bytes(1073741824), "1 GiB")
        self.assertEqual(pc.format_bytes(150 * 1024**3), "150 GiB")

    def test_mib(self):
        self.assertEqual(pc.format_mib(None), "–")
        self.assertEqual(pc.format_mib(512), "512 MiB")
        self.assertEqual(pc.format_mib(40960), "40 GiB")
        self.assertEqual(pc.format_mib(41728), "40.8 GiB")

    def test_quota(self):
        self.assertEqual(pc.format_quota(pc.QUOTA_CORES, 6), "6")
        self.assertEqual(pc.format_quota(pc.QUOTA_CORES, 6.5), "6.5")
        self.assertEqual(pc.format_quota(pc.QUOTA_RAM, 51200), "50 GiB")
        self.assertEqual(pc.format_quota(pc.QUOTA_INSTANCES, None), "–")

    def test_cores(self):
        self.assertEqual(pc.format_cores(3.524), "3.52")
        self.assertEqual(pc.format_cores(12.34), "12.3")
        self.assertEqual(pc.format_cores(4.0), "4")
        self.assertEqual(pc.format_cores(None), "–")

    def test_duration(self):
        self.assertEqual(pc.format_duration(None), "")
        self.assertEqual(pc.format_duration(-5), "1 minute")
        self.assertEqual(pc.format_duration(45 * 60), "45 minutes")
        self.assertEqual(pc.format_duration(7 * 3600 + 59), "7 hours")
        self.assertEqual(pc.format_duration(47 * 3600), "47 hours")
        self.assertEqual(pc.format_duration(9 * 86400), "9 days")

    def test_percent_and_level(self):
        self.assertIsNone(pc.percent(1, 0))
        self.assertIsNone(pc.percent(None, 10))
        self.assertIsNone(pc.percent(1, None))
        self.assertEqual(pc.percent(3, 12), 25.0)
        self.assertEqual(pc.level(None, 75, 90), pc.LEVEL_OK)
        self.assertEqual(pc.level(74.9, 75, 90), pc.LEVEL_OK)
        self.assertEqual(pc.level(75, 75, 90), pc.LEVEL_WARNING)
        self.assertEqual(pc.level(90, 75, 90), pc.LEVEL_CRITICAL)


class ServerTests(base.TestCase):
    def test_groups(self):
        self.assertEqual(pc.server_group("ACTIVE"), pc.GROUP_RUNNING)
        self.assertEqual(pc.server_group("BUILD"), pc.GROUP_RUNNING)
        self.assertEqual(pc.server_group("resize"), pc.GROUP_RUNNING)
        self.assertEqual(pc.server_group("ERROR"), pc.GROUP_ERROR)
        for status in ("SHUTOFF", "PAUSED", "SUSPENDED", "SHELVED"):
            self.assertEqual(pc.server_group(status), pc.GROUP_STOPPED)
        self.assertEqual(
            pc.server_group("SHELVED_OFFLOADED"), pc.GROUP_OFFLOADED
        )
        self.assertEqual(pc.server_group(None), pc.GROUP_RUNNING)

    def test_holds(self):
        server = _server(ID1, vcpus=4, ram=256)
        self.assertEqual(pc.holds(server, pc.QUOTA_INSTANCES), 1)
        self.assertEqual(pc.holds(server, pc.QUOTA_CORES), 4)
        self.assertEqual(pc.holds(server, pc.QUOTA_RAM), 256)
        # offloaded: nothing on a host, but Nova's default (legacy) count
        # still includes its flavor
        offloaded = dict(server, status="SHELVED_OFFLOADED")
        self.assertEqual(pc.holds(offloaded, pc.QUOTA_INSTANCES), 1)
        self.assertEqual(pc.holds(offloaded, pc.QUOTA_CORES), 4)
        self.assertEqual(pc.holds(offloaded, pc.QUOTA_RAM), 256)
        # unknown flavor
        self.assertEqual(pc.holds(dict(server, vcpus=None), "cores"), 0)

    def test_counts(self):
        self.assertEqual(
            pc.server_counts(DEMO_SERVERS),
            {"total": 3, "running": 2, "not_running": 1},
        )
        self.assertEqual(
            pc.server_counts([]), {"total": 0, "running": 0, "not_running": 0}
        )


class QuotaRowsTests(base.TestCase):
    def test_demo_project(self):
        rows = {
            r["key"]: r
            for r in pc.quota_rows(DEMO_SERVERS, DEMO_LIMITS, THRESHOLDS)
        }
        ram = rows[pc.QUOTA_RAM]
        self.assertEqual(ram["used"], 41728)
        self.assertEqual(ram["limit"], 51200)
        self.assertAlmostEqual(ram["percent"], 81.5)
        self.assertEqual(ram["level"], pc.LEVEL_WARNING)
        self.assertEqual(
            [(s["group"], s["value"]) for s in ram["segments"]],
            [("running", 768), ("error", 40960)],
        )
        self.assertAlmostEqual(ram["segments"][1]["width"], 80.0)
        self.assertEqual(ram["used_text"], "40.8 GiB")
        self.assertEqual(ram["limit_text"], "50 GiB")
        cores = rows[pc.QUOTA_CORES]
        self.assertEqual(cores["percent"], 30.0)
        self.assertEqual(cores["level"], pc.LEVEL_OK)
        self.assertEqual(rows[pc.QUOTA_INSTANCES]["listed"], 3)
        self.assertEqual(
            pc.highest_quota(list(rows.values()))["key"], pc.QUOTA_RAM
        )

    def test_order_and_labels(self):
        rows = pc.quota_rows(DEMO_SERVERS, DEMO_LIMITS, THRESHOLDS)
        self.assertEqual([r["key"] for r in rows], list(pc.QUOTAS))
        self.assertEqual(
            [str(r["label"]) for r in rows], ["Instances", "vCPUs", "RAM"]
        )

    def test_critical(self):
        limits = dict(DEMO_LIMITS, maxTotalRAMSize=45000)
        ram = pc.quota_rows(DEMO_SERVERS, limits, THRESHOLDS)[2]
        self.assertEqual(ram["level"], pc.LEVEL_CRITICAL)

    def test_thresholds_are_configurable(self):
        thresholds = dict(THRESHOLDS, quota_warning=20, quota_critical=25)
        cores = pc.quota_rows(DEMO_SERVERS, DEMO_LIMITS, thresholds)[1]
        self.assertEqual(cores["level"], pc.LEVEL_CRITICAL)

    def test_unlimited(self):
        for unlimited in (None, -1, float("inf")):
            limits = dict(DEMO_LIMITS, maxTotalCores=unlimited)
            cores = pc.quota_rows(DEMO_SERVERS, limits, THRESHOLDS)[1]
            self.assertIsNone(cores["limit"])
            self.assertIsNone(cores["percent"])
            self.assertEqual(cores["level"], pc.LEVEL_OK)
            self.assertEqual(cores["limit_text"], "–")
        self.assertIsNone(
            pc.highest_quota(
                pc.quota_rows(
                    DEMO_SERVERS,
                    {k: None for k in DEMO_LIMITS},
                    THRESHOLDS,
                )
            )
        )

    def test_nova_count_wins_over_the_list(self):
        # an instance being created: counted by Nova, not listed yet
        limits = dict(DEMO_LIMITS, totalInstancesUsed=4)
        row = pc.quota_rows(DEMO_SERVERS, limits, THRESHOLDS)[0]
        self.assertEqual(row["used"], 4)
        self.assertEqual(row["listed"], 3)
        self.assertEqual(row["percent"], 40.0)

    def test_without_nova_counts(self):
        row = pc.quota_rows(DEMO_SERVERS, {}, THRESHOLDS)[1]
        self.assertEqual(row["used"], 6)
        self.assertIsNone(row["limit"])

    def test_segment_widths_stay_within_the_bar(self):
        # over quota (the limit was lowered): scaled to the largest value
        limits = dict(DEMO_LIMITS, maxTotalRAMSize=20480)
        ram = pc.quota_rows(DEMO_SERVERS, limits, THRESHOLDS)[2]
        self.assertLessEqual(sum(s["width"] for s in ram["segments"]), 100)

    def test_stopped_and_offloaded(self):
        servers = [
            _server(ID1, "SHUTOFF", vcpus=2, ram=2048),
            _server(ID2, "SHELVED_OFFLOADED", vcpus=4, ram=4096),
        ]
        rows = {r["key"]: r for r in pc.quota_rows(servers, {}, THRESHOLDS)}
        self.assertEqual(
            [(s["group"], s["value"]) for s in rows["instances"]["segments"]],
            [("stopped", 2)],
        )
        # counted by flavor, as Nova's default quota count does
        self.assertEqual(rows["cores"]["segments"][0]["value"], 6)
        self.assertEqual(rows["ram"]["segments"][0]["value"], 6144)

    def test_offloaded_matches_the_legacy_nova_count(self):
        servers = [
            _server(ID1, vcpus=2, ram=2048),
            _server(ID2, "SHELVED_OFFLOADED", vcpus=4, ram=4096),
        ]
        limits = dict(
            DEMO_LIMITS,
            totalInstancesUsed=2,
            totalCoresUsed=6,
            totalRAMUsed=6144,
        )
        rows = pc.quota_rows(servers, limits, THRESHOLDS)
        self.assertEqual(
            [r["listed"] for r in rows], [r["used"] for r in rows]
        )


class IdleHoldersTests(base.TestCase):
    def test_demo_project(self):
        rows, count, totals = pc.idle_holders(DEMO_SERVERS, 51200, now=NOW)
        self.assertEqual(count, 1)
        self.assertEqual(totals, {"instances": 1, "vcpus": 1, "ram": 40960})
        row = rows[0]
        self.assertEqual(row["name"], "grian-ih-error")
        self.assertEqual(row["status"], "ERROR")
        self.assertEqual(row["fault"], "No valid host was found.")
        self.assertEqual(row["ram_percent"], 80.0)
        self.assertEqual(row["ram_text"], "40 GiB")
        self.assertEqual(row["since"], "2 days")

    def test_sorted_by_ram_and_bounded(self):
        servers = [
            _server(f"{i:08d}-0000-0000-0000-000000000000", "SHUTOFF", ram=r)
            for i, r in enumerate([512, 8192, 2048] * 5)
        ]
        servers.append(_server(ID1))  # running: not listed
        rows, count, totals = pc.idle_holders(servers, None, now=NOW)
        self.assertEqual(count, 15)
        self.assertEqual(len(rows), pc.IDLE_LIMIT)
        self.assertEqual([r["ram"] for r in rows[:5]], [8192] * 5)
        self.assertEqual(totals["ram"], 5 * (512 + 8192 + 2048))
        self.assertIsNone(rows[0]["ram_percent"])

    def test_no_updated_time(self):
        rows, _count, _totals = pc.idle_holders(
            [_server(ID3, "ERROR", updated=None)], 1000, now=NOW
        )
        self.assertEqual(rows[0]["since"], "")

    def test_offloaded_is_holding(self):
        # Nova's default count includes offloaded instances' flavors
        rows, count, totals = pc.idle_holders(
            [_server(ID3, "SHELVED_OFFLOADED", vcpus=2, ram=1024)],
            None,
            now=NOW,
        )
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]["status"], "SHELVED_OFFLOADED")
        self.assertEqual(totals, {"instances": 1, "vcpus": 2, "ram": 1024})

    def test_ram_limit(self):
        self.assertEqual(pc.ram_limit(DEMO_LIMITS), 51200)
        self.assertIsNone(pc.ram_limit({"maxTotalRAMSize": -1}))


class ForecastTests(base.TestCase):
    def row(self, key=pc.QUOTA_CORES, used=10, limit=20):
        return {"key": key, "used": used, "limit": limit}

    def test_days(self):
        # 2 vCPUs a day, 10 left: 5 days
        result = pc.forecast(self.row(), 2 / 86400, 400, 15)
        self.assertEqual(result["state"], "days")
        self.assertEqual(result["days"], 5)
        self.assertEqual(result["level"], pc.LEVEL_CRITICAL)

    def test_levels(self):
        def days_level(days):
            slope = 10 / days / 86400
            return pc.forecast(self.row(), slope, 400, 15)["level"]

        self.assertEqual(days_level(14), pc.LEVEL_CRITICAL)
        self.assertEqual(days_level(15), pc.LEVEL_WARNING)
        self.assertEqual(days_level(30), pc.LEVEL_WARNING)
        self.assertEqual(days_level(31), pc.LEVEL_OK)

    def test_rounds_up(self):
        result = pc.forecast(self.row(), 3 / 86400, 400, 15)
        self.assertEqual(result["days"], 4)

    def test_states(self):
        self.assertEqual(
            pc.forecast(self.row(limit=None), 1, 400, 15)["state"],
            "unlimited",
        )
        full = pc.forecast(self.row(used=20), 0, 400, 15)
        self.assertEqual(full["state"], "full")
        self.assertEqual(full["level"], pc.LEVEL_CRITICAL)
        self.assertEqual(
            pc.forecast(self.row(), 1, 400, 5)["state"], "short_retention"
        )
        self.assertEqual(
            pc.forecast(self.row(), 1, 54, 15)["state"], "no_history"
        )
        self.assertEqual(
            pc.forecast(self.row(), None, 400, 15)["state"], "no_data"
        )
        self.assertEqual(
            pc.forecast(self.row(), 1, None, 15)["state"], "no_data"
        )
        self.assertEqual(
            pc.forecast(self.row(), 0.0, 400, 15)["state"], "flat"
        )
        self.assertEqual(
            pc.forecast(self.row(), -1 / 86400, 400, 15)["state"], "flat"
        )
        self.assertEqual(
            pc.forecast(self.row(), 0.01 / 86400, 400, 15)["state"], "far"
        )

    def test_ram_flat_threshold(self):
        row = self.row(pc.QUOTA_RAM, used=1000, limit=51200)
        self.assertEqual(
            pc.forecast(row, 0.5 / 86400, 400, 15)["state"], "flat"
        )
        self.assertEqual(
            pc.forecast(row, 1024 / 86400, 400, 15)["state"], "days"
        )

    def test_lookback_is_clamped_to_retention(self):
        self.assertEqual(pc.forecast_lookback_days(15), 15)
        self.assertEqual(pc.forecast_lookback_days(400), 30)
        self.assertEqual(pc.forecast_lookback_days(0), 1)


class TrendTests(base.TestCase):
    def test_ranges(self):
        self.assertEqual(list(pc.TREND_RANGES), ["6h", "24h", "7d", "30d"])
        self.assertEqual(pc.trend_range("7d"), ("7d", "7d", "2h"))
        self.assertEqual(pc.trend_range("nope"), ("24h", "24h", "15m"))
        self.assertEqual(pc.range_days("6h"), 0.25)

    def test_points_per_range(self):
        counts = {
            key: len(pc.grid(window, step, NOW))
            for key, (window, step) in pc.TREND_RANGES.items()
        }
        self.assertEqual(counts, {"6h": 72, "24h": 96, "7d": 84, "30d": 90})

    def test_available_ranges(self):
        def enabled(retention):
            return [
                r["key"]
                for r in pc.available_ranges(retention)
                if r["enabled"]
            ]

        self.assertEqual(enabled(15), ["6h", "24h", "7d"])
        self.assertEqual(enabled(30), ["6h", "24h", "7d", "30d"])
        self.assertEqual(enabled(1), ["6h", "24h"])

    def test_grid_is_aligned(self):
        timestamps = pc.grid("1h", "15m", 3600 * 10 + 100)
        self.assertEqual(timestamps, [33300, 34200, 35100, 36000])

    def test_fill(self):
        timestamps, values = pc.fill(
            [[34200.2, 1.0], [36000, 3.0]], "1h", "15m", 36100
        )
        self.assertEqual(timestamps, [33300, 34200, 35100, 36000])
        self.assertEqual(values, [None, 1.0, None, 3.0])

    def test_last_value_and_summary(self):
        self.assertEqual(pc.last_value([1, 2, None]), 2)
        self.assertIsNone(pc.last_value([None]))
        self.assertEqual(
            pc.series_summary([None, 3, 1, 2]),
            {"latest": 2, "min": 1, "max": 3},
        )
        self.assertIsNone(pc.series_summary([None]))


class AssessTests(base.TestCase):
    def assess(self, cpu, mem, vcpus=4, ram=1000, p95=None, swap=0):
        stats = {
            "cpu_avg": cpu * vcpus,
            "cpu_p95": (p95 if p95 is not None else cpu) * vcpus,
            "memory_avg": mem * ram,
            "swap": swap,
        }
        return pc.assess(stats, vcpus, ram, RS)

    def test_undersized_cpu(self):
        result = self.assess(0.88, 0.2)
        self.assertEqual(result["assessment"], pc.UNDERSIZED)
        self.assertEqual(result["notes"], ["CPU busy"])
        self.assertIn("88% of its 4 vCPUs", result["hint"])
        self.assertIn("more vCPUs", result["hint"])

    def test_threshold_boundaries(self):
        self.assertEqual(self.assess(0.8, 0.5)["assessment"], pc.UNDERSIZED)
        self.assertEqual(self.assess(0.79, 0.5)["assessment"], pc.RIGHT_SIZED)
        self.assertEqual(self.assess(0.5, 0.9)["assessment"], pc.UNDERSIZED)
        # p95 exactly at the low threshold is not low
        self.assertEqual(
            self.assess(0.05, 0.1, p95=0.1)["assessment"], pc.RIGHT_SIZED
        )

    def test_undersized_memory(self):
        result = self.assess(0.3, 0.95)
        self.assertEqual(result["assessment"], pc.UNDERSIZED)
        self.assertEqual(result["notes"], ["memory full"])
        self.assertEqual(result["hint"], "Resize to a flavor with more RAM.")

    def test_any_swap_is_undersized(self):
        result = self.assess(0.3, 0.5, swap=12.5)
        self.assertEqual(result["assessment"], pc.UNDERSIZED)
        self.assertTrue(result["swap"])
        self.assertEqual(result["notes"], ["swapping"])

    def test_oversized(self):
        result = self.assess(0.02, 0.1, p95=0.05)
        self.assertEqual(result["assessment"], pc.OVERSIZED)
        self.assertIn("CPU peaks at 5%", result["hint"])
        self.assertIn("up to 3 vCPUs", result["hint"])
        single = self.assess(0.02, 0.1, vcpus=1)
        self.assertNotIn("up to", single["hint"])

    def test_oversized_needs_both(self):
        # CPU idle but memory used: right-sized
        self.assertEqual(self.assess(0.02, 0.5)["assessment"], pc.RIGHT_SIZED)
        # memory unused but CPU busy at peaks
        result = self.assess(0.3, 0.1, p95=0.5)
        self.assertEqual(result["assessment"], pc.RIGHT_SIZED)
        self.assertEqual(result["notes"], ["memory mostly unused"])
        self.assertIn("less RAM", result["hint"])

    def test_right_sized(self):
        result = self.assess(0.5, 0.5)
        self.assertEqual(result["assessment"], pc.RIGHT_SIZED)
        self.assertEqual(result["notes"], [])
        self.assertEqual(result["hint"], "The flavor fits the workload.")

    def test_no_data(self):
        for stats in (None, {}, {"cpu_avg": 1.0}, {"memory_avg": 10}):
            result = pc.assess(stats, 4, 1000, RS)
            self.assertIsNone(result["assessment"])
        self.assertIsNone(
            pc.assess({"cpu_avg": 1, "memory_avg": 1}, None, 1, RS)[
                "assessment"
            ]
        )

    def test_thresholds_are_configurable(self):
        thresholds = dict(RS, cpu_high=95)
        stats = {"cpu_avg": 3.52, "cpu_p95": 3.57, "memory_avg": 500}
        self.assertEqual(
            pc.assess(stats, 4, 1000, thresholds)["assessment"],
            pc.RIGHT_SIZED,
        )

    def test_ratios(self):
        result = self.assess(0.5, 0.25, p95=0.75)
        self.assertAlmostEqual(result["cpu"], 0.5)
        self.assertAlmostEqual(result["cpu_p95"], 0.75)
        self.assertAlmostEqual(result["memory"], 0.25)


class DiskTests(base.TestCase):
    def test_root_disk_is_the_first_device(self):
        metrics = {
            "disks": {
                "vdb": {"used": 1, "capacity": 10},
                "vda": {"used": 5, "capacity": 20},
                "sda": {"used": 1, "capacity": None},
            }
        }
        self.assertEqual(pc.root_disk(metrics), ("vda", 5, 20))
        self.assertEqual(pc.root_disk({}), (None, None, None))
        self.assertEqual(pc.root_disk(None), (None, None, None))

    def test_write_latency_needs_real_writes(self):
        mib = 1024 * 1024
        metrics = {
            "disks": {
                "vda": {"write": 0.2 * mib, "latency": 300},
                "vdb": {"write": 5 * mib, "latency": 34},
                "vdc": {"write": 9 * mib, "latency": 12},
                "vdd": {"write": None, "latency": 900},
                "vde": {"write": 9 * mib, "latency": None},
            }
        }
        self.assertEqual(pc.write_latency(metrics, 1), 34)
        self.assertEqual(pc.write_latency(metrics, 0.1), 300)
        self.assertIsNone(pc.write_latency(metrics, 10))
        self.assertIsNone(pc.write_latency(None, 1))


class RightsizingRowsTests(base.TestCase):
    def test_demo_project(self):
        running = pc.running_instances(DEMO_SERVERS, DEMO_SNAPSHOT)
        self.assertEqual([r["id"] for r in running], [ID1, ID2])
        rows = pc.rightsizing_rows(running, DEMO_STATS, DEMO_SNAPSHOT, RS)
        cpu2, disk = rows
        # grian-ih-cpu-2 uses 3.5 of its 4 vCPUs: undersized, 54 h of data
        self.assertEqual(cpu2["name"], "grian-ih-cpu-2")
        self.assertEqual(cpu2["assessment"], pc.UNDERSIZED)
        self.assertAlmostEqual(cpu2["cpu"], 0.881)
        self.assertEqual(cpu2["cpu_level"], "busy")
        self.assertTrue(cpu2["provisional"])
        self.assertEqual(cpu2["hours"], 54)
        self.assertEqual(cpu2["cpu_used_text"], "3.52")
        self.assertAlmostEqual(cpu2["disk_ratio"], 31522816 / 1073741824)
        self.assertEqual(cpu2["disk_capacity_text"], "1 GiB")
        # grian-ih-disk: CPU fits, memory mostly unused
        self.assertEqual(disk["assessment"], pc.RIGHT_SIZED)
        self.assertEqual(disk["notes"], ["memory mostly unused"])
        self.assertEqual(disk["memory_level"], "low")
        self.assertEqual(disk["memory_used_text"], "47.3 MiB")

        summary = pc.rightsizing_summary(rows)
        self.assertEqual(summary["vcpus"], 5)
        self.assertAlmostEqual(summary["cpu_used"], 4.162)
        self.assertEqual(summary["ram"], 768)
        self.assertEqual(summary["counts"], {"under": 1, "over": 0, "ok": 1})
        self.assertTrue(summary["provisional"])
        self.assertEqual(summary["hours"], 54)
        self.assertEqual(summary["assessed"], 2)
        self.assertEqual(summary["no_data"], 0)

    def test_order(self):
        running = [
            {
                "id": f"i{n}",
                "name": f"vm-{n}",
                "flavor": None,
                "vcpus": 1,
                "ram": 1000,
                "disk": None,
            }
            for n in range(6)
        ]
        stats = {
            "i0": {"cpu_avg": 0.5, "cpu_p95": 0.6, "memory_avg": 500},
            "i1": {"cpu_avg": 0.85, "cpu_p95": 0.9, "memory_avg": 500},
            "i2": {"cpu_avg": 0.95, "cpu_p95": 1, "memory_avg": 500},
            "i3": {"cpu_avg": 0.05, "cpu_p95": 0.06, "memory_avg": 100},
            "i4": {"cpu_avg": 0.01, "cpu_p95": 0.02, "memory_avg": 100},
            "i5": {},
        }
        rows = pc.rightsizing_rows(running, stats, {}, RS)
        self.assertEqual(
            [r["name"] for r in rows],
            ["vm-2", "vm-1", "vm-4", "vm-3", "vm-0", "vm-5"],
        )
        self.assertIsNone(rows[-1]["assessment"])
        self.assertTrue(rows[-1]["provisional"])
        summary = pc.rightsizing_summary(rows)
        self.assertEqual(summary["no_data"], 1)
        self.assertEqual(summary["assessed"], 5)
        # no hours of history: provisional
        self.assertTrue(summary["provisional"])

    def test_full_history_is_final(self):
        running = pc.running_instances(DEMO_SERVERS, DEMO_SNAPSHOT)
        stats = {k: dict(v, hours=168.0) for k, v in DEMO_STATS.items()}
        rows = pc.rightsizing_rows(running, stats, DEMO_SNAPSHOT, RS)
        self.assertFalse(any(r["provisional"] for r in rows))
        self.assertFalse(pc.rightsizing_summary(rows)["provisional"])

    def test_running_instances_without_nova(self):
        running = pc.running_instances(None, DEMO_SNAPSHOT)
        self.assertEqual(
            sorted((r["name"], r["vcpus"], r["ram"]) for r in running),
            [("grian-ih-cpu-2", 4.0, 256.0), ("grian-ih-disk", 1.0, 512.0)],
        )
        self.assertIsNone(running[0]["flavor"])

    def test_running_instances_flavor_fallback(self):
        servers = [_server(ID1, vcpus=None, ram=None)]
        running = pc.running_instances(servers, DEMO_SNAPSHOT)
        self.assertEqual(
            (running[0]["vcpus"], running[0]["ram"]), (4.0, 256.0)
        )
        # an instance without metrics or flavor details
        running = pc.running_instances(
            [_server(ID3, vcpus=None, ram=None)], {}
        )
        self.assertIsNone(running[0]["vcpus"])

    def test_empty_summary(self):
        summary = pc.rightsizing_summary([])
        self.assertIsNone(summary["cpu_percent"])
        self.assertIsNone(summary["hours"])
        self.assertEqual(summary["assessed"], 0)


class SymptomTests(base.TestCase):
    def test_demo_project(self):
        # the VM's 8 cores are shared with busy instances of other projects
        cpu2 = pc.symptoms(DEMO_SERVERS[0], DEMO_SNAPSHOT[ID1], THRESHOLDS)
        self.assertEqual(
            [(i["kind"], i["severity"], i["text"]) for i in cpu2],
            [(pc.ISSUE_CPU_WAIT, pc.SEVERITY_WARNING, "CPU wait 13%")],
        )
        disk = pc.symptoms(DEMO_SERVERS[1], DEMO_SNAPSHOT[ID2], THRESHOLDS)
        # 855 MiB/s of writes at 2.7 ms: not slow
        self.assertEqual([i["text"] for i in disk], ["CPU wait 10%"])
        error = pc.symptoms(DEMO_SERVERS[2], None, THRESHOLDS)
        self.assertEqual(
            [(i["kind"], i["severity"]) for i in error],
            [(pc.ISSUE_ERROR, pc.SEVERITY_CRITICAL)],
        )

    def test_critical_and_sorted(self):
        metrics = {
            "vcpus": 2,
            "cpu_wait": 0.5,
            "swap": 12,
            "disks": {"vda": {"write": 5 * 1024 * 1024, "latency": 34}},
        }
        issues = pc.symptoms(_server(ID1), metrics, THRESHOLDS)
        self.assertEqual(
            [(i["kind"], i["severity"]) for i in issues],
            [
                (pc.ISSUE_CPU_WAIT, pc.SEVERITY_CRITICAL),
                (pc.ISSUE_SWAP, pc.SEVERITY_CRITICAL),
                (pc.ISSUE_DISK, pc.SEVERITY_WARNING),
            ],
        )
        self.assertEqual(issues[1]["text"], "swap 12 MiB/s")
        metrics["disks"]["vda"]["latency"] = 150
        issues = pc.symptoms(_server(ID1), metrics, THRESHOLDS)
        self.assertEqual(issues[2]["text"], "disk latency 150 ms")
        self.assertEqual(issues[2]["severity"], pc.SEVERITY_CRITICAL)

    def test_small_swap_is_a_warning(self):
        issues = pc.symptoms(_server(ID1), {"swap": 0.05}, THRESHOLDS)
        self.assertEqual(issues[0]["severity"], pc.SEVERITY_WARNING)
        self.assertEqual(issues[0]["text"], "swap 0.05 MiB/s")
        self.assertEqual(
            pc.symptoms(_server(ID1), {"swap": 0}, THRESHOLDS), []
        )

    def test_cpu_wait_uses_the_flavor_without_metric_vcpus(self):
        server = _server(ID1, vcpus=4)
        issues = pc.symptoms(server, {"cpu_wait": 0.4}, THRESHOLDS)
        self.assertEqual(issues[0]["text"], "CPU wait 10%")

    def test_stopped_instances_ignore_old_metrics(self):
        server = _server(ID1, "SHUTOFF")
        self.assertEqual(
            pc.symptoms(server, {"vcpus": 1, "cpu_wait": 1}, THRESHOLDS), []
        )

    def test_metrics_only(self):
        issues = pc.symptoms(None, {"vcpus": 1, "cpu_wait": 0.3}, THRESHOLDS)
        self.assertEqual(issues[0]["severity"], pc.SEVERITY_CRITICAL)
        self.assertEqual(pc.symptoms(None, None, THRESHOLDS), [])

    def test_advice(self):
        def issues(*kinds):
            return [{"kind": k, "severity": 2, "text": ""} for k in kinds]

        self.assertIsNone(pc.advice_kind([]))
        self.assertEqual(
            pc.advice_kind(issues(pc.ISSUE_DISK, pc.ISSUE_SWAP)), pc.ISSUE_SWAP
        )
        self.assertEqual(
            pc.advice_kind(issues(pc.ISSUE_SWAP, pc.ISSUE_CPU_WAIT)),
            pc.ISSUE_CPU_WAIT,
        )
        self.assertEqual(
            pc.advice_kind(issues(pc.ISSUE_CPU_WAIT, pc.ISSUE_ERROR)),
            pc.ISSUE_ERROR,
        )


class AttentionRowsTests(base.TestCase):
    def test_demo_project(self):
        rows, count = pc.attention_rows(
            DEMO_SERVERS, DEMO_SNAPSHOT, THRESHOLDS
        )
        self.assertEqual(count, 3)
        self.assertEqual(
            [(r["name"], r["severity"], r["advice"]) for r in rows],
            [
                ("grian-ih-error", 3, pc.ISSUE_ERROR),
                ("grian-ih-cpu-2", 2, pc.ISSUE_CPU_WAIT),
                ("grian-ih-disk", 2, pc.ISSUE_CPU_WAIT),
            ],
        )
        self.assertEqual(rows[0]["fault"], "No valid host was found.")

    def test_without_metrics(self):
        rows, count = pc.attention_rows(DEMO_SERVERS, None, THRESHOLDS)
        self.assertEqual([r["name"] for r in rows], ["grian-ih-error"])
        self.assertEqual(count, 1)

    def test_without_nova(self):
        rows, count = pc.attention_rows(None, DEMO_SNAPSHOT, THRESHOLDS)
        self.assertEqual(count, 2)
        self.assertEqual({r["status"] for r in rows}, {None})
        self.assertEqual(
            {r["name"] for r in rows}, {"grian-ih-cpu-2", "grian-ih-disk"}
        )

    def test_bounded(self):
        servers = [
            _server(f"{i:08d}-0000-0000-0000-000000000000", "ERROR")
            for i in range(40)
        ]
        rows, count = pc.attention_rows(servers, {}, THRESHOLDS)
        self.assertEqual(count, 40)
        self.assertEqual(len(rows), pc.TABLE_LIMIT)
        rows, _count = pc.attention_rows(servers, {}, THRESHOLDS, limit=3)
        self.assertEqual(len(rows), 3)


class SampleDataTests(base.TestCase):
    """The fake project exercises every state of the page."""

    def setUp(self):
        super().setUp()
        inventory = fake.get_inventory(now=NOW)
        self.servers = inventory["servers"]
        self.limits = inventory["limits"]
        self.snapshot = fake.get_snapshot()["instances"]
        self.stats = fake.get_rightsizing()["instances"]

    def test_quota(self):
        rows = pc.quota_rows(self.servers, self.limits, THRESHOLDS)
        self.assertEqual(
            [(r["used"], r["limit"], r["level"]) for r in rows],
            [(18, 25, "ok"), (43, 50, "warning"), (84992, 102400, "warning")],
        )
        self.assertEqual(pc.highest_quota(rows)["key"], pc.QUOTA_CORES)

    def test_assessments(self):
        running = pc.running_instances(self.servers, self.snapshot)
        rows = pc.rightsizing_rows(running, self.stats, self.snapshot, RS)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(
            sorted(
                n for n, r in by_name.items() if r["assessment"] == "under"
            ),
            ["api-01", "api-02", "api-03", "db-01"],
        )
        self.assertEqual(
            sorted(n for n, r in by_name.items() if r["assessment"] == "over"),
            ["batch-01", "batch-02", "cache-01"],
        )
        self.assertTrue(by_name["worker-new"]["provisional"])
        self.assertIsNone(by_name["probe-01"]["assessment"])
        self.assertEqual(rows[-1]["name"], "probe-01")

    def test_attention(self):
        rows, count = pc.attention_rows(
            self.servers, self.snapshot, THRESHOLDS
        )
        self.assertEqual(
            [r["name"] for r in rows],
            ["api-03", "old-test", "api-02", "db-01"],
        )
        self.assertEqual(count, 4)


class ConfigTests(base.TestCase):
    def test_defaults_are_the_mockup_values(self):
        self.assertEqual(
            config.get_project_rightsizing_thresholds(),
            {
                "cpu_high": 80,
                "cpu_low": 10,
                "memory_high": 90,
                "memory_low": 30,
            },
        )
        thresholds = config.get_project_compute_thresholds()
        self.assertEqual(thresholds["quota_warning"], 75)
        self.assertEqual(thresholds["quota_critical"], 90)
        self.assertEqual(thresholds["idle_ram_critical"], 25)
        self.assertEqual(thresholds["cpu_wait_warning"], 5)
        self.assertEqual(thresholds["disk_latency_critical"], 100)
        self.assertEqual(thresholds["disk_write_floor"], 1)

    @override_settings(
        GRIAN_PLUGIN={
            "project_rightsizing_thresholds": {"cpu_high": 95},
            "project_compute_thresholds": {"quota_warning": 50},
        }
    )
    def test_overrides(self):
        self.assertEqual(
            config.get_project_rightsizing_thresholds()["cpu_high"], 95
        )
        self.assertEqual(
            config.get_project_compute_thresholds()["quota_warning"], 50
        )

    @override_settings(
        GRIAN_PLUGIN={"project_rightsizing_thresholds": {"memory_low": 95}}
    )
    def test_low_above_high(self):
        with self.assertRaisesRegex(config.ConfigurationError, "memory_low"):
            config.get_project_rightsizing_thresholds()

    @override_settings(
        GRIAN_PLUGIN={"project_compute_thresholds": {"quota_warning": 95}}
    )
    def test_warning_above_critical(self):
        with self.assertRaisesRegex(
            config.ConfigurationError, "quota_critical"
        ):
            config.get_project_compute_thresholds()

    @override_settings(
        GRIAN_PLUGIN={"project_compute_thresholds": {"bogus": 1}}
    )
    def test_unknown_key(self):
        with self.assertRaisesRegex(config.ConfigurationError, "bogus"):
            config.get_project_compute_thresholds()
