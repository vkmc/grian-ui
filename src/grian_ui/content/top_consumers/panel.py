import horizon

from django.utils.translation import gettext_lazy as _


class TopConsumers(horizon.Panel):
    name = _("Top Consumers")
    slug = "top_consumers"
    permissions = ("openstack.roles.admin",)
    urls = "grian_ui.content.top_consumers.urls"
