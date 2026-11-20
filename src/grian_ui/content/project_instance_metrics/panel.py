#
# SPDX-License-Identifier: Apache-2.0

import horizon

from django.utils.translation import gettext_lazy as _


class InstanceMetrics(horizon.Panel):
    name = _("Instance metrics")
    slug = "instance_metrics"
    permissions = (
        "openstack.services.compute",
        "openstack.services.metric-storage",
    )
    urls = "grian_ui.content.project_instance_metrics.urls"
    # Not in the sidebar: one instance at a time, reached from the links on
    # the other Telemetry pages (or the picker). Horizon still registers
    # the URLs of a panel with nav = False.
    nav = False
