#
# SPDX-License-Identifier: Apache-2.0

from django.test import override_settings

from grian_ui import config
from grian_ui_tests.unit import base


class StorageStaleMinutesTests(base.TestCase):
    def test_default(self):
        self.assertEqual(config.get_storage_stale_minutes(), 10)

    @override_settings(GRIAN_PLUGIN={"storage_stale_minutes": 30})
    def test_override(self):
        self.assertEqual(config.get_storage_stale_minutes(), 30)

    def test_invalid(self):
        for value in (0, -5, 361, 1.5, "10", True, None):
            with override_settings(
                GRIAN_PLUGIN={"storage_stale_minutes": value}
            ):
                self.assertRaisesRegex(
                    config.ConfigurationError,
                    "storage_stale_minutes",
                    config.get_storage_stale_minutes,
                )

    @override_settings(GRIAN_PLUGIN={"storage_stale_minutes": 360})
    def test_maximum(self):
        self.assertEqual(config.get_storage_stale_minutes(), 360)
