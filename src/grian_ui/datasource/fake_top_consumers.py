#
# SPDX-License-Identifier: Apache-2.0

"""
Sample data of the Top Consumers dashboard (``fake`` datasource).

An invented cloud of 12 projects with running instances and 2 idle ones.
Project values are the sums of their instances', and the trends of
projects and totals are the sums of the instance trends, so every panel
agrees. ``ci-runners`` has the saw-tooth shape of batch jobs.
"""

import math
import uuid

from grian_ui import timeseries
from grian_ui import top_consumers as tc


_NAMESPACE = uuid.UUID("6f1d3c52-0b8e-4c3a-9a57-5a4f1b2c7d10")

#: name -> (cores in use, vCPUs, MiB in use, disk bytes/s, network bytes/s)
_PROJECTS = {
    "ml-training": (180, 192, 1_400_000, 9e6, 4e6),
    "data-lake": (95, 160, 900_000, 140e6, 30e6),
    "ci-runners": (70, 120, 300_000, 60e6, 12e6),
    "analytics": (31, 64, 380_000, 75e6, 3e6),
    "erp": (22, 48, 180_000, 18e6, 5e6),
    "mobile-api": (14, 24, 60_000, 2e6, 9e6),
    "web-shop": (10.3, 40, 26_000, 3e6, 2.6e6),
    "monitoring": (6, 12, 40_000, 8e6, 1.5e6),
    "staging": (3, 40, 90_000, 1e6, 0.5e6),
    "backup": (2, 8, 16_000, 220e6, 40e6),
    "hr-portal": (1.1, 4, 8_000, 0.3e6, 0.4e6),
    "sandbox": (0.4, 32, 50_000, 0.1e6, 0.1e6),
}
#: Projects without running instances.
_IDLE_PROJECTS = ("service", "decommissioned")
#: Flavor RAM per vCPU (MiB).
_MIB_PER_VCPU = 8192
_VCPUS_PER_INSTANCE = 4


def _project_id(name):
    return uuid.uuid5(_NAMESPACE, f"project:{name}").hex


def _make_instances():
    instances = []
    for seed, (project, values) in enumerate(_PROJECTS.items()):
        cpu, vcpus, memory, disk, net = values
        count = max(1, round(vcpus / _VCPUS_PER_INSTANCE))
        # bigger instances first: shares n, n-1, ..., 1
        weights = [count - i for i in range(count)]
        total_weight = sum(weights)
        prefix = project.split("-")[0]
        for i, weight in enumerate(weights):
            share = weight / total_weight
            instance_vcpus = vcpus / count
            instances.append(
                {
                    "id": str(uuid.uuid5(_NAMESPACE, f"{project}:{i}")),
                    "name": f"{prefix}-{i + 1:02d}",
                    "project_id": _project_id(project),
                    "project": project,
                    "seed": seed * 31 + i,
                    "cpu": cpu * share,
                    "vcpus": instance_vcpus,
                    "memory": memory * share,
                    "memory_alloc": instance_vcpus * _MIB_PER_VCPU,
                    "disk_read": disk * share * 0.4,
                    "disk_write": disk * share * 0.6,
                    "net_in": net * share * 0.55,
                    "net_out": net * share * 0.45,
                }
            )
    return instances


_INSTANCES = _make_instances()

#: Project id -> name, for the fake Keystone lookup.
FAKE_PROJECT_NAMES = {
    _project_id(name): name for name in list(_PROJECTS) + list(_IDLE_PROJECTS)
}


def get_projects():
    """The per-project snapshot (sums of the instances)."""
    projects = {}
    for instance in _INSTANCES:
        row = projects.setdefault(
            instance["project_id"],
            dict(
                {f: 0 for f in tc.SNAPSHOT_FIELDS}, id=instance["project_id"]
            ),
        )
        row["instances"] += 1
        for field in tc.SNAPSHOT_FIELDS:
            if field != "instances":
                row[field] += instance[field]
    return {"projects": list(projects.values())}


def _instance_value(instance, resource):
    return tc.resource_value(instance, resource)


def get_instances(resource, project_id=None, limit=tc.TOP_LIMIT):
    """The ``limit`` instances using the most of ``resource``."""
    candidates = [
        i
        for i in _INSTANCES
        if project_id is None or i["project_id"] == project_id
    ]
    candidates.sort(key=lambda i: -_instance_value(i, resource))
    return [
        {
            "id": i["id"],
            "name": i["name"],
            "project_id": i["project_id"],
            "value": _instance_value(i, resource),
            "vcpus": i["vcpus"],
            "memory_alloc": i["memory_alloc"],
        }
        for i in candidates[:limit]
    ]


def _series(instance, resource, grid):
    level = _instance_value(instance, resource)
    seed = instance["seed"]
    batch = instance["project"] == "ci-runners"
    values = []
    for i, _timestamp in enumerate(grid):
        factor = 1 + 0.12 * math.sin((i + seed * 7) / 6.0)
        if batch:
            factor *= 1.7 if i % 32 < 10 else 0.7
        values.append(level * factor)
    return values


def get_trend(
    resource,
    by,
    keys,
    project_id=None,
    range_key=tc.DEFAULT_TREND_RANGE,
    at=None,
):
    """Trends of ``keys`` and the total, summed from the instances."""
    grid = timeseries.range_grid(range_key, at)
    keys = set(keys)
    sums, total = {}, [0.0] * len(grid)
    for instance in _INSTANCES:
        if project_id is not None and instance["project_id"] != project_id:
            continue
        values = _series(instance, resource, grid)
        total = [a + b for a, b in zip(total, values)]
        key = (
            instance["id"] if by == tc.BY_INSTANCE else instance["project_id"]
        )
        if key in keys:
            current = sums.get(key, [0.0] * len(grid))
            sums[key] = [a + b for a, b in zip(current, values)]

    def points(values):
        return [[float(t), v] for t, v in zip(grid, values)]

    return {
        "series": {key: points(values) for key, values in sums.items()},
        "total": points(total),
    }
