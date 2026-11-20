#
# SPDX-License-Identifier: Apache-2.0

from django.urls import re_path

from grian_ui.content.project_usage import views


urlpatterns = [
    re_path(r"^$", views.UsageIndexView.as_view(), name="index"),
    re_path(r"^summary/$", views.SummaryView.as_view(), name="summary"),
    re_path(r"^chart/$", views.ChartView.as_view(), name="chart"),
    re_path(r"^table/$", views.TableView.as_view(), name="table"),
    re_path(r"^csv/$", views.CsvView.as_view(), name="csv"),
]
