#
# SPDX-License-Identifier: Apache-2.0

"""
URLconf for unit tests: mirrors horizon's "horizon:admin:<panel>" and
"horizon:project:<panel>" URL namespaces so the panel templates can be
rendered without horizon.

Add each Admin panel's URLs to ``PANELS`` and each Project panel's URLs to
``PROJECT_PANELS``.
"""

from django.urls import include
from django.urls import path

from grian_ui.content.project_network import urls as project_network_urls
from grian_ui.content.volume_metrics import urls as volume_metrics_urls


PANELS = {
    "volume_metrics": volume_metrics_urls,
}

PROJECT_PANELS = {
    "telemetry_network": project_network_urls,
}


def _dashboard(panels, name):
    return (
        [
            path(f"{panel}/", include((module.urlpatterns, panel)))
            for panel, module in panels.items()
        ],
        name,
    )


horizon = (
    [
        path("admin/", include(_dashboard(PANELS, "admin"))),
        path("project/", include(_dashboard(PROJECT_PANELS, "project"))),
    ],
    "horizon",
)

urlpatterns = [path("dashboard/", include(horizon))]
