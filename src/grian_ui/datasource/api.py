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

    # Project network (see grian_ui.project_network) ----------------------

    @abstractmethod
    def get_project_network_interfaces(self, project_matcher) -> dict:
        """
        Get the rates of the project's instance network interfaces.

        Args:
            project_matcher: ``project="<id>"``, from
                grian_ui.project.project_matcher(); in every selector

        Returns:
            dict: ``{"interfaces": [{"key", "name", "in_pps", "usual_pps",
            "usual_basis", "in_bps", "out_bps", "lost_pps"}]}``
        """
        pass

    @abstractmethod
    def get_project_network_traffic_trend(
        self, project_matcher, range_key, at=None
    ) -> dict:
        """
        Get the project's inbound and outbound bytes/s over a range.

        Args:
            at: evaluation time (epoch seconds), or None for now

        Returns:
            dict: ``{"in": [[ts, bytes/s], ...], "out": [...]}``
        """
        pass

    @abstractmethod
    def get_project_network_interface_trend(
        self, project_matcher, range_key, at=None
    ) -> dict:
        """
        Get the inbound packets/s of each project interface over a range.

        Returns:
            dict: ``{port key: [[ts, packets/s], ...]}``
        """
        pass
