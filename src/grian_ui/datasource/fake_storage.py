#
# SPDX-License-Identifier: Apache-2.0

"""
Sample Block Storage API inventory for the ``fake`` datasource.

Volumes and snapshots in every state the Storage Health page reacts to,
with times relative to now.
"""

import time


FAKE_PROJECTS = {
    "p-data": "data-lake",
    "p-ci": "ci-runners",
    "p-ml": "ml-training",
    "p-erp": "erp",
    "p-web": "web",
}

_HOURS = 3600
_DAYS = 86400


def _volume(vid, name, project, status, size, age, attached=False, pool=1):
    now = time.time()
    return {
        "id": vid,
        "name": name,
        "project_id": project,
        "status": status,
        "size": size,
        "host": f"ctl-01@lvm#storage-pool-0{pool}",
        "attached": attached,
        "created": now - max(age, 3 * _DAYS),
        "updated": now - age,
    }


def fake_volumes():
    """Sample volumes (compact dicts, see grian_ui.storage_health)."""
    return [
        _volume("v-01", "pg-data-07", "p-data", "error", 500, 2 * _HOURS),
        _volume(
            "v-02", "ci-cache-112", "p-ci", "error_deleting", 50, 3 * _DAYS
        ),
        _volume(
            "v-03", "ml-ckpt-31", "p-ml", "creating", 2000, 47 * 60, pool=2
        ),
        _volume(
            "v-04", "ml-ckpt-32", "p-ml", "creating", 2000, 5 * 60, pool=2
        ),
        _volume(
            "v-05", "erp-archive-2025", "p-erp", "available", 800, 64 * _DAYS
        ),
        _volume("v-06", "web-tmp", "p-web", "available", 20, 2 * _DAYS),
        _volume("v-07", "web-db", "p-web", "in-use", 200, 90 * _DAYS, True),
        _volume("v-08", "erp-db", "p-erp", "in-use", 400, 10 * _DAYS, True),
    ]


def fake_snapshots():
    """Sample snapshots (compact dicts, see grian_ui.storage_health)."""
    now = time.time()

    def snapshot(sid, name, project, status, size, age, volume_id):
        return {
            "id": sid,
            "name": name,
            "project_id": project,
            "status": status,
            "size": size,
            "volume_id": volume_id,
            "created": now - age,
            "updated": now - age,
        }

    return [
        snapshot(
            "s-01",
            "erp-db-2026-08",
            "p-erp",
            "available",
            400,
            109 * _DAYS,
            "v-08",
        ),
        snapshot(
            "s-02",
            "erp-db-2026-11",
            "p-erp",
            "available",
            400,
            2 * _DAYS,
            "v-08",
        ),
        snapshot(
            "s-03",
            "web-db-old",
            "p-web",
            "available",
            200,
            120 * _DAYS,
            "v-07",
        ),
        snapshot("s-04", "ci-snap", "p-ci", "error", 50, 6 * _HOURS, "v-02"),
    ]


# Telemetry samples matching the fake pools (grian_ui.datasource.fake)

#: The fake pool storage-pool-03 is stale: its volume service went down
#: three hours ago.
_DOWN_FOR = 3 * _HOURS


def fake_services():
    """Sample Cinder service states."""
    return [
        {
            "resource": "cinder-scheduler@ctl-01",
            "binary": "cinder-scheduler",
            "host": "ctl-01",
            "up": True,
        },
        {
            "resource": "cinder-volume@ctl-01@lvm",
            "binary": "cinder-volume",
            "host": "ctl-01@lvm",
            "up": True,
        },
        {
            "resource": "cinder-volume@ctl-02@nfs",
            "binary": "cinder-volume",
            "host": "ctl-02@nfs",
            "up": False,
        },
    ]


def fake_service_history(at=None):
    """24 hours of service states: ctl-02@nfs down for the last 3 hours."""
    from grian_ui import storage_health

    now = at if at is not None else time.time()
    timestamps = storage_health.grid(*storage_health.HISTORY, now)
    history = {}
    for service in fake_services():
        down = service["up"] is False
        history[service["resource"]] = [
            [ts, 0.0 if down and ts > now - _DOWN_FOR else 1.0]
            for ts in timestamps
        ]
    return history


def fake_capacity_trend(range_key, pools=None, at=None):
    """Capacity growing linearly towards the fake pools' current values."""
    from grian_ui import storage_health

    now = at if at is not None else time.time()
    _key, window, step = storage_health.get_range(range_key)
    timestamps = storage_health.grid(window, step, now)
    span = float(timestamps[-1] - timestamps[0]) or 1.0

    def growing(current, growth):
        return [
            [ts, round(current - growth * (timestamps[-1] - ts) / span, 2)]
            for ts in timestamps
        ]

    if pools:
        usage = {
            "ctl-01@lvm#storage-pool-01": 80.0,
            "ctl-01@lvm#storage-pool-02": 40.0,
        }
        return {
            name: growing(usage[name], 6.0) for name in pools if name in usage
        }
    return {
        "allocated": growing(6656.0, 400.0),
        "provisioned": growing(13912.0, 900.0),
        "total": [[ts, 12288.0] for ts in timestamps],
    }


def fake_images():
    """Sample image store and backup totals."""
    return {
        "images": 48.0,
        "image_bytes": 1.6e11,
        "image_projects": 5.0,
        "images_downloaded": 12.0,
        "backups": None,
        "backup_gib": None,
    }
