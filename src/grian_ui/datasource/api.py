#
# SPDX-License-Identifier: Apache-2.0

"""
Datasource API for telemetry data.
Provides an abstraction layer for different telemetry backends.
"""

import importlib

from abc import ABC
from abc import abstractmethod

from grian_ui import config


def get_datasource(request):
    """
    Get the configured datasource backend.

    Returns the datasource implementation based on GRIAN_PLUGIN['datasource']
    setting. Defaults to 'fake' if not configured.
    """
    datasource_type = config.get_datasource_type()
    module_path = f"grian_ui.datasource.{datasource_type}"

    try:
        datasource_module = importlib.import_module(module_path)
        return datasource_module.get_client(request)
    except ImportError:
        # Fallback to fake datasource
        import grian_ui.datasource.fake as fake_datasource

        return fake_datasource.get_client(request)


class DatasourceClient(ABC):
    """Abstract base class for datasource clients."""

    @abstractmethod
    def get_volumes_count(self) -> int:
        """Get volumes count telemetry data."""
        pass

    @abstractmethod
    def get_snapshots_count(self) -> int:
        """Get snapshots count telemetry data."""
        pass

    @abstractmethod
    def get_storage_pools(self) -> list:
        """Get storage pools information."""
        pass

    @abstractmethod
    def get_capacity_predictions(self) -> list:
        """Get capacity predictions for storage pools."""
        pass

    # Storage Health (see grian_ui.storage_health)

    @abstractmethod
    def get_volume_services(self) -> list:
        """
        Get the current status of every Cinder service.

        Returns:
            list: ``{"resource", "binary", "host", "up"}``; ``up`` is None
            for services that stopped reporting in the last 24 hours
        """
        pass

    @abstractmethod
    def get_volume_service_history(self, at: float = None) -> dict:
        """
        Get each Cinder service's status over the last 24 hours.

        Returns:
            dict: resource -> ``[[timestamp, value], ...]``
        """
        pass

    @abstractmethod
    def get_storage_capacity_trend(
        self, range_key: str, pools: list = None, at: float = None
    ) -> dict:
        """
        Get the cloud's allocated, provisioned and total capacity over a
        range, or the physical use of the given pools.

        Returns:
            dict: series name -> ``[[timestamp, value], ...]``
        """
        pass

    @abstractmethod
    def get_storage_images(self) -> dict:
        """
        Get the image store and volume backup totals.

        Returns:
            dict: ``images``, ``image_bytes``, ``image_projects``,
            ``images_downloaded``, ``backups``, ``backup_gib`` (None when
            not reported)
        """
        pass

    @abstractmethod
    def get_compute_host_snapshot(self) -> dict:
        """
        Get one compact sample per compute host.

        Returns:
            dict: ``{"hosts": [{"name", "cpu_avg", "iowait_avg"}],
            "stale": [{"name", "last_seen"}]}``, where ``stale`` only
            covers STALE_RECENT_LOOKBACK (see get_compute_stale_hosts). See
            :mod:`grian_ui.compute_health` for the exact format.
        """
        pass

    @abstractmethod
    def get_compute_stale_hosts(self) -> list:
        """
        Get the hosts that reported in STALE_LOOKBACK but not recently.

        Returns:
            list: ``[{"name", "last_seen"}, ...]``
        """
        pass

    @abstractmethod
    def get_compute_host_details(self, names: list) -> dict:
        """
        Get the current CPU breakdown of the given compute hosts.

        Returns:
            dict: host name -> ``{"cpu", "user", "kernel", "iowait",
            "idle", "frequency", "last_seen"}`` (missing values are None)
        """
        pass

    @abstractmethod
    def get_compute_host_trends(
        self, names: list, range_key: str, at: float = None
    ) -> dict:
        """
        Get the CPU usage trend of the given compute hosts.

        ``at`` is the evaluation time (epoch), the end of the chart grid.

        Returns:
            dict: host name -> ``[[timestamp, percent], ...]``
        """
        pass

    @abstractmethod
    def get_compute_fleet_trend(
        self, range_key: str, at: float = None
    ) -> dict:
        """
        Get the fleet CPU usage trend (median, 95th percentile, max).

        ``at`` is the evaluation time (epoch), the end of the chart grid.

        Returns:
            dict: ``{"p50": [[ts, v], ...], "p95": [...], "max": [...]}``
        """
        pass

    @abstractmethod
    def get_reporting_instances(self, window=None) -> dict:
        """
        Get the instances that reported a cpu sample in ``window`` (a
        duration, ``instance_health.STALE_AFTER`` by default).

        Returns:
            dict: instance id -> ``{"name", "project_id"}``, taken from the
            metric labels. See :mod:`grian_ui.instance_health`.
        """
        pass

    @abstractmethod
    def get_instance_symptoms(
        self, thresholds: dict, rate_window: str
    ) -> dict:
        """
        Get the instances above a warning threshold, per metric symptom.

        Only instances above a warning threshold are returned, so the size
        of the result follows the number of problems, not of instances.

        Returns:
            dict: ``{"cpu": {id: percent}, "memory": {id: MiB/s},
            "disk": {id: {"value": ms, "device": str}},
            "network": {id: packets/s}}``
        """
        pass

    @abstractmethod
    def get_instance_last_seen(self, instance_ids: list) -> dict:
        """
        Get the time of the last cpu sample of at most a page of instances.

        Returns:
            dict: instance id -> epoch seconds (missing if not seen in the
            lookback window)
        """
        pass

    @abstractmethod
    def get_instance_trends(
        self,
        instance_id: str,
        range_key: str,
        rate_window: str,
        at: float = None,
    ) -> dict:
        """
        Get the four symptom measures of one instance over a range.

        ``at`` is the evaluation time (epoch), the end of the chart grid.

        Returns:
            dict: ``{"cpu": [[ts, v], ...], "memory": [...], "disk": [...],
            "network": [...]}``
        """
        pass

    @abstractmethod
    def get_symptom_trends(
        self,
        symptom: str,
        instance_ids: list,
        range_key: str,
        rate_window: str,
        at: float = None,
    ) -> dict:
        """
        Get one symptom measure of at most a page of instances over a range.

        ``at`` is the evaluation time (epoch), the end of the chart grid.

        Returns:
            dict: instance id -> ``[[ts, v], ...]``
        """
        pass

    # Top consumers (see grian_ui.top_consumers) ---------------------------

    @abstractmethod
    def get_top_consumers_projects(self) -> dict:
        """
        Get the current use and allocation of every project with running
        instances.

        Returns:
            dict: ``{"projects": [{"id", "instances", "cpu", "vcpus",
            "memory", "memory_alloc", "disk_read", "disk_write", "net_in",
            "net_out"}, ...]}``
        """
        pass

    @abstractmethod
    def get_top_consumers_instances(
        self, resource: str, project_id: str = None, limit: int = 10
    ) -> list:
        """
        Get the instances using the most of ``resource``.

        Returns:
            list: at most ``limit`` ``{"id", "name", "project_id",
            "value", "vcpus", "memory_alloc"}``
        """
        pass

    @abstractmethod
    def get_top_consumers_trend(
        self,
        resource: str,
        by: str,
        keys: list,
        project_id: str = None,
        range_key: str = "24h",
        at: float = None,
    ) -> dict:
        """
        Get ``resource`` over ``range_key`` (see grian_ui.timeseries) for
        some projects or instances, plus the total.

        Returns:
            dict: ``{"series": {key: [[ts, v], ...]}, "total": [...]}``
        """
        pass

    @abstractmethod
    def get_network_interfaces(self) -> dict:
        """
        Get the inbound, usual and lost packet rates of every instance
        interface, plus the cloud's traffic.

        Returns:
            dict: ``{"interfaces": [{"key", "project", "in_pps",
            "usual_pps", "lost_pps"}], "traffic": {"in_bps", "out_bps"}}``.
            See :mod:`grian_ui.network_health` for the exact format.
        """
        pass

    @abstractmethod
    def get_network_traffic_trend(
        self, range_key: str, at: float = None
    ) -> dict:
        """
        Get the cloud's inbound and outbound traffic over a range.

        ``at`` is the evaluation time (epoch), the end of the chart grid.

        Returns:
            dict: ``{"in": [[ts, bytes/s], ...], "out": [...]}``
        """
        pass

    @abstractmethod
    def get_provisioning_boots(self, range_key: str) -> list:
        """
        Get the instance boots of a Provisioning range ("24h", "7d", ...).

        Returns:
            list: one dict per boot (``id``, ``name``, ``project_id``,
            ``host``, ``seconds``, ``at``), see :mod:`grian_ui.provisioning`
        """
        pass
