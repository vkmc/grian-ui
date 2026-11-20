#
# SPDX-License-Identifier: Apache-2.0

import sys
import types

from unittest import mock

from grian_ui import config
from grian_ui import project_storage_inventory as inventory
from grian_ui_tests.unit import base


PROJECT = "b8753cd7d01b4455b774b46991f36f83"


def _resource(**attrs):
    return types.SimpleNamespace(**attrs)


class EpochTests(base.TestCase):
    def test_formats(self):
        self.assertEqual(inventory.epoch("1970-01-02T00:00:00.000000"), 86400)
        self.assertEqual(inventory.epoch("1970-01-02T00:00:00Z"), 86400)
        self.assertEqual(inventory.epoch("1970-01-02T01:00:00+01:00"), 86400)
        self.assertIsNone(inventory.epoch(None))
        self.assertIsNone(inventory.epoch(""))
        self.assertIsNone(inventory.epoch("yesterday"))


class CompactTests(base.TestCase):
    def test_limits(self):
        limits = {
            "maxTotalVolumeGigabytes": 1000,
            "maxTotalVolumes": 10,
            # Horizon turns -1 (unlimited) into inf
            "maxTotalSnapshots": float("inf"),
            "maxTotalBackups": -1,
            "totalBackupsUsed": 2,
            "totalGigabytesUsed": 0,
        }
        self.assertEqual(
            inventory.compact_limits(limits),
            {
                "gigabytes": 1000,
                "volumes": 10,
                "snapshots": None,
                "backups": None,
                "backups_used": 2,
            },
        )
        self.assertEqual(
            inventory.compact_limits({}),
            {
                "gigabytes": None,
                "volumes": None,
                "snapshots": None,
                "backups": None,
                "backups_used": 0,
            },
        )

    def test_volume(self):
        volume = _resource(
            id="v1",
            name="db-data",
            size=120,
            status="in-use",
            attachments=[{"server_id": "s1"}, {"attachment_id": "x"}],
            created_at="1970-01-02T00:00:00.000000",
            updated_at="1970-01-03T00:00:00.000000",
            volume_image_metadata={"image_id": "i1", "image_name": "x"},
        )
        self.assertEqual(
            inventory.compact_volume(volume),
            {
                "id": "v1",
                "name": "db-data",
                "size": 120,
                "status": "in-use",
                "server_ids": ["s1"],
                "created": 86400,
                "updated": 2 * 86400,
                "image_id": "i1",
            },
        )

    def test_volume_without_optional_fields(self):
        volume = _resource(id="v1", name=None, size=1, status="available")
        compact = inventory.compact_volume(volume)
        self.assertEqual(compact["name"], "")
        self.assertEqual(compact["server_ids"], [])
        self.assertIsNone(compact["image_id"])
        self.assertIsNone(compact["updated"])

    def test_snapshot(self):
        snapshot = _resource(
            id="s1",
            name="snap",
            size=50,
            status="available",
            volume_id="v1",
            created_at="1970-01-02T00:00:00.000000",
        )
        self.assertEqual(
            inventory.compact_snapshot(snapshot),
            {
                "id": "s1",
                "name": "snap",
                "size": 50,
                "status": "available",
                "volume_id": "v1",
                "created": 86400,
            },
        )

    def test_server(self):
        server = _resource(
            id="s1", name="web", image={"id": "i1", "links": []}
        )
        self.assertEqual(
            inventory.compact_server(server),
            {"id": "s1", "name": "web", "image_id": "i1"},
        )
        # booted from a volume
        server = _resource(id="s2", name="db", image="")
        self.assertIsNone(inventory.compact_server(server)["image_id"])

    def test_image(self):
        image = _resource(
            id="i1",
            name="golden",
            size=2 * 1024**3,
            created_at="1970-01-02T00:00:00Z",
            visibility="private",
        )
        self.assertEqual(
            inventory.compact_image(image),
            {
                "id": "i1",
                "name": "golden",
                "size": 2 * 1024**3,
                "created": 86400,
                "visibility": "private",
            },
        )


class OpenStackInventoryTests(base.TestCase):
    """Horizon's API layer, with the user's token, current project only."""

    def setUp(self):
        super().setUp()
        self.cinder = mock.MagicMock()
        self.nova = mock.MagicMock()
        self.glance = mock.MagicMock()
        api = mock.MagicMock(
            cinder=self.cinder, nova=self.nova, glance=self.glance
        )
        modules = {
            "openstack_dashboard": mock.MagicMock(api=api),
            "openstack_dashboard.api": api,
            "openstack_dashboard.api.cinder": self.cinder,
            "openstack_dashboard.api.nova": self.nova,
            "openstack_dashboard.api.glance": self.glance,
        }
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request = mock.MagicMock()
        self.request.user.project_id = PROJECT
        self.inventory = inventory.OpenStackProjectStorageInventory(
            self.request
        )

    def test_block_storage(self):
        self.cinder.tenant_absolute_limits.return_value = {
            "maxTotalVolumeGigabytes": 1000,
            "maxTotalVolumes": 10,
            "maxTotalSnapshots": 10,
            "maxTotalBackups": 10,
            "totalBackupsUsed": 0,
        }
        client = self.cinder.cinderclient.return_value
        client.volumes.list.return_value = [
            _resource(id="v1", name="a", size=1, status="available")
        ]
        client.volume_snapshots.list.return_value = [
            _resource(id="s1", name="b", size=1, volume_id="v1")
        ]
        self.cinder.volume_backup_supported.return_value = False
        found = self.inventory.block_storage()
        self.assertEqual(found["limits"]["gigabytes"], 1000)
        self.assertEqual([v["id"] for v in found["volumes"]], ["v1"])
        self.assertEqual([s["id"] for s in found["snapshots"]], ["s1"])
        self.assertFalse(found["backup_enabled"])
        # the project's own resources: never all_tenants
        client.volumes.list.assert_called_once_with(detailed=True)
        client.volume_snapshots.list.assert_called_once_with(detailed=True)
        self.cinder.tenant_absolute_limits.assert_called_once_with(
            self.request
        )

    def test_servers(self):
        self.nova.server_list.return_value = (
            [_resource(id="s1", name="web", image={"id": "i1"})],
            True,
        )
        self.assertEqual(
            self.inventory.servers(),
            {
                "servers": [{"id": "s1", "name": "web", "image_id": "i1"}],
                "truncated": True,
            },
        )
        self.nova.server_list.assert_called_once_with(self.request)

    def test_images_are_the_projects_private_ones(self):
        self.glance.image_list_detailed.return_value = (
            [
                _resource(id="i1", name="mine", visibility="private"),
                _resource(id="i2", name="shared", visibility="shared"),
                _resource(id="i3", name="cirros", visibility="public"),
                _resource(id="i4", name="comm", visibility="community"),
            ],
            False,
            False,
        )
        found = self.inventory.images()
        self.assertEqual([i["id"] for i in found["images"]], ["i1", "i2"])
        self.assertFalse(found["truncated"])
        self.glance.image_list_detailed.assert_called_once_with(
            self.request, filters={"owner": PROJECT}
        )

    def test_invalid_project_id_is_refused(self):
        self.request.user.project_id = 'x" or 1'
        self.assertRaises(ValueError, self.inventory.images)
        self.glance.image_list_detailed.assert_not_called()


class GetInventoryTests(base.TestCase):
    def test_fake(self):
        self.assertIsInstance(
            inventory.get_inventory(None),
            inventory.FakeProjectStorageInventory,
        )
        fake = inventory.get_inventory(None)
        self.assertEqual(len(fake.block_storage()["volumes"]), 5)
        self.assertEqual(len(fake.servers()["servers"]), 9)
        self.assertEqual(len(fake.images()["images"]), 3)

    def test_prometheus(self):
        with mock.patch(
            "grian_ui.project_storage_inventory.config.get_datasource_type",
            return_value=config.DATASOURCE_PROMETHEUS,
        ):
            request = object()
            found = inventory.get_inventory(request)
        self.assertIsInstance(
            found, inventory.OpenStackProjectStorageInventory
        )
        self.assertIs(found.request, request)
