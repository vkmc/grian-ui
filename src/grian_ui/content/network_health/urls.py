from django.urls import re_path

from grian_ui.content.network_health import views


urlpatterns = [
    re_path(r"^$", views.NetworkHealthIndexView.as_view(), name="index"),
    re_path(r"^summary/$", views.SummaryView.as_view(), name="summary"),
    re_path(r"^agents/$", views.AgentsView.as_view(), name="agents"),
    re_path(r"^traffic/$", views.TrafficView.as_view(), name="traffic"),
    re_path(r"^routers/$", views.RoutersView.as_view(), name="routers"),
    re_path(r"^drops/$", views.DropsView.as_view(), name="drops"),
    re_path(
        r"^floating-ips/$",
        views.FloatingIPsView.as_view(),
        name="floating_ips",
    ),
]
