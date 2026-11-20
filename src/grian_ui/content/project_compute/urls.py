#
# SPDX-License-Identifier: Apache-2.0

from django.urls import path

from grian_ui.content.project_compute import views


urlpatterns = [
    path("", views.ProjectComputeIndexView.as_view(), name="index"),
    path("summary/", views.SummaryView.as_view(), name="summary"),
    path("quota/", views.QuotaView.as_view(), name="quota"),
    path("trend/", views.TrendView.as_view(), name="trend"),
    path("idle/", views.IdleView.as_view(), name="idle"),
    path("rightsizing/", views.RightsizingView.as_view(), name="rightsizing"),
    path("instance/", views.InstanceView.as_view(), name="instance"),
    path("attention/", views.AttentionView.as_view(), name="attention"),
]
