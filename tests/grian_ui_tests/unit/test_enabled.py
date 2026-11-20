#
# SPDX-License-Identifier: Apache-2.0

import importlib
import pkgutil

from grian_ui.local import enabled
from grian_ui_tests.unit import base


def _enabled_files():
    for module in pkgutil.iter_modules(enabled.__path__):
        yield (
            module.name,
            importlib.import_module(f"{enabled.__name__}.{module.name}"),
        )


class EnabledFilesTests(base.TestCase):
    def test_app_is_added_once_by_the_panel_group(self):
        # Horizon does not de-duplicate ADD_INSTALLED_APPS, and skips a
        # disabled panel's file entirely, so only the panel group adds it.
        adding = [
            name
            for name, module in _enabled_files()
            if getattr(module, "ADD_INSTALLED_APPS", None)
        ]
        self.assertEqual(adding, ["_1000_admin_telemetry_panel_group"])
        group = dict(_enabled_files())["_1000_admin_telemetry_panel_group"]
        self.assertEqual(group.ADD_INSTALLED_APPS, ["grian_ui"])

    def test_static_files_are_not_auto_discovered(self):
        # would add vendored htmx and Chart.js to every Horizon page
        for name, module in _enabled_files():
            self.assertFalse(
                getattr(module, "AUTO_DISCOVER_STATIC_FILES", False), name
            )

    def test_panels_belong_to_the_telemetry_group(self):
        for name, module in _enabled_files():
            if hasattr(module, "PANEL"):
                self.assertEqual(module.PANEL_GROUP, "telemetry", name)
                self.assertEqual(
                    module.PANEL_DASHBOARD, _dashboard_of(name), name
                )

    def test_panel_groups(self):
        groups = {
            name: module.PANEL_GROUP_DASHBOARD
            for name, module in _enabled_files()
            if hasattr(module, "PANEL_GROUP_DASHBOARD")
        }
        self.assertEqual(
            groups,
            {
                "_1000_admin_telemetry_panel_group": "admin",
                "_1100_project_telemetry_panel_group": "project",
            },
        )
        group = dict(_enabled_files())["_1100_project_telemetry_panel_group"]
        self.assertEqual(group.PANEL_GROUP, "telemetry")
        self.assertEqual(group.PANEL_GROUP_NAME, "Telemetry")


def _dashboard_of(name):
    # _10xx files are Admin panels, _11xx files Project panels
    return "project" if name.startswith("_11") else "admin"
