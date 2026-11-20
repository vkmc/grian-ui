#
# SPDX-License-Identifier: Apache-2.0

from django.urls import path

from grian_ui.content.project_network import views


urlpatterns = [
    path("", views.IndexView.as_view(), name="index"),
    path("summary/", views.SummaryView.as_view(), name="summary"),
    path("topology/", views.TopologyView.as_view(), name="topology"),
    path("traffic/", views.TrafficView.as_view(), name="traffic"),
    path("quota/", views.QuotaView.as_view(), name="quota"),
    path(
        "floating-ips/",
        views.FloatingIPsView.as_view(),
        name="floating_ips",
    ),
    path("interfaces/", views.InterfacesView.as_view(), name="interfaces"),
]
