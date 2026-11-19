#
# SPDX-License-Identifier: Apache-2.0

"""
URLconf for unit tests: mirrors horizon's "horizon:admin:<panel>" URL
namespaces so the panel templates can be rendered without horizon.

Add each panel's URLs to ``PANELS``.
"""

from django.urls import include
from django.urls import path

from grian_ui.content.compute_host_health import urls as chh_urls
from grian_ui.content.instance_health import urls as instance_health_urls
from grian_ui.content.network_health import urls as network_health_urls
from grian_ui.content.provisioning import urls as provisioning_urls
from grian_ui.content.top_consumers import urls as top_consumers_urls
from grian_ui.content.volume_metrics import urls as volume_metrics_urls


PANELS = {
    "compute_host_health": chh_urls,
    "instance_health": instance_health_urls,
    "top_consumers": top_consumers_urls,
    "network_health": network_health_urls,
    "volume_metrics": volume_metrics_urls,
    "provisioning": provisioning_urls,
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
