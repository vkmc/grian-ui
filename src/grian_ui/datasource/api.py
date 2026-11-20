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

    # Project Compute (see grian_ui.project_compute) -----------------------
    # ``matcher`` is grian_ui.project.project_matcher(request): every query
    # is scoped to the current project.

    @abstractmethod
    def get_project_compute_snapshot(self, matcher: str) -> dict:
        """
        Get the current values of the project's running instances.

        Returns:
            dict: ``{"instances": {uuid: {"name", "vcpus", "memory_alloc",
            "cpu", "cpu_wait", "memory", "swap", "disks": {device:
            {"used", "capacity", "write", "latency"}}}}}``
        """
        pass

    @abstractmethod
    def get_project_compute_rightsizing(self, matcher: str) -> dict:
        """
        Get the 7-day statistics of the project's instances.

        Returns:
            dict: ``{"instances": {uuid: {"cpu_avg", "cpu_p95",
            "memory_avg", "swap", "hours"}}}``
        """
        pass

    @abstractmethod
    def get_project_compute_forecast(self, matcher: str, days: int) -> dict:
        """
        Get the growth of what running instances hold over ``days``.

        Returns:
            dict: ``{"slopes": {quota: per second}, "hours", "days"}``
        """
        pass

    @abstractmethod
    def get_project_compute_trend(
        self, matcher: str, resource: str, range_key: str, at: float = None
    ) -> dict:
        """
        Get what running instances held of a quota over a chart range.

        Returns:
            dict: ``{"points": [[ts, value], ...]}``
        """
        pass

    @abstractmethod
    def get_project_compute_cpu_series(
        self, matcher: str, instance_ids: list, at: float = None
    ) -> dict:
        """
        Get the 7-day CPU use (cores) of some instances.

        Returns:
            dict: ``{"series": {uuid: [[ts, cores], ...]}}``
        """
        pass

    @abstractmethod
    def get_project_compute_instance_trend(
        self, matcher: str, instance_id: str, at: float = None
    ) -> dict:
        """
        Get the 7-day CPU (cores) and memory (MiB) of one instance.

        Returns:
            dict: ``{"cpu": [[ts, v], ...], "memory": [[ts, v], ...]}``
        """
        pass
