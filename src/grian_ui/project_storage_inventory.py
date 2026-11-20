#
# SPDX-License-Identifier: Apache-2.0

"""
Project storage inventory from the OpenStack APIs (Cinder, Glance, Nova).

What holds quota, attachment state and ages are only known to the APIs
(Ceilometer reports sizes, and nothing for an error volume). They are read
through Horizon's API layer with the user's token, which lists the current
project's resources only, also for administrators (no ``all_tenants``), and
returned as the compact dicts of :mod:`grian_ui.project_storage`.

The ``fake`` datasource uses :class:`FakeProjectStorageInventory`.
"""

import abc
import datetime
import math

from grian_ui import config
from grian_ui import project as grian_project


#: Image visibilities listed as "private images": owned by the project
#: and not published to every project of the cloud. Shared images may be
#: used by the projects they are shared with: they are listed, but never
#: as clean-up candidates (grian_ui.project_storage.is_private).
PRIVATE_VISIBILITIES = ("private", "shared")


def epoch(value):
    """API timestamp (``2026-11-17T14:16:47.000000``, ``...Z``) to epoch."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _get(resource, name, default=None):
    """An attribute of a client or Horizon wrapper object, or ``default``."""
    try:
        value = getattr(resource, name)
    except AttributeError:
        return default
    return default if value is None else value


def _limit(value):
    """A Cinder limit: None for unlimited (-1, or Horizon's inf)."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isinf(value) or value < 0:
        return None
    return int(value)


def compact_limits(limits):
    """Cinder absolute limits (dict) to the compact limits."""
    return {
        "gigabytes": _limit(limits.get("maxTotalVolumeGigabytes")),
        "volumes": _limit(limits.get("maxTotalVolumes")),
        "snapshots": _limit(limits.get("maxTotalSnapshots")),
        "backups": _limit(limits.get("maxTotalBackups")),
        "backups_used": _limit(limits.get("totalBackupsUsed")) or 0,
    }


def compact_volume(volume):
    """Keep the fields the panel needs from a cinderclient Volume."""
    attachments = _get(volume, "attachments", []) or []
    image_meta = _get(volume, "volume_image_metadata", {}) or {}
    return {
        "id": _get(volume, "id"),
        "name": _get(volume, "name", ""),
        "size": _get(volume, "size", 0),
        "status": _get(volume, "status"),
        "server_ids": [
            a.get("server_id")
            for a in attachments
            if isinstance(a, dict) and a.get("server_id")
        ],
        "created": epoch(_get(volume, "created_at")),
        "updated": epoch(_get(volume, "updated_at")),
        "image_id": image_meta.get("image_id")
        if isinstance(image_meta, dict)
        else None,
    }


def compact_snapshot(snapshot):
    """Keep the fields the panel needs from a cinderclient Snapshot."""
    return {
        "id": _get(snapshot, "id"),
        "name": _get(snapshot, "name", ""),
        "size": _get(snapshot, "size", 0),
        "status": _get(snapshot, "status"),
        "volume_id": _get(snapshot, "volume_id"),
        "created": epoch(_get(snapshot, "created_at")),
    }


def compact_server(server):
    """Keep the fields the panel needs from a Nova server."""
    image = _get(server, "image")
    # "" when booted from a volume, else {"id": ..., "links": [...]}
    image_id = image.get("id") if isinstance(image, dict) else None
    return {
        "id": _get(server, "id"),
        "name": _get(server, "name", ""),
        "image_id": image_id,
    }


def compact_image(image):
    """Keep the fields the panel needs from a Glance image."""
    return {
        "id": _get(image, "id"),
        "name": _get(image, "name", ""),
        "size": _get(image, "size", 0),
        "created": epoch(_get(image, "created_at")),
        "visibility": _get(image, "visibility"),
    }


class ProjectStorageInventory(abc.ABC):
    """The current project's storage, quota and instances."""

    @abc.abstractmethod
    def block_storage(self) -> dict:
        """Limits, volumes, snapshots and whether backups are enabled."""

    @abc.abstractmethod
    def servers(self) -> dict:
        """``{"servers": [...], "truncated": bool}``."""

    @abc.abstractmethod
    def images(self) -> dict:
        """``{"images": [...], "truncated": bool}`` (private images)."""


class OpenStackProjectStorageInventory(ProjectStorageInventory):
    """Inventory read through Horizon's Cinder, Nova and Glance API layers."""

    def __init__(self, request):
        self.request = request

    def block_storage(self):
        # imported lazily: unit tests run without horizon
        from openstack_dashboard.api import cinder

        limits = cinder.tenant_absolute_limits(self.request)
        client = cinder.cinderclient(self.request)
        # without a limit, cinderclient follows every page; the wrappers of
        # cinder.volume_list() drop updated_at and the image metadata
        volumes = client.volumes.list(detailed=True)
        snapshots = client.volume_snapshots.list(detailed=True)
        return {
            "limits": compact_limits(limits),
            "volumes": [compact_volume(v) for v in volumes],
            "snapshots": [compact_snapshot(s) for s in snapshots],
            # the setting Horizon's own Volume Backups panel follows
            # (Cinder does not report whether c-bak runs)
            "backup_enabled": bool(
                cinder.volume_backup_supported(self.request)
            ),
        }

    def servers(self):
        from openstack_dashboard.api import nova

        servers, more = nova.server_list(self.request)
        return {
            "servers": [compact_server(s) for s in servers],
            "truncated": bool(more),
        }

    def images(self):
        from openstack_dashboard.api import glance

        project_id = grian_project.project_id(self.request)
        images, more, _prev = glance.image_list_detailed(
            self.request, filters={"owner": project_id}
        )
        return {
            "images": [
                compact_image(i)
                for i in images
                if _get(i, "visibility") in PRIVATE_VISIBILITIES
            ],
            "truncated": bool(more),
        }


class FakeProjectStorageInventory(ProjectStorageInventory):
    """Sample inventory, for development and tests."""

    def block_storage(self):
        from grian_ui.datasource import fake_project_storage

        return fake_project_storage.block_storage()

    def servers(self):
        from grian_ui.datasource import fake_project_storage

        return {"servers": fake_project_storage.servers(), "truncated": False}

    def images(self):
        from grian_ui.datasource import fake_project_storage

        return {"images": fake_project_storage.images(), "truncated": False}


def get_inventory(request):
    """Project storage inventory for the configured datasource."""
    if config.get_datasource_type() == config.DATASOURCE_FAKE:
        return FakeProjectStorageInventory()
    return OpenStackProjectStorageInventory(request)
