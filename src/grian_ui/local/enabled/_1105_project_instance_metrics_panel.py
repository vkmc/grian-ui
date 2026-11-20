# SPDX-License-Identifier: Apache-2.0

# The name of the panel to be added to HORIZON_CONFIG. Required.
PANEL = "instance_metrics"

# The name of the dashboard the PANEL associated with. Required.
PANEL_DASHBOARD = "project"

# The name of the panel group the PANEL is associated with.
PANEL_GROUP = "telemetry"

# Python panel class of the PANEL to be added.
ADD_PANEL = "grian_ui.content.project_instance_metrics.panel.InstanceMetrics"

# The grian_ui app is added by _1000_admin_telemetry_panel_group.py (which
# must be installed too); Horizon does not de-duplicate ADD_INSTALLED_APPS.
