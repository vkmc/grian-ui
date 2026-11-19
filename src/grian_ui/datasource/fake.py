#
# SPDX-License-Identifier: Apache-2.0

"""
Fake datasource implementation for development and testing.
Provides realistic sample telemetry data.
"""

import time

from grian_ui.datasource import fake_storage
from grian_ui.datasource.api import DatasourceClient


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


def get_client(request=None) -> DatasourceClient:
    """
    Get a fake telemetry client instance.

    Args:
        request: HTTP request (unused for fake client, accepted for interface compatibility)

    Returns:
        FakeTelemetryClient instance
    """
    return FakeTelemetryClient()
