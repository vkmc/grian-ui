#
# SPDX-License-Identifier: Apache-2.0

from django.test import override_settings

from grian_ui import config
from grian_ui import project_instance_metrics as pim
from grian_ui_tests.unit import base


ID = "c726b640-2a18-41e3-9fac-af2730c12410"
THRESHOLDS = dict(config.DEFAULT_PROJECT_INSTANCE_METRICS_THRESHOLDS)
FLAVOR = {"name": "m1.tiny", "vcpus": 1, "ram_mib": 512, "disk_gb": 1}


def _points(values, start=1_700_000_000, step=60):
    return [[start + i * step, v] for i, v in enumerate(values)]


class ValidateInstanceIdTests(base.TestCase):
    def test_valid(self):
        self.assertEqual(pim.validate_instance_id(ID), ID)
        self.assertTrue(pim.is_instance_id(ID))

    def test_invalid(self):
        for value in (
            "",
            None,
            42,
            "not-a-uuid",
            ID.upper(),
            f"{ID}\n",
            f" {ID}",
            f'{ID}"}} or vector(1)',
            f"{ID}|.*",
            ID.replace("-", ""),
            "</script><script>alert(1)</script>",
        ):
            with self.subTest(value=value):
                self.assertFalse(pim.is_instance_id(value))
                with self.assertRaises(ValueError):
                    pim.validate_instance_id(value)


class OwnershipTests(base.TestCase):
    def test_same_project(self):
        self.assertTrue(
            pim.instance_belongs_to_project({"project_id": "p1"}, "p1")
        )
        self.assertTrue(
            pim.instance_belongs_to_project({"tenant_id": "p1"}, "p1")
        )

    def test_other_project_or_unknown(self):
        self.assertFalse(
            pim.instance_belongs_to_project({"project_id": "p2"}, "p1")
        )
        self.assertFalse(pim.instance_belongs_to_project({"id": ID}, "p1"))
        self.assertFalse(pim.instance_belongs_to_project(None, "p1"))
        # never "no project" == "no project"
        self.assertFalse(
            pim.instance_belongs_to_project({"project_id": None}, None)
        )
        self.assertFalse(pim.instance_belongs_to_project({}, ""))


class RangeTests(base.TestCase):
    def test_retention_limits_the_ranges(self):
        self.assertEqual(pim.available_ranges(15), ["1h", "6h", "24h", "7d"])
        self.assertEqual(pim.available_ranges(7), ["1h", "6h", "24h", "7d"])
        self.assertEqual(pim.available_ranges(6), ["1h", "6h", "24h"])
        self.assertEqual(pim.available_ranges(1), ["1h", "6h", "24h"])

    def test_default_range(self):
        self.assertEqual(pim.default_range(["1h", "6h", "24h", "7d"]), "24h")
        self.assertEqual(pim.default_range(["1h", "6h"]), "6h")

    def test_rate_window_is_never_shorter_than_the_step(self):
        self.assertEqual(pim.rate_window("1h", "10m"), "10m")
        self.assertEqual(pim.rate_window("6h", "10m"), "10m")
        self.assertEqual(pim.rate_window("24h", "10m"), "15m")
        self.assertEqual(pim.rate_window("7d", "10m"), "1h")
        self.assertEqual(pim.rate_window("7d", "2h"), "2h")
        self.assertEqual(pim.rate_window("1h", "1m"), "1m")

    def test_coverage(self):
        now = 1_700_000_000
        # created 9 hours ago: inside 24h, not inside 6h
        self.assertEqual(
            pim.coverage(now - 9 * 3600 - 60, "24h", now),
            {"created_at": now - 9 * 3600 - 60, "hours": 9},
        )
        self.assertIsNone(pim.coverage(now - 9 * 3600, "6h", now))
        self.assertIsNone(pim.coverage(None, "24h", now))
        self.assertEqual(pim.coverage(now - 60, "1h", now)["hours"], 0)


class SeriesHelperTests(base.TestCase):
    def test_latest_and_summary(self):
        self.assertEqual(pim.latest([1, 2, None]), 2)
        self.assertIsNone(pim.latest([None, None]))
        self.assertIsNone(pim.latest(None))
        self.assertEqual(
            pim.summary([3, None, 1, 2]), {"latest": 2, "min": 1, "max": 3}
        )
        self.assertIsNone(pim.summary([None]))

    def test_percent(self):
        self.assertEqual(pim.percent(1, 4), 25.0)
        self.assertIsNone(pim.percent(None, 4))
        self.assertIsNone(pim.percent(1, 0))
        self.assertIsNone(pim.percent(1, None))

    def test_wait_percent_follows_a_resize(self):
        self.assertEqual(
            pim.wait_percent([0.1, 0.1, None, 0.2], [1, 2, 2, None], 4),
            [10.0, 5.0, None, 5.0],
        )
        self.assertEqual(pim.wait_percent([0.1], [], None), [None])

    def test_latency_needs_real_writes(self):
        mib = 1048576
        self.assertEqual(
            pim.latency_with_writes(
                [2.5, 31.0, None, 3.0], [900 * mib, 0.2 * mib, mib, None], 1
            ),
            [2.5, None, None, None],
        )


class FormatTests(base.TestCase):
    def test_bytes(self):
        self.assertEqual(pim.format_bytes(None), "–")
        self.assertEqual(pim.format_bytes(512), "512 B")
        self.assertEqual(pim.format_bytes(1536), "1.5 KiB")
        self.assertEqual(pim.format_bytes(1024 * 1024), "1 MiB")
        self.assertEqual(pim.format_bytes(898_200_000), "857 MiB")
        self.assertEqual(pim.format_bytes(12.5 * 1024**3), "12.5 GiB")

    def test_units(self):
        self.assertEqual(pim.format_rate(2048), "2 KiB/s")
        self.assertEqual(pim.format_rate(None), "–")
        self.assertEqual(pim.format_mib(512), "512 MiB")
        self.assertEqual(pim.format_mib(4096), "4 GiB")
        self.assertEqual(pim.format_mib(None), "–")
        self.assertEqual(pim.format_cores(0.6354), "0.64")
        self.assertEqual(pim.format_cores(12.34), "12.3")
        self.assertEqual(pim.format_cores(None), "–")
        self.assertEqual(pim.format_percent(9.64), "10%")
        self.assertEqual(pim.format_percent(0.44), "0.4%")
        self.assertEqual(pim.format_percent(None), "–")

    def test_format_value(self):
        self.assertEqual(pim.format_value("cores", 3.5), "3.50 vCPU")
        self.assertEqual(pim.format_value("percent", 12.0), "12%")
        self.assertEqual(pim.format_value("mib", 48), "48 MiB")
        self.assertEqual(pim.format_value("bytes_rate", 1024), "1 KiB/s")
        self.assertEqual(pim.format_value("ms", 2.734), "2.7 ms")
        self.assertEqual(pim.format_value("other", 2.5), "2.5")
        for unit in ("cores", "ms", "other"):
            self.assertEqual(pim.format_value(unit, None), "–")


class InstanceTests(base.TestCase):
    def test_state(self):
        self.assertEqual(pim.state_of("ACTIVE"), pim.STATE_ACTIVE)
        self.assertEqual(pim.state_of("error"), pim.STATE_ERROR)
        for status in ("SHUTOFF", "SUSPENDED", "SHELVED_OFFLOADED"):
            self.assertEqual(pim.state_of(status), pim.STATE_SHUTOFF)
        for status in ("BUILD", "PAUSED", "REBOOT", None):
            self.assertEqual(pim.state_of(status), pim.STATE_OTHER)

    def test_parse_time(self):
        self.assertEqual(pim.parse_time("2026-11-18T09:59:18Z"), 1794995958.0)
        self.assertEqual(
            pim.parse_time("2026-11-18T10:59:18+01:00"), 1794995958.0
        )
        # no zone: never guessed as local time
        self.assertIsNone(pim.parse_time("2026-11-18T09:59:18"))
        self.assertIsNone(pim.parse_time("yesterday"))
        self.assertIsNone(pim.parse_time(None))

    def test_addresses(self):
        addresses = {
            "private": [
                {"addr": "fdc4::1", "version": 6},
                {
                    "addr": "203.0.113.5",
                    "version": 4,
                    "OS-EXT-IPS:type": "floating",
                },
                {"addr": "10.0.0.54", "version": 4},
            ],
            "b-net": [],
        }
        self.assertEqual(
            pim.addresses_of(addresses),
            [
                {"network": "b-net", "ips": []},
                {
                    "network": "private",
                    "ips": ["10.0.0.54", "fdc4::1", "203.0.113.5"],
                },
            ],
        )
        self.assertEqual(pim.addresses_of(None), [])

    def test_embedded_flavor(self):
        # microversion 2.47+
        flavor = {"original_name": "m1.tiny", "vcpus": 1, "ram": 512}
        flavor["disk"] = 1
        self.assertEqual(pim.flavor_of(flavor), FLAVOR)

    def test_flavor_by_id(self):
        details = {"name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 1}
        self.assertEqual(pim.flavor_of({"id": "42"}, details), FLAVOR)

    def test_flavor_that_cannot_be_read(self):
        self.assertEqual(
            pim.flavor_of({"id": "42"}),
            {"name": "42", "vcpus": None, "ram_mib": None, "disk_gb": None},
        )
        self.assertIsNone(pim.flavor_of(None)["name"])


class TileTests(base.TestCase):
    def groups(self, **values):
        series = {
            "cpu": [0.5, 0.6354],
            "cpu_wait": [0.05, 0.0964],
            "vcpus": [1, 1],
            "memory": [47.0, 47.28],
            "memory_alloc": [512, 512],
            "swap": [0, 0],
            "disk_read": [0, 0],
            "disk_write": [9e8, 898_200_000],
            "disk_write_latency": [2.7, 2.734],
            "net_in": [2.4, 2.489],
            "net_out": [0.7, 0.7487],
        }
        series.update(values)
        return {
            group: {name: _points(series[name]) for name in names}
            for group, names in pim.SERIES.items()
        }

    def by_key(self, groups, flavor=FLAVOR):
        return {t["key"]: t for t in pim.tiles(groups, flavor, THRESHOLDS)}

    def test_real_example(self):
        # grian-ih-disk, read as demo through Aetos
        tiles = self.by_key(self.groups())
        self.assertEqual(
            list(tiles), ["cpu", "cpu_wait", "memory", "disk", "network"]
        )
        self.assertEqual(tiles["cpu"]["value"], "0.64")
        self.assertEqual(str(tiles["cpu"]["unit"]), "of 1 vCPU")
        self.assertEqual(str(tiles["cpu"]["sub"]), "64% of the flavor")
        self.assertEqual(tiles["cpu"]["level"], "")
        self.assertEqual(tiles["cpu_wait"]["value"], "10%")
        self.assertEqual(tiles["cpu_wait"]["level"], "warning")
        self.assertEqual(tiles["memory"]["value"], "47.3 MiB")
        self.assertEqual(
            str(tiles["memory"]["sub"]), "of 512 MiB (9%) | no swap"
        )
        self.assertEqual(tiles["disk"]["value"], "857 MiB/s")
        self.assertEqual(str(tiles["disk"]["sub"]), "written | 0 B/s read")
        self.assertEqual(tiles["network"]["value"], "2 B/s")
        self.assertEqual(str(tiles["network"]["sub"]), "in | 1 B/s out")

    def test_busy_cpu_and_critical_wait(self):
        tiles = self.by_key(
            self.groups(cpu=[3.523], cpu_wait=[1.0], vcpus=[4])
        )
        self.assertEqual(tiles["cpu"]["level"], "danger")
        self.assertEqual(str(tiles["cpu"]["sub"]), "88% of the flavor")
        self.assertEqual(tiles["cpu_wait"]["value"], "25%")
        self.assertEqual(tiles["cpu_wait"]["level"], "danger")

    def test_flavor_when_metrics_have_no_allocation(self):
        tiles = self.by_key(
            self.groups(vcpus=[None, None], memory_alloc=[None, None]),
            flavor=dict(FLAVOR, vcpus=2, ram_mib=1024),
        )
        self.assertEqual(str(tiles["cpu"]["unit"]), "of 2 vCPU")
        self.assertIn("of 1 GiB", str(tiles["memory"]["sub"]))

    def test_nothing_known(self):
        tiles = self.by_key(
            self.groups(
                cpu=[None],
                vcpus=[None],
                cpu_wait=[None],
                memory=[None],
                memory_alloc=[None],
            ),
            flavor={},
        )
        self.assertEqual(tiles["cpu"]["value"], "–")
        self.assertEqual(tiles["cpu"]["unit"], "")
        self.assertEqual(str(tiles["cpu"]["sub"]), "no vCPU count reported")
        self.assertEqual(tiles["cpu_wait"]["level"], "")
        self.assertEqual(str(tiles["memory"]["sub"]), "no swap")

    def test_swap(self):
        tiles = self.by_key(self.groups(swap=[0, 2.0]))
        self.assertEqual(tiles["memory"]["level"], "warning")
        self.assertIn("swapping 2 MiB/s", str(tiles["memory"]["sub"]))

    def test_swap_not_reported_is_not_no_swap(self):
        for swap in ([], [None, None]):
            with self.subTest(swap=swap):
                tiles = self.by_key(self.groups(swap=swap))
                self.assertEqual(
                    str(tiles["memory"]["sub"]),
                    "of 512 MiB (9%) | swap not reported",
                )
                self.assertEqual(tiles["memory"]["level"], "")
        tiles = self.by_key(self.groups(swap=[None, 0.0]))
        self.assertEqual(
            str(tiles["memory"]["sub"]), "of 512 MiB (9%) | no swap"
        )

    def test_missing_groups_are_skipped(self):
        groups = self.groups()
        del groups[pim.GROUP_DISK]
        del groups[pim.GROUP_CPU]
        self.assertEqual(list(self.by_key(groups)), ["memory", "network"])

    def test_level_of(self):
        self.assertEqual(pim.level_of(None, 5, 20), "")
        self.assertEqual(pim.level_of(4.9, 5, 20), "")
        self.assertEqual(pim.level_of(5, 5, 20), "warning")
        self.assertEqual(pim.level_of(20, 5, 20), "danger")


class ChartTests(base.TestCase):
    def charts(self, group, flavor=FLAVOR, **values):
        series = {name: _points(values.get(name, [])) for name in values}
        for name in pim.SERIES[group]:
            series.setdefault(name, [])
        return {
            c["key"]: c for c in pim.charts(group, series, flavor, THRESHOLDS)
        }

    def test_cpu(self):
        charts = self.charts(
            "cpu", cpu=[0.5, 0.6], cpu_wait=[0.1, 0.3], vcpus=[1, 1]
        )
        used, wait = charts["cpu"], charts["cpu_wait"]
        self.assertEqual(used["unit"], "cores")
        self.assertEqual(used["max"], 1.15)
        self.assertEqual(
            [d["label"] for d in used["datasets"]],
            ["Used", "vCPUs in flavor (1)"],
        )
        self.assertTrue(used["datasets"][1]["dash"])
        self.assertTrue(used["has_data"])
        self.assertEqual(
            used["summary"],
            [
                {
                    "label": "Used",
                    "latest": "0.60 vCPU",
                    "min": "0.50 vCPU",
                    "max": "0.60 vCPU",
                }
            ],
        )
        self.assertEqual(wait["datasets"][0]["values"], [10.0, 30.0])
        self.assertEqual(
            [d["label"] for d in wait["datasets"][1:]],
            ["Warning 5%", "Critical 20%"],
        )
        self.assertEqual(wait["datasets"][1]["values"], [5, 5])
        # room above the highest value, and above the critical line
        self.assertEqual(wait["max"], 35)

    def test_cpu_reference_from_the_flavor(self):
        charts = self.charts(
            "cpu", flavor=dict(FLAVOR, vcpus=4), cpu=[1.0, None]
        )
        self.assertEqual(charts["cpu"]["datasets"][1]["values"], [4, 4])
        self.assertEqual(charts["cpu_wait"]["max"], 25)
        self.assertFalse(charts["cpu_wait"]["has_data"])

    def test_without_data(self):
        charts = self.charts("cpu", flavor={}, cpu=[None, None])
        self.assertFalse(charts["cpu"]["has_data"])
        self.assertIsNone(charts["cpu"]["max"])
        self.assertEqual(
            charts["cpu"]["datasets"][1]["label"], "vCPUs in flavor"
        )

    def test_memory(self):
        chart = self.charts(
            "memory", memory=[47.0, 48.0], memory_alloc=[512, 512]
        )["memory"]
        self.assertEqual(chart["max"], 563.2)
        self.assertEqual(chart["datasets"][1]["label"], "Flavor RAM (512 MiB)")
        self.assertFalse(chart["swapped"])
        chart = self.charts("memory", flavor={}, memory=[47.0], swap=[0, 3.0])[
            "memory"
        ]
        self.assertTrue(chart["swapped"])
        self.assertIn("swapped in this range (up to 3 MiB/s)", chart["note"])
        self.assertEqual(chart["datasets"][1]["label"], "Flavor RAM")

    def test_disk(self):
        mib = 1048576
        charts = self.charts(
            "disk",
            disk_read=[0, 0],
            disk_write=[900 * mib, 0.1 * mib],
            disk_write_latency=[2.7, 31.0],
        )
        self.assertEqual(
            [d["label"] for d in charts["disk"]["datasets"]],
            ["Read", "Write"],
        )
        self.assertEqual(
            [d["color"] for d in charts["disk"]["datasets"]],
            ["blue", "magenta"],
        )
        latency = charts["disk_latency"]
        # 31 ms from a handful of writes is hidden
        self.assertEqual(latency["datasets"][0]["values"], [2.7, None])
        self.assertEqual(
            [d["label"] for d in latency["datasets"][1:]],
            ["Warning 20 ms", "Critical 100 ms"],
        )
        self.assertFalse(latency["too_few_writes"])
        self.assertIn("at least 1 MiB/s", latency["note"])

    def test_too_few_writes(self):
        latency = self.charts(
            "disk", disk_write=[1000, 2000], disk_write_latency=[31.0, 12.0]
        )["disk_latency"]
        self.assertFalse(latency["has_data"])
        self.assertTrue(latency["too_few_writes"])
        latency = self.charts("disk", disk_write=[0, 0])["disk_latency"]
        self.assertFalse(latency["too_few_writes"])

    def test_network(self):
        chart = self.charts("network", net_in=[2.5], net_out=[0.75])["network"]
        self.assertEqual(
            [(d["label"], d["color"]) for d in chart["datasets"]],
            [("Inbound", "blue"), ("Outbound", "magenta")],
        )
        self.assertIn("Network page", chart["note"])

    def test_unknown_group(self):
        with self.assertRaises(ValueError):
            pim.charts("gpu", {}, FLAVOR, THRESHOLDS)


class ConfigTests(base.TestCase):
    def test_defaults(self):
        self.assertEqual(
            config.get_project_instance_metrics_thresholds(), THRESHOLDS
        )
        self.assertEqual(
            config.get_project_instance_metrics_rate_window(), "10m"
        )

    def test_threshold_overrides(self):
        plugin = {
            "datasource": "fake",
            "project_instance_metrics_thresholds": {"cpu_wait_warning": 2},
        }
        with override_settings(GRIAN_PLUGIN=plugin):
            self.assertEqual(
                config.get_project_instance_metrics_thresholds()[
                    "cpu_wait_warning"
                ],
                2,
            )
        plugin["project_instance_metrics_thresholds"] = {
            "cpu_wait_warning": 30
        }
        with override_settings(GRIAN_PLUGIN=plugin):
            with self.assertRaises(config.ConfigurationError):
                config.get_project_instance_metrics_thresholds()

    def test_rate_window(self):
        for value, ok in (
            ("5m", True),
            ("1h", True),
            ("6h", True),
            ("7h", False),
            ("0m", False),
            ("05m", False),
            ("10s", False),
            ("m", False),
            ("10m\n", False),
            ("１０m", False),
            (10, False),
        ):
            plugin = {
                "datasource": "fake",
                "project_instance_metrics_rate_window": value,
            }
            with (
                self.subTest(value=value),
                override_settings(GRIAN_PLUGIN=plugin),
            ):
                if ok:
                    self.assertEqual(
                        config.get_project_instance_metrics_rate_window(),
                        value,
                    )
                else:
                    with self.assertRaises(config.ConfigurationError):
                        config.get_project_instance_metrics_rate_window()


class GroupTests(base.TestCase):
    def test_every_group_has_series_and_a_label(self):
        self.assertEqual(set(pim.SERIES), set(pim.GROUPS))
        self.assertEqual(set(pim.GROUP_LABELS), set(pim.GROUPS))
        self.assertIn(pim.DEFAULT_RANGE, pim.RANGES)
        self.assertIn(pim.TILE_RANGE, pim.RANGES)
