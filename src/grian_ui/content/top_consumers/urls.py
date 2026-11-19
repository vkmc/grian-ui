from django.urls import re_path

from grian_ui.content.top_consumers import views


urlpatterns = [
    re_path(r"^$", views.TopConsumersIndexView.as_view(), name="index"),
    re_path(r"^summary/$", views.SummaryView.as_view(), name="summary"),
    re_path(r"^top/$", views.TopTableView.as_view(), name="top"),
    re_path(r"^trend/$", views.TrendView.as_view(), name="trend"),
]
