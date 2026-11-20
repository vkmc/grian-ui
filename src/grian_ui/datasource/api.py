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

    # Project instance metrics (see grian_ui.project_instance_metrics) -----

    @abstractmethod
    def get_project_instance_metrics(
        self,
        project_matcher,
        group,
        instance_id,
        range_key,
        min_rate_window,
        at=None,
    ) -> dict:
        """
        Get the time series of one chart group of one instance.

        Args:
            project_matcher: grian_ui.project.project_matcher(request), the
                current project, put in every selector
            group: one of grian_ui.project_instance_metrics.GROUPS
            instance_id: server UUID
            range_key: one of grian_ui.timeseries.RANGES
            min_rate_window: shortest rate() window, e.g. ``"10m"``
            at: evaluation time (epoch seconds), or None for now

        Returns:
            dict: ``{series: [[timestamp, value], ...]}`` with every
            series of the group (see project_instance_metrics.SERIES)
        """
        pass
