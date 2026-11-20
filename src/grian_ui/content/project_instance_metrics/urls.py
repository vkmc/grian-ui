#
# SPDX-License-Identifier: Apache-2.0

from django.urls import re_path

from grian_ui.content.project_instance_metrics import views


urlpatterns = [
    re_path(r"^$", views.IndexView.as_view(), name="index"),
    re_path(r"^info/$", views.InfoView.as_view(), name="info"),
    re_path(r"^tiles/$", views.TilesView.as_view(), name="tiles"),
    re_path(
        r"^charts/(?P<group>cpu|memory|disk|network)/$",
        views.ChartsView.as_view(),
        name="charts",
    ),
]
