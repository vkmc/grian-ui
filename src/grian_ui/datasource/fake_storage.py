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
