#
# SPDX-License-Identifier: Apache-2.0

"""
Sample data of the project Usage report (fake datasource).

Deterministic: the same period always gives the same report. Used by the
fake datasource (development without a cloud) and the unit tests. The
format is described in :mod:`grian_ui.project_usage`.

The sample project has two instances running the whole period, one that
ran for its first days and was deleted since, one shut off and one in
ERROR (both without usage: they appear in the note under the table), and
a volume and a snapshot.
"""

import datetime

from grian_ui import project_usage as pu


HOUR = 3600

#: Instances with usage: (id, name, vCPUs, flavor RAM MiB, CPU use,
#: memory use, bytes/s in, bytes/s out, hours running from the start of
#: the period or None for all of it).
FAKE_INSTANCES = (
    (
        "d414fbd9-66a6-485c-bec7-8191c2fa4221",
        "web-1",
        4,
        8192,
        0.62,
        0.55,
        42_000,
        180_000,
        None,
    ),
    (
        "c726b640-2a18-41e3-9fac-af2730c12410",
        "db-1",
        2,
        4096,
        0.04,
        0.71,
        9_000,
        6_000,
        None,
    ),
    (
        "6a0a1f43-6b0c-4f1e-9a43-3c2f1d1e8b11",
        "batch-2",
        8,
        16384,
        0.93,
        0.40,
        1_000,
        500,
        30,
    ),
)
#: Compute API view of the project (batch-2 was deleted).
FAKE_SERVERS = (
    {
        "id": "d414fbd9-66a6-485c-bec7-8191c2fa4221",
        "name": "web-1",
        "status": "ACTIVE",
        "flavor": "m1.large",
    },
    {
        "id": "c726b640-2a18-41e3-9fac-af2730c12410",
        "name": "db-1",
        "status": "ACTIVE",
        "flavor": "m1.medium",
    },
    {
        "id": "9e4b6a2c-1b8f-4d4b-8f3e-2a6c5d7e9f01",
        "name": "build-01",
        "status": "SHUTOFF",
        "flavor": "m1.small",
    },
    {
        "id": "133a85dc-be0f-44e3-8f4f-475b79e704c6",
        "name": "grian-ih-error",
        "status": "ERROR",
        "flavor": "m1.tiny",
    },
)
#: Storage held the whole period, GiB.
FAKE_VOLUME_GIB = 20
FAKE_SNAPSHOT_GIB = 5


def _weekend(epoch):
    return datetime.datetime.fromtimestamp(epoch, pu.UTC).weekday() >= 5


def get_usage(start, end):
    """The sample report between ``start`` and ``end`` (aware datetimes)."""
    first, last = start.timestamp(), end.timestamp()
    hourly = {field: {} for field in pu.HOURLY_FIELDS}
    instances = {}
    for spec in FAKE_INSTANCES:
        uuid, name, vcpus, ram, cpu, mem, rx, tx, hours = spec
        stop = last if hours is None else min(last, first + hours * HOUR)
        totals = dict.fromkeys(pu.SUM_FIELDS, 0.0)
        hour = first
        while hour < stop:
            span = (min(hour + HOUR, stop) - hour) / HOUR
            # quieter weekends, as in the mockup
            busy = 0.7 if _weekend(hour) else 1.0
            values = {
                "hours": span,
                "vcpu_hours": vcpus * span,
                "cpu_hours": vcpus * span * cpu * busy,
                "ram_gb_hours_held": ram / 1024 * span,
                "ram_gb_hours_used": ram / 1024 * span * mem,
                "net_in": rx * span * HOUR * busy,
                "net_out": tx * span * HOUR * busy,
            }
            for field, value in values.items():
                totals[field] += value
            for field, value in (
                ("vcpu_hours", values["vcpu_hours"]),
                ("cpu_hours", values["cpu_hours"]),
                ("ram_gb_hours_held", values["ram_gb_hours_held"]),
                ("ram_gb_hours_used", values["ram_gb_hours_used"]),
                ("net_bytes", values["net_in"] + values["net_out"]),
            ):
                hourly[field][hour] = hourly[field].get(hour, 0.0) + value
            hour += HOUR
        if totals["hours"] <= 0:
            continue
        instances[uuid] = dict(
            totals, name=name, vcpus=float(vcpus), memory_mib=float(ram)
        )
    held_hours = max(0.0, (last - first) / HOUR)
    return {
        "instances": instances,
        "volume_gb_hours": FAKE_VOLUME_GIB * held_hours,
        "snapshot_gb_hours": FAKE_SNAPSHOT_GIB * held_hours,
        "hourly": {
            field: [[t, v] for t, v in sorted(points.items())]
            for field, points in hourly.items()
        },
        "start": first,
        "end": last,
    }


def get_servers():
    """The sample Compute API server list (new dicts)."""
    return [dict(server) for server in FAKE_SERVERS]
