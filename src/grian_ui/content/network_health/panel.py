import horizon

from django.utils.translation import gettext_lazy as _


class NetworkHealth(horizon.Panel):
    name = _("Network Health")
    slug = "network_health"
    permissions = ("openstack.roles.admin",)
    urls = "grian_ui.content.network_health.urls"
