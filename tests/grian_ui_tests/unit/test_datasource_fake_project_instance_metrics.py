#
# SPDX-License-Identifier: Apache-2.0

from grian_ui import project_instance_metrics as pim
from grian_ui import timeseries
from grian_ui.datasource import fake
from grian_ui.datasource import fake_project_instance_metrics as fake_data
from grian_ui_tests.unit import base


AT = 1_700_000_000
WEB = "5d0c3f6e-1b2a-4c8d-9e7f-0a1b2c3d4e5f"
SHUTOFF = "2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d"
UNKNOWN = "00000000-0000-4000-8000-000000000000"


class FakeServersTests(base.TestCase):
    def test_every_state(self):
        servers = fake_data.servers("p1")
        self.assertEqual(
            sorted(s["status"] for s in servers),
            ["ACTIVE", "ACTIVE", "ERROR", "SHUTOFF"],
        )
        for server in servers:
            with self.subTest(server=server["name"]):
                self.assertEqual(server["project_id"], "p1")
                self.assertTrue(pim.is_instance_id(server["id"]))
                self.assertEqual(
                    set(server["flavor"]),
                    {"name", "vcpus", "ram_mib", "disk_gb"},
                )
                self.assertIsNotNone(server["created_at"])
        error = [s for s in servers if s["status"] == "ERROR"][0]
        self.assertEqual(error["fault"], "No valid host was found.")

    def test_server(self):
        self.assertEqual(fake_data.server("p2", WEB)["name"], "web-01")
        self.assertEqual(fake_data.server("p2", WEB)["project_id"], "p2")
        self.assertIsNone(fake_data.server("p2", UNKNOWN))

    def test_copies(self):
        fake_data.servers("p1")[0]["name"] = "changed"
        self.assertEqual(fake_data.servers("p1")[0]["name"], "web-01")


class FakeSeriesTests(base.TestCase):
    def get(self, group, instance_id=WEB, range_key="24h"):
        return fake.get_client().get_project_instance_metrics(
            'project="p1"', group, instance_id, range_key, "10m", at=AT
        )

    def test_every_group_on_the_range_grid(self):
        for range_key in pim.RANGES:
            grid = timeseries.range_grid(range_key, AT)
            for group in pim.GROUPS:
                with self.subTest(range_key=range_key, group=group):
                    series = self.get(group, range_key=range_key)
                    self.assertEqual(tuple(series), pim.SERIES[group])
                    for points in series.values():
                        self.assertEqual([t for t, _v in points], grid)
                        self.assertTrue(
                            all(v is not None and v >= 0 for _t, v in points)
                        )

    def test_deterministic(self):
        self.assertEqual(self.get("cpu"), self.get("cpu"))

    def test_allocation_matches_the_flavor(self):
        series = self.get("cpu")
        self.assertEqual({v for _t, v in series["vcpus"]}, {1})
        memory = self.get("memory")
        self.assertEqual({v for _t, v in memory["memory_alloc"]}, {2048})
        self.assertTrue(
            all(v < 2048 for _t, v in memory["memory"]),
        )

    def test_not_running_or_unknown_have_no_series(self):
        for instance_id in (SHUTOFF, UNKNOWN):
            self.assertEqual(
                self.get("disk", instance_id),
                {"disk_read": [], "disk_write": [], "disk_write_latency": []},
            )

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            self.get("gpu")
        with self.assertRaises(ValueError):
            self.get("cpu", instance_id="nope")
