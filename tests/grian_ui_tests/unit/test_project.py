#
# SPDX-License-Identifier: Apache-2.0

from unittest import mock

from grian_ui import project
from grian_ui_tests.unit import base


def _request(project_id):
    request = mock.MagicMock()
    request.user.project_id = project_id
    return request


class ProjectMatcherTests(base.TestCase):
    def test_matcher(self):
        self.assertEqual(
            project.project_matcher(_request("0a1b2c")), 'project="0a1b2c"'
        )

    def test_project_id(self):
        project_id = "8f14e45fceea167a5a36dedd4bea2543"
        self.assertEqual(project.project_id(_request(project_id)), project_id)

    def test_unsafe_or_missing_ids_are_refused(self):
        for value in (None, "", 'a"}', "a b", "a\\", "x" * 65, "a\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                project.project_matcher(_request(value))
