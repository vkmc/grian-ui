#
# SPDX-License-Identifier: Apache-2.0

import datetime

from grian_ui import project_usage as pu
from grian_ui.datasource import fake
from grian_ui.datasource import fake_project_usage
from grian_ui_tests.unit import base


UTC = datetime.timezone.utc
START = datetime.datetime(2026, 11, 6, tzinfo=UTC)
END = datetime.datetime(2026, 11, 20, 15, 40, tzinfo=UTC)
HOURS = (END - START).total_seconds() / 3600


class FakeProjectUsageTests(base.TestCase):
    def setUp(self):
        super().setUp()
        self.report = fake.get_client().get_project_usage(None, START, END)

    def test_format(self):
        self.assertEqual(
            set(self.report),
            {
                "instances",
                "volume_gb_hours",
                "snapshot_gb_hours",
                "hourly",
                "start",
                "end",
            },
        )
        self.assertEqual(set(self.report["hourly"]), set(pu.HOURLY_FIELDS))
        for usage in self.report["instances"].values():
            for field in pu.SUM_FIELDS + ("name", "vcpus", "memory_mib"):
                self.assertIn(field, usage)

    def test_deterministic(self):
        again = fake_project_usage.get_usage(START, END)
        self.assertEqual(self.report, again)

    def test_instances_and_storage(self):
        instances = self.report["instances"]
        self.assertEqual(len(instances), 3)
        web = instances["d414fbd9-66a6-485c-bec7-8191c2fa4221"]
        self.assertAlmostEqual(web["hours"], HOURS)
        self.assertAlmostEqual(web["vcpu_hours"], 4 * HOURS)
        self.assertLess(web["cpu_hours"], web["vcpu_hours"])
        # ran 30 hours, then deleted
        batch = instances["6a0a1f43-6b0c-4f1e-9a43-3c2f1d1e8b11"]
        self.assertAlmostEqual(batch["hours"], 30)
        self.assertAlmostEqual(self.report["volume_gb_hours"], 20 * HOURS)
        self.assertAlmostEqual(self.report["snapshot_gb_hours"], 5 * HOURS)

    def test_hourly_adds_up_to_the_totals(self):
        for field, total in (
            ("vcpu_hours", "vcpu_hours"),
            ("cpu_hours", "cpu_hours"),
            ("ram_gb_hours_used", "ram_gb_hours_used"),
        ):
            with self.subTest(field=field):
                self.assertAlmostEqual(
                    sum(v for _t, v in self.report["hourly"][field]),
                    sum(u[total] for u in self.report["instances"].values()),
                )
        net = sum(
            u["net_in"] + u["net_out"]
            for u in self.report["instances"].values()
        )
        self.assertAlmostEqual(
            sum(v for _t, v in self.report["hourly"]["net_bytes"]), net
        )
        # hourly points start at the start of the period, on the hour
        first = self.report["hourly"]["vcpu_hours"][0][0]
        self.assertEqual(first, START.timestamp())

    def test_weekends_are_quieter(self):
        hourly = dict(self.report["hourly"]["cpu_hours"])
        saturday = datetime.datetime(2026, 11, 7, 12, tzinfo=UTC).timestamp()
        monday = datetime.datetime(2026, 11, 9, 12, tzinfo=UTC).timestamp()
        self.assertLess(hourly[saturday], hourly[monday])

    def test_empty_period(self):
        report = fake_project_usage.get_usage(START, START)
        self.assertEqual(report["instances"], {})
        self.assertEqual(report["volume_gb_hours"], 0.0)

    def test_servers(self):
        servers = fake_project_usage.get_servers()
        statuses = {s["name"]: s["status"] for s in servers}
        self.assertEqual(statuses["grian-ih-error"], "ERROR")
        self.assertEqual(statuses["build-01"], "SHUTOFF")
        # new dicts each time
        servers[0]["name"] = "changed"
        self.assertNotEqual(
            fake_project_usage.get_servers()[0]["name"], "changed"
        )
