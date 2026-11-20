#
# SPDX-License-Identifier: Apache-2.0

import horizon

from django.utils.translation import gettext_lazy as _


class ProjectUsage(horizon.Panel):
    name = _("Usage")
    slug = "telemetry_usage"
    permissions = (
        "openstack.services.compute",
        "openstack.services.metric-storage",
    )
    policy_rules = (("telemetry", "telemetry:query"),)
