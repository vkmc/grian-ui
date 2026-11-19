#
# SPDX-License-Identifier: Apache-2.0

"""
URLconf for unit tests: mirrors horizon's "horizon:admin:<panel>" URL
namespaces so the panel templates can be rendered without horizon.

Add each panel's URLs to ``PANELS``.
"""

from django.urls import include
from django.urls import path

from grian_ui.content.network_health import urls as network_health_urls
from grian_ui.content.volume_metrics import urls as volume_metrics_urls


PANELS = {
    "network_health": network_health_urls,
    "volume_metrics": volume_metrics_urls,
}

admin = (
    [
        path(f"{name}/", include((module.urlpatterns, name)))
        for name, module in PANELS.items()
    ],
    "admin",
)
horizon = ([path("admin/", include(admin))], "horizon")

urlpatterns = [path("dashboard/", include(horizon))]
