#
# SPDX-License-Identifier: Apache-2.0

"""
Fake datasource implementation for development and testing.
Provides realistic sample telemetry data.
"""

import math
import random  # nosec
import time
import uuid

from grian_ui import compute_health
from grian_ui import instance_health
from grian_ui import timeseries
from grian_ui.datasource import fake_network
from grian_ui.datasource import fake_storage
from grian_ui.datasource.api import DatasourceClient


FAKE_PROJECTS = {
    "7bb2692e0d994decacab9a372f166676": "admin",
    "b8753cd7d01b4455b774b46991f36f83": "demo",
    "1f0c6a3e9d2b4c58a7e4f6b1c2d3e4f5": "web-prod",
    "2a1b3c4d5e6f47a8b9c0d1e2f3a4b5c6": "ci-runners",
}
_ADMIN, _DEMO, _WEB, _CI = FAKE_PROJECTS

# name, project, host, status, power state, task state, minutes since the
# last update, metric symptoms (only for reporting instances)
_FAKE_FLEET = [
    ("web-0001", _WEB, "compute-01", "ACTIVE", 1, None, 900, {"cpu": 28.0}),
    ("web-0002", _WEB, "compute-01", "ACTIVE", 1, None, 900, {}),
    (
        "web-0003",
        _WEB,
        "compute-02",
        "ACTIVE",
        1,
        None,
        900,
        {"network": 20.7},
    ),
    (
        "db-0001",
        _ADMIN,
        "compute-02",
        "ACTIVE",
        1,
        None,
        900,
        {"memory": 11.2, "disk": (140.0, "vdb")},
    ),
    ("db-0002", _ADMIN, "compute-02", "ERROR", 0, None, 60, None),
    (
        "cache-0001",
        _DEMO,
        "compute-01",
        "ACTIVE",
        1,
        None,
        900,
        {"disk": (24.0, "vda")},
    ),
    ("cache-0002", _DEMO, "compute-03", "ACTIVE", 1, None, 900, {"cpu": 8.0}),
    ("etl-0001", _CI, "compute-03", "ACTIVE", 1, None, 900, {"memory": 5.1}),
    ("etl-0002", _CI, "compute-03", "ACTIVE", 1, None, 900, None),
    ("ci-0001", _CI, "compute-01", "ACTIVE", 4, None, 30, None),
    ("ci-0002", _CI, "compute-02", "BUILD", 0, "spawning", 50, None),
    ("ci-0003", _CI, "compute-03", "BUILD", 0, "spawning", 2, None),
    ("api-0001", _WEB, "compute-01", "SHUTOFF", 4, None, 900, None),
    ("api-0002", _WEB, "compute-02", "SHUTOFF", 4, None, 900, None),
    ("api-0003", _DEMO, "compute-03", "PAUSED", 3, None, 900, None),
    ("api-0004", _DEMO, "compute-03", "SUSPENDED", 7, None, 900, None),
    ("worker-0001", _ADMIN, "compute-01", "ACTIVE", 1, None, 900, {}),
    ("worker-0002", _ADMIN, "compute-02", "ACTIVE", 1, None, 900, {}),
]


def _fake_id(name):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{name}.grian.example"))


def fake_servers(now=None):
    """Compact Nova servers matching the fake telemetry."""
    now = time.time() if now is None else now
    servers = []
    for name, project, host, status, power, task, minutes, _m in _FAKE_FLEET:
        servers.append(
            {
                "id": _fake_id(name),
                "name": name,
                "project_id": project,
                "host": host,
                "status": status,
                "power_state": power,
                "task_state": task,
                "updated": now - minutes * 60,
                "launched_at": None if status == "BUILD" else now - 86400,
                "fault": (
                    "No valid host was found. There are not enough hosts "
                    "available."
                    if status == "ERROR"
                    else None
                ),
            }
        )
    return servers


def _fake_symptoms():
    """instance id -> symptoms, for the reporting fake instances."""
    return {
        _fake_id(name): symptoms
        for name, *_rest, symptoms in _FAKE_FLEET
        if symptoms is not None
    }


def _symptom_value(entry):
    return entry[0] if isinstance(entry, tuple) else entry


def _fake_trend(instance_id, end_value, range_key):
    """A deterministic wavy series ending at ``end_value``."""
    timestamps = timeseries.range_grid(range_key)
    seed = int(instance_id[:8], 16)
    n = len(timestamps)
    points = []
    for i, timestamp in enumerate(timestamps):
        ramp = 0.3 + 0.7 * i / max(n - 1, 1)
        wave = 1 + 0.15 * math.sin(i / 5 + seed % 7)
        points.append([timestamp, round(end_value * ramp * wave, 2)])
    if points:
        points[-1][1] = end_value
    return points


class FakeTelemetryClient(DatasourceClient):
    """Fake telemetry client that generates sample data."""

    def get_volumes_count(self) -> int:
        """
        Get volumes count telemetry data.

        Returns hardcoded placeholder value for development/testing.
        """
        return 120

    def get_snapshots_count(self) -> int:
        """
        Get snapshots count telemetry data.

        Returns hardcoded placeholder value for development/testing.
        """
        return 45

    def get_storage_pools(self) -> list:
        """
        Get storage pools information.

        Returns sample pool data for development/testing: a thick pool, a
        thin pool close to its over-subscription limit, and a stale pool.
        """
        now = time.time()
        return [
            {
                "name": "ctl-01@lvm#storage-pool-01",
                "total": 5120.0,
                "allocated": 4096.0,
                "free": 1024.0,
                "provisioned": 5120.0,
                "virtual_free": None,
                "last_seen": now - 40,
                "stale": False,
            },
            {
                "name": "ctl-01@lvm#storage-pool-02",
                "total": 5120.0,
                "allocated": 2048.0,
                "free": 3072.0,
                "provisioned": 8192.0,
                "virtual_free": 2048.0,
                "last_seen": now - 25,
                "stale": False,
            },
            {
                "name": "ctl-02@nfs#storage-pool-03",
                "total": 2048.0,
                "allocated": 512.0,
                "free": 1536.0,
                "provisioned": 600.0,
                "virtual_free": 3496.0,
                "last_seen": now - 3 * 3600,
                "stale": True,
            },
        ]

    def get_capacity_predictions(self) -> list:
        """
        Get capacity predictions for storage pools.

        Returns sample prediction data for development/testing (pools
        reporting now only).
        """
        return [
            {
                "name": "ctl-01@lvm#storage-pool-01",
                "current_free": 1024.0,
                "current_virtual_free": None,
                "days_physical": 51.2,
                "days_virtual": None,
                "days_until_full": 51.2,
                "limiting": "physical",
                "will_run_out": True,
                "history_days": 7.0,
                "enough_history": True,
            },
            {
                "name": "ctl-01@lvm#storage-pool-02",
                "current_free": 3072.0,
                "current_virtual_free": 2048.0,
                "days_physical": None,
                "days_virtual": 24.5,
                "days_until_full": 24.5,
                "limiting": "virtual",
                "will_run_out": True,
                "history_days": 7.0,
                "enough_history": True,
            },
        ]

    # Storage Health (sample data in grian_ui.datasource.fake_storage)

    def get_volume_services(self) -> list:
        """Get sample Cinder service states (see fake_storage)."""
        return fake_storage.fake_services()

    def get_volume_service_history(self, at: float = None) -> dict:
        """Get a sample 24-hour Cinder service history."""
        return fake_storage.fake_service_history(at)

    def get_storage_capacity_trend(
        self, range_key: str, pools: list = None, at: float = None
    ) -> dict:
        """Get a sample capacity trend."""
        return fake_storage.fake_capacity_trend(range_key, pools, at)

    def get_storage_images(self) -> dict:
        """Get sample image and backup totals."""
        return fake_storage.fake_images()

    # Compute host health ------------------------------------------------

    def get_compute_host_snapshot(self) -> dict:
        """
        Get sample compute host data.

        24 reporting hosts (two critical, two warning) and one recently
        stale host; the 24 h stale list adds a second.
        """
        now = time.time()
        hosts = [
            {"name": h["name"], "cpu_avg": h["cpu"], "iowait_avg": h["iowait"]}
            for h in _FAKE_HOSTS
        ]
        stale = [
            s
            for s in self.get_compute_stale_hosts()
            if now - s["last_seen"]
            <= timeseries.duration_seconds(
                compute_health.STALE_RECENT_LOOKBACK
            )
        ]
        return {"hosts": hosts, "stale": stale}

    def get_compute_stale_hosts(self) -> list:
        """Two stale hosts: one recently (12 min), one long ago (95 min)."""
        now = time.time()
        return [
            {"name": "compute-25", "last_seen": now - 12 * 60},
            {"name": "compute-26", "last_seen": now - 95 * 60},
        ]

    def get_compute_host_details(self, names: list) -> dict:
        """Get sample CPU breakdown for the given hosts."""
        now = time.time()
        details = {}
        for host in _FAKE_HOSTS:
            if host["name"] not in names:
                continue
            cpu, iowait = host["cpu"], host["iowait"]
            user = round((cpu - iowait) * 0.82, 1)
            details[host["name"]] = {
                "cpu": cpu,
                "user": user,
                "kernel": round(cpu - iowait - user, 1),
                "iowait": iowait,
                "idle": round(100 - cpu, 1),
                "frequency": host["frequency"],
                "last_seen": now - host["age"],
            }
        return details

    def get_compute_host_trends(
        self, names: list, range_key: str, at: float = None
    ) -> dict:
        """Get sample CPU trends for the given hosts."""
        return {
            host["name"]: _fake_series(
                host["cpu"], host["seed"], range_key, at
            )
            for host in _FAKE_HOSTS
            if host["name"] in names
        }

    def get_compute_fleet_trend(
        self, range_key: str, at: float = None
    ) -> dict:
        """Get sample fleet median / p95 / max trends."""
        series = [
            _fake_series(h["cpu"], h["seed"], range_key, at)
            for h in _FAKE_HOSTS
        ]
        trend = {"p50": [], "p95": [], "max": []}
        for points in zip(*series):
            timestamp = points[0][0]
            values = [p[1] for p in points]
            trend["p50"].append(
                [timestamp, compute_health.quantile(values, 0.5)]
            )
            trend["p95"].append(
                [timestamp, compute_health.quantile(values, 0.95)]
            )
            trend["max"].append([timestamp, max(values)])
        return trend

    def get_reporting_instances(self, window=None) -> dict:
        names = {_fake_id(f[0]): f for f in _FAKE_FLEET}
        return {
            instance_id: {
                "name": names[instance_id][0],
                "project_id": names[instance_id][1],
            }
            for instance_id in _fake_symptoms()
        }

    def get_instance_symptoms(self, thresholds, rate_window) -> dict:
        symptoms = {s: {} for s in instance_health.METRIC_SYMPTOMS}
        for instance_id, found in _fake_symptoms().items():
            for symptom, entry in found.items():
                prefix = instance_health.SYMPTOM_THRESHOLDS[symptom][0]
                if _symptom_value(entry) <= thresholds[f"{prefix}_warning"]:
                    continue
                if symptom == instance_health.DISK:
                    symptoms[symptom][instance_id] = {
                        "value": entry[0],
                        "device": entry[1],
                    }
                else:
                    symptoms[symptom][instance_id] = entry
        return symptoms

    def get_instance_last_seen(self, instance_ids) -> dict:
        now = time.time()
        reporting = _fake_symptoms()
        last_seen = {}
        for instance_id in instance_ids:
            if instance_id in reporting:
                last_seen[instance_id] = now - 12
            elif instance_id == _fake_id("etl-0002"):
                last_seen[instance_id] = now - 35 * 60
        return last_seen

    def get_instance_trends(
        self, instance_id, range_key, rate_window, at=None
    ):
        found = _fake_symptoms().get(instance_id) or {}
        return {
            symptom: _fake_trend(
                instance_id, _symptom_value(found.get(symptom, 0.0)), range_key
            )
            if instance_id in _fake_symptoms()
            else []
            for symptom in instance_health.METRIC_SYMPTOMS
        }

    def get_symptom_trends(
        self, symptom, instance_ids, range_key, rate_window, at=None
    ):
        symptoms = _fake_symptoms()
        return {
            instance_id: _fake_trend(
                instance_id,
                _symptom_value(symptoms[instance_id][symptom]),
                range_key,
            )
            for instance_id in instance_ids
            if symptom in (symptoms.get(instance_id) or {})
        }

    # Top consumers (sample data in fake_top_consumers) ------------------

    def get_top_consumers_projects(self) -> dict:
        from grian_ui.datasource import fake_top_consumers

        return fake_top_consumers.get_projects()

    def get_top_consumers_instances(
        self, resource: str, project_id: str = None, limit: int = 10
    ) -> list:
        from grian_ui.datasource import fake_top_consumers

        return fake_top_consumers.get_instances(resource, project_id, limit)

    def get_top_consumers_trend(
        self,
        resource: str,
        by: str,
        keys: list,
        project_id: str = None,
        range_key: str = "24h",
        at: float = None,
    ) -> dict:
        from grian_ui.datasource import fake_top_consumers

        return fake_top_consumers.get_trend(
            resource, by, keys, project_id, range_key, at
        )

    # Network health (sample data in grian_ui.datasource.fake_network)

    def get_network_interfaces(self) -> dict:
        """Get sample interface rates (see fake_network)."""
        return fake_network.fake_interfaces()

    def get_network_traffic_trend(
        self, range_key: str, at: float = None
    ) -> dict:
        """Get a sample cloud traffic trend."""
        return fake_network.fake_traffic_trend(range_key, at)

    # Provisioning --------------------------------------------------------

    def get_provisioning_boots(self, range_key: str) -> list:
        """Get sample boots (see grian_ui.datasource.fake_provisioning)."""
        from grian_ui.datasource import fake_provisioning

        return fake_provisioning.boots_in_range(range_key)


def _make_fake_hosts():
    rng = random.Random(42)  # nosec
    hosts = []
    for i in range(1, 25):
        hosts.append(
            {
                "name": f"compute-{i:02d}",
                "cpu": round(rng.uniform(12, 62), 1),  # nosec
                "iowait": round(rng.uniform(0, 4), 1),  # nosec
                "frequency": 2100 + 100 * rng.randint(0, 10),  # nosec
                "age": rng.randint(5, 70),  # nosec
                "seed": i,
            }
        )
    # A few hosts that need attention.
    hosts[2].update(cpu=93.5)  # critical CPU
    hosts[9].update(cpu=48.0, iowait=23.0)  # critical I/O wait
    hosts[6].update(cpu=81.2)  # warning CPU
    hosts[13].update(cpu=37.5, iowait=13.5)  # warning I/O wait
    return hosts


_FAKE_HOSTS = _make_fake_hosts()


def _fake_series(level, seed, range_key, now=None):
    """Deterministic wavy series around ``level`` for the given range."""
    points = []
    for i, timestamp in enumerate(timeseries.range_grid(range_key, now)):
        value = (
            level + 6 * math.sin((i + seed * 7) / 5.0) + 2 * math.cos(i * seed)
        )
        points.append([float(timestamp), round(min(100, max(0, value)), 1)])
    return points


def get_client(request=None) -> DatasourceClient:
    """
    Get a fake telemetry client instance.

    Args:
        request: HTTP request (unused for fake client, accepted for interface compatibility)

    Returns:
        FakeTelemetryClient instance
    """
    return FakeTelemetryClient()
