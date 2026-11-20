#
# SPDX-License-Identifier: Apache-2.0

import horizon

from django.utils.translation import gettext_lazy as _


class ProjectCompute(horizon.Panel):
    name = _("Compute")
    slug = "telemetry_compute"
    # the instance list of the project, as Project > Compute > Instances
    permissions = (
        "openstack.services.compute",
        "openstack.services.metric-storage",
    )
    policy_rules = (("compute", "os_compute_api:servers:index"),)
    urls = "grian_ui.content.project_compute.urls"
