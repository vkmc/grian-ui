from django.urls import re_path

from grian_ui.content.volume_metrics import views


urlpatterns = [
    re_path(r"^$", views.VolumesMetricsIndexView.as_view(), name="index"),
    re_path(
        r"^metrics-update/$",
        views.MetricsUpdateView.as_view(),
        name="metrics_update",
    ),
    re_path(
        r"^attention/$",
        views.AttentionView.as_view(),
        name="attention",
    ),
    re_path(r"^capacity/$", views.CapacityView.as_view(), name="capacity"),
    re_path(r"^pools/$", views.PoolsView.as_view(), name="pools"),
    re_path(
        r"^pools/lookup/$",
        views.PoolLookupView.as_view(),
        name="pool_lookup",
    ),
    re_path(
        r"^pools/suggestions/$",
        views.PoolSuggestionsView.as_view(),
        name="pool_suggestions",
    ),
    re_path(r"^services/$", views.ServicesView.as_view(), name="services"),
    re_path(r"^backends/$", views.BackendsView.as_view(), name="backends"),
    re_path(r"^projects/$", views.ProjectsView.as_view(), name="projects"),
    re_path(r"^images/$", views.ImagesView.as_view(), name="images"),
]
