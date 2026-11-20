#
# SPDX-License-Identifier: Apache-2.0

"""
Sample data of the Project Compute dashboard (``fake`` datasource).

An invented "web-shop" project (the larger sample of the mockup): 18
instances, of which 16 are active, with every state the page can show:

* quota: instances 18 of 25, vCPUs 43 of 50 (86%, the highest), RAM
  83 GiB of 100 GiB; full in about 28 days (instances), 19 (vCPUs)
  and 12 (RAM);
* ``build-01`` (SHUTOFF, 8 GiB) and ``old-test`` (ERROR "No valid host
  was found.", 4 GiB) hold quota without running;
* rightsizing: the three ``api-*`` and ``db-01`` (swapping) are
  undersized, ``cache-01``, ``batch-01`` and ``batch-02`` oversized,
  ``worker-new`` provisional (9 hours of data), ``probe-01`` reports no
  metrics;
* attention: ``api-03`` CPU wait 25% (critical), ``api-02`` 6%, ``db-01``
  swapping and slow writes (34 ms), ``old-test`` in ERROR.

Values are deterministic, so unit tests can assert them.
"""

import math
import time

from grian_ui import project_compute as pc


_HOUR = 3600
_DAY = 86400
_MIB = 1024 * 1024
_GIB = 1024 * _MIB

#: name: (flavor, vcpus, ram MiB, disk GB)
FLAVORS = {
    "m1.tiny": (1, 512, 1),
    "m1.small": (1, 2048, 20),
    "m1.medium": (2, 4096, 40),
    "m1.large": (4, 8192, 80),
    "m1.xlarge": (8, 16384, 160),
}

#: (name, flavor, status, 7-day CPU average and current memory as shares
#: of the flavor, extra fields)
_SERVERS = (
    ("web-01", "m1.small", "ACTIVE", 0.48, 0.58, {}),
    ("web-02", "m1.small", "ACTIVE", 0.52, 0.61, {}),
    ("web-03", "m1.small", "ACTIVE", 0.55, 0.63, {}),
    ("web-04", "m1.small", "ACTIVE", 0.59, 0.57, {}),
    ("web-05", "m1.small", "ACTIVE", 0.61, 0.66, {}),
    ("web-06", "m1.small", "ACTIVE", 0.63, 0.69, {}),
    ("api-01", "m1.medium", "ACTIVE", 0.86, 0.62, {}),
    ("api-02", "m1.medium", "ACTIVE", 0.9, 0.65, {"cpu_wait": 0.12}),
    ("api-03", "m1.medium", "ACTIVE", 0.93, 0.68, {"cpu_wait": 0.5}),
    (
        "db-01",
        "m1.large",
        "ACTIVE",
        0.32,
        0.93,
        {"swap": 0.8, "swap_7d": 3200, "write_mib": 5, "latency": 34},
    ),
    ("cache-01", "m1.medium", "ACTIVE", 0.02, 0.14, {}),
    ("batch-01", "m1.xlarge", "ACTIVE", 0.03, 0.08, {}),
    ("batch-02", "m1.xlarge", "ACTIVE", 0.01, 0.06, {}),
    ("bastion", "m1.tiny", "ACTIVE", 0.03, 0.4, {}),
    ("worker-new", "m1.small", "ACTIVE", 0.35, 0.45, {"hours": 9}),
    ("probe-01", "m1.tiny", "ACTIVE", None, None, {"no_metrics": True}),
    ("build-01", "m1.large", "SHUTOFF", None, None, {"since_days": 23}),
    (
        "old-test",
        "m1.medium",
        "ERROR",
        None,
        None,
        {"since_days": 9, "fault": "No valid host was found."},
    ),
)

LIMITS = {
    "maxTotalInstances": 25,
    "maxTotalCores": 50,
    "maxTotalRAMSize": 102400,
}

#: Growth per day of what the running instances hold (the forecast).
GROWTH_PER_DAY = {
    pc.QUOTA_INSTANCES: 0.25,
    pc.QUOTA_CORES: 0.38,
    pc.QUOTA_RAM: 1500,
}


def instance_id(index):
    """A stable UUID per sample server."""
    return f"0000{index:04x}-5a3e-4c1b-9d2f-{index:012x}"


def _servers():
    for index, (name, flavor, status, cpu, mem, extra) in enumerate(_SERVERS):
        vcpus, ram, disk = FLAVORS[flavor]
        yield {
            "index": index,
            "id": instance_id(index + 1),
            "name": name,
            "flavor": flavor,
            "status": status,
            "vcpus": vcpus,
            "ram": ram,
            "disk": disk,
            "cpu": cpu,
            "mem": mem,
            "extra": extra,
        }


def _reporting():
    """Active servers that report metrics."""
    return [
        s
        for s in _servers()
        if s["status"] == "ACTIVE" and not s["extra"].get("no_metrics")
    ]


def get_inventory(now=None):
    """Servers and limits, as NovaComputeInventory.read() returns them."""
    now = time.time() if now is None else now
    servers = []
    for s in _servers():
        since = s["extra"].get("since_days")
        servers.append(
            {
                "id": s["id"],
                "name": s["name"],
                "status": s["status"],
                "flavor": s["flavor"],
                "vcpus": s["vcpus"],
                "ram": s["ram"],
                "disk": s["disk"],
                "fault": s["extra"].get("fault"),
                "updated": now - since * _DAY if since else now - 30 * _DAY,
            }
        )
    limits = dict(LIMITS)
    limits["totalInstancesUsed"] = len(servers)
    limits["totalCoresUsed"] = sum(s["vcpus"] for s in servers)
    limits["totalRAMUsed"] = sum(s["ram"] for s in servers)
    return {"servers": servers, "limits": limits}


def _wiggle(i, amplitude=0.08):
    return 1 + amplitude * math.sin(i / 3.0)


def get_snapshot():
    """Current values, as prometheus_project_compute.get_snapshot()."""
    instances = {}
    for s in _reporting():
        extra = s["extra"]
        write = extra.get("write_mib", 0.05) * _MIB
        instances[s["id"]] = {
            "name": s["name"],
            "vcpus": s["vcpus"],
            "memory_alloc": s["ram"],
            "cpu": round(s["cpu"] * s["vcpus"] * _wiggle(s["index"]), 3),
            "cpu_wait": extra.get("cpu_wait", 0.01 * s["vcpus"]),
            "memory": round(s["mem"] * s["ram"], 1),
            "swap": extra.get("swap", 0),
            "disks": {
                "vda": {
                    "used": s["disk"] * _GIB * (0.35 + 0.02 * s["index"]),
                    "capacity": s["disk"] * _GIB,
                    "write": write,
                    # slow but tiny writes elsewhere: ignored (floor)
                    "latency": extra.get("latency", 31),
                }
            },
        }
    return {"instances": instances}


def get_rightsizing():
    """7-day statistics, as prometheus_project_compute.get_rightsizing()."""
    instances = {}
    for s in _reporting():
        extra = s["extra"]
        instances[s["id"]] = {
            "cpu_avg": s["cpu"] * s["vcpus"],
            "cpu_p95": min(1.0, s["cpu"] * 1.6 + 0.01) * s["vcpus"],
            "memory_avg": s["mem"] * s["ram"],
            "swap": extra.get("swap_7d", 0),
            "hours": extra.get("hours", 168),
        }
    return {"instances": instances}


def get_forecast(days):
    """Growth and history, as prometheus_project_compute.get_forecast()."""
    return {
        "slopes": {q: v / _DAY for q, v in GROWTH_PER_DAY.items()},
        "hours": min(int(days), 30) * 24,
        "days": days,
    }


def _running_totals():
    reporting = _reporting()
    return {
        pc.QUOTA_INSTANCES: len(reporting),
        pc.QUOTA_CORES: sum(s["vcpus"] for s in reporting),
        pc.QUOTA_RAM: sum(s["ram"] for s in reporting),
    }


def get_trend(resource, range_key, at=None):
    """A quota trend, as prometheus_project_compute.get_trend()."""
    _key, window, step = pc.trend_range(range_key)
    timestamps = pc.grid(window, step, at)
    now_value = _running_totals()[resource]
    n = len(timestamps)
    points = []
    for i, ts in enumerate(timestamps):
        # growing towards today's value, the oldest quarter missing on 30d
        if range_key == "30d" and i < n // 4:
            continue
        days_ago = (timestamps[-1] - ts) / _DAY
        value = max(0.0, now_value - GROWTH_PER_DAY[resource] * days_ago)
        if resource != pc.QUOTA_RAM:
            value = float(round(value))
        points.append([float(ts), round(value, 3)])
    return {"points": points}


def get_cpu_series(instance_ids, at=None):
    """7-day CPU sparklines, as prometheus_project_compute.get_cpu_series."""
    wanted = set(instance_ids)
    timestamps = pc.grid(pc.RIGHTSIZING_WINDOW, pc.SPARK_STEP, at)
    series = {}
    for s in _reporting():
        if s["id"] not in wanted:
            continue
        hours = s["extra"].get("hours", 168)
        start = timestamps[-1] - hours * _HOUR
        series[s["id"]] = [
            [float(ts), round(s["cpu"] * s["vcpus"] * _wiggle(i, 0.15), 3)]
            for i, ts in enumerate(timestamps)
            if ts >= start
        ]
    return {"series": series}


def get_instance_trend(instance_id, at=None):
    """Detail charts, as prometheus_project_compute.get_instance_trend()."""
    timestamps = pc.grid(pc.RIGHTSIZING_WINDOW, pc.DETAIL_STEP, at)
    for s in _reporting():
        if s["id"] != instance_id:
            continue
        hours = s["extra"].get("hours", 168)
        start = timestamps[-1] - hours * _HOUR
        kept = [(i, ts) for i, ts in enumerate(timestamps) if ts >= start]
        return {
            "cpu": [
                [float(ts), round(s["cpu"] * s["vcpus"] * _wiggle(i, 0.15), 3)]
                for i, ts in kept
            ],
            "memory": [
                [float(ts), round(s["mem"] * s["ram"] * _wiggle(i, 0.03), 1)]
                for i, ts in kept
            ],
        }
    return {"cpu": [], "memory": []}
