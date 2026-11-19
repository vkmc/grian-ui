#
# SPDX-License-Identifier: Apache-2.0

"""
Volume and snapshot inventory from the OpenStack APIs (Cinder, Keystone).

Ceilometer only reports volumes and snapshots with their size: their
state (error, stuck in a transition, unattached) is only known to Cinder.
They are read from the Block Storage API through Horizon's API layer,
with the administrator's token, and returned as compact dicts (see
:mod:`grian_ui.storage_health` for the format).

The ``fake`` datasource uses :class:`FakeStorageInventory`.
"""

import abc
import datetime

from grian_ui import config


def _epoch(value):
    """Cinder timestamp (``2026-11-17T14:16:47.000000``, UTC) to epoch."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _get(resource, *names):
    """First attribute that is set."""
    for name in names:
        value = getattr(resource, name, None)
        if value is not None:
            return value
    return None


def compact_volume(volume):
    """Keep the fields the dashboard needs from a cinderclient Volume."""
    return {
        "id": _get(volume, "id"),
        "name": _get(volume, "name") or "",
        "project_id": _get(volume, "os-vol-tenant-attr:tenant_id"),
        "status": _get(volume, "status"),
        "size": _get(volume, "size"),
        "host": _get(volume, "os-vol-host-attr:host"),
        "attached": bool(_get(volume, "attachments")),
        "created": _epoch(_get(volume, "created_at")),
        "updated": _epoch(_get(volume, "updated_at")),
    }


def compact_snapshot(snapshot):
    """Keep the fields the dashboard needs from a cinderclient Snapshot."""
    return {
        "id": _get(snapshot, "id"),
        "name": _get(snapshot, "name") or "",
        "project_id": _get(
            snapshot, "os-extended-snapshot-attributes:project_id"
        ),
        "status": _get(snapshot, "status"),
        "size": _get(snapshot, "size"),
        "volume_id": _get(snapshot, "volume_id"),
        "created": _epoch(_get(snapshot, "created_at")),
        "updated": _epoch(_get(snapshot, "updated_at")),
    }


class StorageInventory(abc.ABC):
    """Volume, snapshot and project inventory."""

    @abc.abstractmethod
    def list_volumes(self) -> list:
        """Every volume of every project (compact dicts)."""

    @abc.abstractmethod
    def list_snapshots(self) -> list:
        """Every volume snapshot of every project (compact dicts)."""

    @abc.abstractmethod
    def project_names(self) -> dict:
        """Project id -> name."""


class CinderStorageInventory(StorageInventory):
    """Inventory read through Horizon's Cinder and Keystone API layers."""

    def __init__(self, request):
        self.request = request

    def _client(self):
        # imported lazily: unit tests run without horizon
        from openstack_dashboard.api import cinder

        if getattr(self, "_cinder", None) is None:
            self._cinder = cinder.cinderclient(self.request)
        return self._cinder

    def list_volumes(self):
        # without a limit, cinderclient follows every page (one request
        # per osapi_max_limit volumes, 1000 by default)
        volumes = self._client().volumes.list(
            detailed=True, search_opts={"all_tenants": 1}
        )
        return [compact_volume(v) for v in volumes]

    def list_snapshots(self):
        snapshots = self._client().volume_snapshots.list(
            detailed=True, search_opts={"all_tenants": 1}
        )
        return [compact_snapshot(s) for s in snapshots]

    def project_names(self):
        from openstack_dashboard.api import keystone

        projects, _more = keystone.tenant_list(self.request, admin=True)
        return {p.id: p.name for p in projects}


class FakeStorageInventory(StorageInventory):
    """Sample inventory, for development and tests."""

    def list_volumes(self):
        from grian_ui.datasource import fake_storage

        return fake_storage.fake_volumes()

    def list_snapshots(self):
        from grian_ui.datasource import fake_storage

        return fake_storage.fake_snapshots()

    def project_names(self):
        from grian_ui.datasource import fake_storage

        return dict(fake_storage.FAKE_PROJECTS)


def get_inventory(request):
    """Storage inventory for the configured datasource."""
    if config.get_datasource_type() == config.DATASOURCE_FAKE:
        return FakeStorageInventory()
    return CinderStorageInventory(request)
