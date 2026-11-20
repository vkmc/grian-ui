#
# SPDX-License-Identifier: Apache-2.0

import sys
import types

from unittest import mock

from django.test import override_settings

from grian_ui import project_compute as pc
from grian_ui import project_compute_inventory as inventory
from grian_ui.datasource import fake
from grian_ui.datasource import fake_project_compute
from grian_ui_tests.unit import base


ID1 = "d414fbd9-66a6-485c-bec7-8191c2fa4221"
ID3 = "133a85dc-be0f-44e3-8f4f-475b79e704c6"


def _flavor(fid, name, vcpus, ram, disk=1):
    return types.SimpleNamespace(
        id=fid, name=name, vcpus=vcpus, ram=ram, disk=disk
    )


def _server(sid, name, status, flavor_id, fault=None, updated=None):
    raw = types.SimpleNamespace(updated=updated)
    server = types.SimpleNamespace(
        id=sid,
        name=name,
        status=status,
        flavor={"id": flavor_id, "links": []},
        _apiresource=raw,
    )
    if fault is not None:
        server.fault = fault
    return server


class CompactTests(base.TestCase):
    def test_server_with_flavor_id(self):
        flavors = {"f1": _flavor("f1", "grian.toobig", 1, 40960)}
        server = _server(
            ID3,
            "grian-ih-error",
            "ERROR",
            "f1",
            fault={"code": 500, "message": "No valid host was found. "},
            updated="2026-11-18T09:59:35Z",
        )
        self.assertEqual(
            inventory.compact_server(server, flavors),
            {
                "id": ID3,
                "name": "grian-ih-error",
                "status": "ERROR",
                "flavor": "grian.toobig",
                "vcpus": 1,
                "ram": 40960,
                "disk": 1,
                "fault": "No valid host was found.",
                "updated": 1794995975.0,
            },
        )

    def test_embedded_flavor(self):
        # microversion 2.47+: no id, the flavor itself
        server = _server(ID1, "vm", "ACTIVE", None)
        server.flavor = {
            "original_name": "grian.cpu4",
            "vcpus": 4,
            "ram": 256,
            "disk": 1,
        }
        row = inventory.compact_server(server, {})
        self.assertEqual(
            (row["flavor"], row["vcpus"], row["ram"]), ("grian.cpu4", 4, 256)
        )

    def test_unknown_flavor_and_no_fault(self):
        row = inventory.compact_server(
            _server(ID1, "vm", "ACTIVE", "gone"), {}
        )
        self.assertIsNone(row["flavor"])
        self.assertIsNone(row["vcpus"])
        self.assertIsNone(row["fault"])
        self.assertIsNone(row["updated"])

    def test_long_fault_is_cut(self):
        server = _server(
            ID1, "vm", "ERROR", "f", fault={"message": "x" * 1000}
        )
        row = inventory.compact_server(server, {})
        self.assertEqual(len(row["fault"]), inventory.FAULT_MAX_LENGTH)

    def test_timestamps(self):
        self.assertIsNone(inventory._epoch(None))
        self.assertIsNone(inventory._epoch("yesterday"))
        self.assertEqual(inventory._epoch("2026-11-18T09:59:35"), 1794995975.0)
        self.assertEqual(
            inventory._epoch("2026-11-18T10:59:35+01:00"), 1794995975.0
        )

    def test_limits(self):
        limits = inventory.compact_limits(
            {
                "maxTotalInstances": 10,
                "totalInstancesUsed": 3,
                "maxTotalCores": float("inf"),
                "totalCoresUsed": 6,
                "maxTotalRAMSize": 51200,
                "totalRAMUsed": 41728,
                "maxTotalKeypairs": 100,
            }
        )
        self.assertEqual(
            limits,
            {
                "maxTotalInstances": 10,
                "totalInstancesUsed": 3,
                "maxTotalCores": None,
                "totalCoresUsed": 6,
                "maxTotalRAMSize": 51200,
                "totalRAMUsed": 41728,
            },
        )
        self.assertEqual(set(inventory.compact_limits({}).values()), {None})


class NovaInventoryTests(base.TestCase):
    """Horizon's API layer, faked: the unit tests run without horizon."""

    def setUp(self):
        super().setUp()
        self.nova = mock.MagicMock()
        api = types.ModuleType("openstack_dashboard.api")
        api.nova = self.nova
        dashboard = types.ModuleType("openstack_dashboard")
        dashboard.api = api
        patcher = mock.patch.dict(
            sys.modules,
            {"openstack_dashboard": dashboard, "openstack_dashboard.api": api},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request = mock.MagicMock()
        self.request.user.project_id = "b8753cd7d01b4455b774b46991f36f83"
        client = self.nova.get_novaclient_with_locked_status.return_value
        self.servers = client.servers
        self.servers.list.return_value = [
            _server(ID1, "grian-ih-cpu-2", "ACTIVE", "cpu4"),
            _server(ID3, "grian-ih-error", "ERROR", "toobig"),
        ]
        self.nova.flavor_list.return_value = [
            _flavor("cpu4", "grian.cpu4", 4, 256)
        ]
        self.nova.flavor_get.return_value = _flavor(
            "toobig", "grian.toobig", 1, 40960
        )
        self.nova.tenant_absolute_limits.return_value = {
            "maxTotalInstances": 10,
            "totalInstancesUsed": 3,
            "maxTotalCores": 20,
            "totalCoresUsed": 6,
            "maxTotalRAMSize": 51200,
            "totalRAMUsed": 41728,
        }

    def test_read(self):
        result = inventory.NovaComputeInventory(self.request).read()
        # the project's servers, every page, with the user's token
        self.nova.get_novaclient_with_locked_status.assert_called_once_with(
            self.request
        )
        self.servers.list.assert_called_once_with(
            detailed=True,
            search_opts={"project_id": "b8753cd7d01b4455b774b46991f36f83"},
            limit=-1,
        )
        # the private flavor is read on its own
        self.nova.flavor_get.assert_called_once_with(self.request, "toobig")
        self.nova.tenant_absolute_limits.assert_called_once_with(
            self.request, reserved=True
        )
        self.assertEqual(
            [(s["name"], s["vcpus"], s["ram"]) for s in result["servers"]],
            [("grian-ih-cpu-2", 4, 256), ("grian-ih-error", 1, 40960)],
        )
        self.assertEqual(result["limits"]["totalRAMUsed"], 41728)

    def test_unreadable_flavor(self):
        self.nova.flavor_get.side_effect = RuntimeError("404")
        with self.assertLogs(inventory.LOG, "INFO"):
            result = inventory.NovaComputeInventory(self.request).read()
        self.assertIsNone(result["servers"][1]["vcpus"])

    def test_errors_propagate(self):
        # the cache records the failure and the page says so
        self.servers.list.side_effect = RuntimeError("nova down")
        with self.assertRaises(RuntimeError):
            inventory.NovaComputeInventory(self.request).read()


class GetInventoryTests(base.TestCase):
    def test_fake(self):
        self.assertIsInstance(
            inventory.get_inventory(mock.MagicMock()),
            inventory.FakeComputeInventory,
        )

    @override_settings(GRIAN_PLUGIN={"datasource": "prometheus"})
    def test_nova(self):
        request = mock.MagicMock()
        found = inventory.get_inventory(request)
        self.assertIsInstance(found, inventory.NovaComputeInventory)
        self.assertIs(found.request, request)


class FakeDataTests(base.TestCase):
    """The fake telemetry and inventory describe the same project."""

    def setUp(self):
        super().setUp()
        self.client = fake.get_client()
        self.inventory = inventory.FakeComputeInventory().read()

    def test_ids_match(self):
        active = {
            s["id"]: s
            for s in self.inventory["servers"]
            if s["status"] == "ACTIVE"
        }
        snapshot = self.client.get_project_compute_snapshot("x")["instances"]
        stats = self.client.get_project_compute_rightsizing("x")["instances"]
        # every reporting instance is active in Nova; probe-01 reports none
        self.assertLess(set(snapshot), set(active))
        self.assertEqual(set(stats), set(snapshot))
        self.assertEqual(
            {active[i]["name"] for i in set(active) - set(snapshot)},
            {"probe-01"},
        )
        for instance_id, metrics in snapshot.items():
            self.assertEqual(metrics["name"], active[instance_id]["name"])
            self.assertEqual(metrics["vcpus"], active[instance_id]["vcpus"])

    def test_limits_count_the_servers(self):
        limits = self.inventory["limits"]
        servers = self.inventory["servers"]
        self.assertEqual(limits["totalInstancesUsed"], len(servers))
        self.assertEqual(
            limits["totalCoresUsed"], sum(s["vcpus"] for s in servers)
        )
        self.assertEqual(limits["maxTotalCores"], 50)

    def test_ids_are_uuids(self):
        from grian_ui.datasource import prometheus_project_compute as q

        for server in self.inventory["servers"]:
            self.assertTrue(q.is_instance_id(server["id"]), server["id"])

    def test_trend_is_on_the_grid(self):
        at = 1_795_190_000
        for range_key, (window, step) in pc.TREND_RANGES.items():
            points = self.client.get_project_compute_trend(
                "x", pc.QUOTA_CORES, range_key, at=at
            )["points"]
            grid = pc.grid(window, step, at)
            self.assertTrue(points)
            self.assertTrue({p[0] for p in points} <= set(grid))
            self.assertEqual(points[-1][1], 36.0)
        thirty = self.client.get_project_compute_trend(
            "x", pc.QUOTA_RAM, "30d", at=at
        )["points"]
        # the oldest quarter is missing (a gap in the chart)
        self.assertEqual(len(thirty), 90 - 90 // 4)

    def test_forecast(self):
        forecast = self.client.get_project_compute_forecast("x", 15)
        self.assertEqual(forecast["hours"], 15 * 24)
        self.assertAlmostEqual(forecast["slopes"][pc.QUOTA_RAM] * 86400, 1500)

    def test_series(self):
        at = 1_795_190_000
        ids = [fake_project_compute.instance_id(i) for i in (1, 15, 16)]
        series = self.client.get_project_compute_cpu_series("x", ids, at=at)[
            "series"
        ]
        # web-01 (7 days), worker-new (9 hours), probe-01 (no metrics)
        self.assertEqual(len(series[ids[0]]), 84)
        self.assertEqual(len(series[ids[1]]), 5)
        self.assertNotIn(ids[2], series)
        detail = self.client.get_project_compute_instance_trend(
            "x", ids[0], at=at
        )
        self.assertEqual(len(detail["cpu"]), 168)
        self.assertEqual(len(detail["memory"]), 168)
        self.assertEqual(
            self.client.get_project_compute_instance_trend("x", ids[2]),
            {"cpu": [], "memory": []},
        )
