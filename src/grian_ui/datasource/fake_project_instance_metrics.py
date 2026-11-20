#
# SPDX-License-Identifier: Apache-2.0

"""
Sample data of the project Instance metrics page (fake datasource).

A small project: two running instances with different loads, one shut
off and one in ERROR, so every state of the page can be seen without a
cloud. The series are smooth curves (no randomness): the same request
always gives the same data, and tests can rely on it.
"""

import math

from grian_ui import project_instance_metrics as pim
from grian_ui import timeseries


#: Servers in the form the views read them from the Compute API (see
#: content.project_instance_metrics.views.fetch_server); the project is
#: filled in with the request's.
FAKE_SERVERS = (
    {
        "id": "5d0c3f6e-1b2a-4c8d-9e7f-0a1b2c3d4e5f",
        "name": "web-01",
        "status": "ACTIVE",
        "flavor": {
            "name": "m1.small",
            "vcpus": 1,
            "ram_mib": 2048,
            "disk_gb": 20,
        },
        "addresses": [{"network": "frontend", "ips": ["10.0.0.12"]}],
        "created_at": pim.parse_time("2026-11-01T08:00:00Z"),
    },
    {
        "id": "8f4e2d1c-3b5a-4d6e-8f7a-1b2c3d4e5f60",
        "name": "db-01",
        "status": "ACTIVE",
        "flavor": {
            "name": "m1.medium",
            "vcpus": 2,
            "ram_mib": 4096,
            "disk_gb": 40,
        },
        "addresses": [
            {"network": "backend", "ips": ["10.0.1.20", "203.0.113.21"]}
        ],
        "created_at": pim.parse_time("2026-10-20T08:00:00Z"),
    },
    {
        "id": "2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d",
        "name": "build-01",
        "status": "SHUTOFF",
        "flavor": {
            "name": "m1.large",
            "vcpus": 4,
            "ram_mib": 8192,
            "disk_gb": 80,
        },
        "addresses": [{"network": "backend", "ips": ["10.0.1.31"]}],
        "created_at": pim.parse_time("2026-10-01T08:00:00Z"),
    },
    {
        "id": "9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
        "name": "old-test",
        "status": "ERROR",
        "flavor": {
            "name": "m1.medium",
            "vcpus": 2,
            "ram_mib": 4096,
            "disk_gb": 40,
        },
        "addresses": [],
        "created_at": pim.parse_time("2026-11-10T08:00:00Z"),
        "fault": "No valid host was found.",
    },
)

#: Load of the running instances: (CPU share, wait share, memory share,
#: write bytes/s, read bytes/s, latency ms, inbound bytes/s, outbound).
_PROFILES = {
    "web-01": (0.45, 0.01, 0.55, 4e5, 1e5, 3.0, 180e3, 120e3),
    "db-01": (0.32, 0.03, 0.93, 8e6, 3e6, 4.5, 60e3, 90e3),
}


def servers(project_id):
    """The sample servers, owned by ``project_id``."""
    return [dict(s, project_id=project_id) for s in FAKE_SERVERS]


def server(project_id, instance_id):
    """One sample server, or None."""
    for found in servers(project_id):
        if found["id"] == instance_id:
            return found
    return None


def _wave(level, timestamps, period, depth=0.15, phase=0.0):
    return [
        round(
            level * (1 + depth * math.sin(2 * math.pi * t / period + phase)),
            4,
        )
        for t in timestamps
    ]


def get_instance_metrics(group, instance_id, range_key, at=None):
    """
    The series of one group (see the Prometheus implementation).

    Instances that are not running, and unknown ones, have no series.
    """
    if group not in pim.SERIES:
        raise ValueError(f"unknown group {group!r}")
    pim.validate_instance_id(instance_id)
    result = {name: [] for name in pim.SERIES[group]}
    found = server("fake", instance_id)
    if found is None or found["status"] != "ACTIVE":
        return result
    cpu, wait, mem, write, read, lat, net_in, net_out = _PROFILES[
        found["name"]
    ]
    vcpus = found["flavor"]["vcpus"]
    ram = found["flavor"]["ram_mib"]
    grid = timeseries.range_grid(range_key, at)
    day = 86400
    values = {
        "cpu": _wave(cpu * vcpus, grid, day, 0.5),
        "cpu_wait": _wave(wait * vcpus, grid, day / 3, 0.6, 1.0),
        "vcpus": [vcpus] * len(grid),
        "memory": _wave(mem * ram, grid, day, 0.03),
        "memory_alloc": [ram] * len(grid),
        "swap": [0.0] * len(grid),
        "disk_read": _wave(read, grid, day / 4, 0.5),
        "disk_write": _wave(write, grid, day / 6, 0.4),
        "disk_write_latency": _wave(lat, grid, day / 6, 0.25),
        "net_in": _wave(net_in, grid, day, 0.3),
        "net_out": _wave(net_out, grid, day, 0.3, 0.5),
    }
    for name in result:
        result[name] = [[t, v] for t, v in zip(grid, values[name])]
    return result
