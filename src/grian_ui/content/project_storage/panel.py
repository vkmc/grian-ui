#
# SPDX-License-Identifier: Apache-2.0

"""
Project -> Telemetry -> Storage panel.
"""

import horizon

from django.utils.translation import gettext_lazy as _


class ProjectStorage(horizon.Panel):
    name = _("Storage")
    slug = "telemetry_storage"
    # the same requirements as Horizon's Project -> Volumes panel: the
    # page starts from the Block Storage API (quota, volumes, snapshots)
    permissions = (
        (
            "openstack.services.block-storage",
            "openstack.services.block-store",
            "openstack.services.volume",
            "openstack.services.volumev3",
        ),
        # and the metrics through Aetos
        "openstack.services.metric-storage",
    )
    policy_rules = (("volume", "volume:get_all"),)
    urls = "grian_ui.content.project_storage.urls"
