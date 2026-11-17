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
    def get_telemetry_overview(self) -> dict:
        """Get overview telemetry data."""
        pass

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
