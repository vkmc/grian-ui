#
# SPDX-License-Identifier: Apache-2.0

import horizon

from django.utils.translation import gettext_lazy as _


class ProjectNetwork(horizon.Panel):
    name = _("Network")
    slug = "telemetry_network"
    # the page reads the project's Neutron resources with the user's token
    permissions = (
        "openstack.services.network",
        "openstack.services.metric-storage",
    )
    urls = "grian_ui.content.project_network.urls"
