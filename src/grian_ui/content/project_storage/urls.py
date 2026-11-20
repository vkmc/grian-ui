#
# SPDX-License-Identifier: Apache-2.0

"""
URLs for the Project Storage panel.
"""

from django.urls import re_path

from grian_ui.content.project_storage import views


urlpatterns = [
    re_path(r"^$", views.IndexView.as_view(), name="index"),
    re_path(r"^summary/$", views.SummaryView.as_view(), name="summary"),
    re_path(r"^quota/$", views.QuotaView.as_view(), name="quota"),
    re_path(r"^footprint/$", views.FootprintView.as_view(), name="footprint"),
    re_path(r"^cleanup/$", views.CleanupView.as_view(), name="cleanup"),
    re_path(r"^volumes/$", views.VolumesView.as_view(), name="volumes"),
    re_path(r"^images/$", views.ImagesView.as_view(), name="images"),
]
