#
# SPDX-License-Identifier: Apache-2.0

"""
Helpers for the Project dashboard (Project -> Telemetry) panels.

Aetos filters queries by the token's project only for users without an
admin role: an administrator browsing the Project dashboard would see every
project. Project panels must therefore add the current project to every
selector themselves, with project_matcher(), and cache with
grian_ui.cache.project_cache_key().

Aetos also rejects selectors without a metric name for project users
(``{__name__=~"..."}``), so name every metric explicitly.
"""

import re


# Keystone project IDs are UUID hex strings; allow the usual ID characters
# and refuse anything that could break out of a PromQL string.
_PROJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def project_id(request):
    """The request's current project ID, validated for use in PromQL."""
    value = getattr(request.user, "project_id", None) or ""
    if not _PROJECT_ID.fullmatch(value):
        raise ValueError(f"invalid project ID {value!r}")
    return value


def project_matcher(request):
    """
    PromQL label matcher for the current project, e.g. ``project="abc"``.

    Put it in every selector of a project panel query:
    ``ceilometer_cpu{%s}`` % project_matcher(request).
    """
    return f'project="{project_id(request)}"'
