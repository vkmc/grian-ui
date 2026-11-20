#
# SPDX-License-Identifier: Apache-2.0

"""
Sample data of the Project Storage panel (``fake`` datasource).

The invented "web-shop" project of the mockup: five volumes (two
unattached), three snapshots and three private images against a 1000 GB
quota, 840 GB used. The metric history grows linearly to today's sizes
over 90 days, so the footprint chart, the forecast and the quota rows
agree. Ages are relative to the time of the call.
"""

import time
import uuid

from grian_ui import project_storage as ps


_NAMESPACE = uuid.UUID("0f6f8e8e-2c55-4f0b-9d0f-1b8b4c3a7e21")
GIB = ps.GIB


def _id(name):
    return str(uuid.uuid5(_NAMESPACE, name))


LIMITS = {
    "gigabytes": 1000,
    "volumes": 10,
    "snapshots": 10,
    "backups": 10,
    "backups_used": 0,
}

#: name -> (GB, attached to, age in days, days since the last update)
_VOLUMES = {
    "db-data": (120, "db-01", 210, 1),
    "web-assets": (50, "web-01", 180, 2),
    "logs-archive": (300, None, 95, 41),
    "old-test-vol": (60, None, 30, 12),
    "scratch": (20, "batch-01", 60, 3),
}
#: name -> (GB, volume, age in days)
_SNAPSHOTS = {
    "db-data-2026-11-17": (120, "db-data", 1),
    "db-data-2026-09-01": (120, "db-data", 78),
    "web-assets-base": (50, "web-assets", 120),
}
#: name -> (GB, age in days)
_IMAGES = {
    "web-golden-2026-10": (2.1, 40),
    "web-golden-2026-06": (2.0, 155),
    "batch-worker": (3.4, 70),
}
#: server name -> boot image name (None: booted from a volume, or a
#: public image)
_SERVERS = {
    **{f"web-0{i}": "web-golden-2026-10" for i in range(1, 7)},
    "db-01": None,
    "batch-01": "batch-worker",
    "batch-02": "batch-worker",
}

#: GB of volumes and snapshots 90 days ago (the history grows linearly to
#: today's totals).
_HISTORY_DAYS = 90
_VOLUMES_GB_START = 380
_SNAPSHOTS_GB_START = 120


def _now(now):
    return time.time() if now is None else now


def block_storage(now=None):
    """Limits, volumes and snapshots, like the Block Storage API."""
    now = _now(now)
    volumes = [
        {
            "id": _id(name),
            "name": name,
            "size": gb,
            "status": ps.STATUS_IN_USE if server else ps.STATUS_AVAILABLE,
            "server_ids": [_id(server)] if server else [],
            "created": now - age * ps.DAY_SECONDS,
            "updated": now - idle * ps.DAY_SECONDS,
            "image_id": None,
        }
        for name, (gb, server, age, idle) in _VOLUMES.items()
    ]
    snapshots = [
        {
            "id": _id(name),
            "name": name,
            "size": gb,
            "status": "available",
            "volume_id": _id(volume),
            "created": now - age * ps.DAY_SECONDS,
        }
        for name, (gb, volume, age) in _SNAPSHOTS.items()
    ]
    return {
        "limits": dict(LIMITS),
        "volumes": volumes,
        "snapshots": snapshots,
        "backup_enabled": False,
    }


def servers():
    """The project's instances, like the Compute API."""
    return [
        {
            "id": _id(name),
            "name": name,
            "image_id": _id(image) if image else None,
        }
        for name, image in _SERVERS.items()
    ]


def images(now=None):
    """The project's private images, like the Image API."""
    now = _now(now)
    return [
        {
            "id": _id(name),
            "name": name,
            "size": int(gb * GIB),
            "created": now - age * ps.DAY_SECONDS,
            "visibility": "private",
        }
        for name, (gb, age) in _IMAGES.items()
    ]


def _totals():
    block = block_storage(0)
    use = ps.usage(block)
    return (
        use["attached_gb"] + use["unattached_gb"],
        use["snapshots_gb"],
        use["volumes"],
        use["snapshots"],
    )


def _grown(start, end, ts, now):
    """Linear growth from ``start`` (_HISTORY_DAYS ago) to ``end`` (now)."""
    since = _HISTORY_DAYS * ps.DAY_SECONDS - (now - ts)
    share = min(1.0, max(0.0, since / (_HISTORY_DAYS * ps.DAY_SECONDS)))
    return round(start + (end - start) * share, 3)


def _steps(days, step, now):
    """Subquery timestamps: multiples of ``step`` in (now - days, now]."""
    end = int(now // step) * step
    count = int(days * ps.DAY_SECONDS // step)
    return [end - (count - 1 - i) * step for i in range(count)]


def get_footprint(project_matcher, days, at=None):
    """Like prometheus_project_storage.get_footprint."""
    now = _now(at)
    volumes_gb, snapshots_gb, _v, _s = _totals()
    stamps = _steps(days, ps.DAY_SECONDS, now)
    return {
        "volumes": [
            [t, _grown(_VOLUMES_GB_START, volumes_gb, t, now)] for t in stamps
        ],
        "snapshots": [
            [t, _grown(_SNAPSHOTS_GB_START, snapshots_gb, t, now)]
            for t in stamps
        ],
    }


def get_trend(project_matcher, days, at=None):
    """Like prometheus_project_storage.get_trend."""
    now = _now(at)
    volumes_gb, snapshots_gb, volumes, snapshots = _totals()
    stamps = _steps(days, ps.TREND_STEP_SECONDS, now)
    return {
        "gigabytes": [
            [
                t,
                round(
                    _grown(_VOLUMES_GB_START, volumes_gb, t, now)
                    + _grown(_SNAPSHOTS_GB_START, snapshots_gb, t, now),
                    3,
                ),
            ]
            for t in stamps
        ],
        "volumes": [[t, volumes] for t in stamps],
        "snapshots": [[t, snapshots] for t in stamps],
    }
