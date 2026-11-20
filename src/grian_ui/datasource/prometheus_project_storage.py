#
# SPDX-License-Identifier: Apache-2.0

"""
Prometheus queries of the Project -> Telemetry -> Storage panel.

Ceilometer reports the size of every volume (``volume.size``, GiB) and
volume snapshot (``volume.snapshot.size``, GiB) with the owning project in
the ``project`` label. Every selector carries the current project's matcher
(grian_ui.project.project_matcher): Aetos only adds it for users without
an admin role. Every metric is named: Aetos rejects ``{__name__=~...}``
selectors for project users.

Aetos has no range query endpoint, so the history comes from subqueries
(``(expr)[30d:1d]``) through ``PrometheusTelemetryClient.query_matrix``.
Each point takes the last sample of each volume in the CURRENT_WINDOW
before it, so a deleted volume stops counting within that window. Samples
are grouped by ``resource`` (the volume or snapshot ID) first: when the
other labels of a volume change (a rename changes ``resource_name``), its
old and new series both have a sample in the window, and it would count
twice.
"""

import re

from grian_ui import project_storage as ps


#: Look-back of each point for the last sample of a volume or snapshot:
#: longer than the Ceilometer polling interval (300 s by default).
CURRENT_WINDOW = "10m"

VOLUME_SIZE = "ceilometer_volume_size"
SNAPSHOT_SIZE = "ceilometer_volume_snapshot_size"

_MATCHER = re.compile(r'project="[A-Za-z0-9_-]{1,64}"')


def _checked(project_matcher):
    """Refuse anything but ``project="<id>"`` (see project_matcher())."""
    if not isinstance(project_matcher, str) or not _MATCHER.fullmatch(
        project_matcher
    ):
        raise ValueError(f"invalid project matcher {project_matcher!r}")
    return project_matcher


def _selector(metric, project_matcher):
    """The last size of each volume (or snapshot), one series per ID."""
    return (
        f"max by (resource) (last_over_time({metric}"
        f"{{{_checked(project_matcher)}}}[{CURRENT_WINDOW}]))"
    )


def _days(days):
    days = int(days)
    if days < 1:
        raise ValueError(f"invalid number of days {days!r}")
    return days


def footprint_queries(project_matcher, days):
    """GB of volumes and of snapshots, one point per day over ``days``."""
    days = _days(days)
    return {
        "volumes": (
            f"(sum({_selector(VOLUME_SIZE, project_matcher)}))[{days}d:1d]"
        ),
        "snapshots": (
            f"(sum({_selector(SNAPSHOT_SIZE, project_matcher)}))[{days}d:1d]"
        ),
    }


def trend_queries(project_matcher, days):
    """GB and number of volumes and snapshots, every TREND_STEP."""
    days = _days(days)
    step = ps.TREND_STEP
    queries = {}
    for name, metric in (
        ("volumes", VOLUME_SIZE),
        ("snapshots", SNAPSHOT_SIZE),
    ):
        selector = _selector(metric, project_matcher)
        queries[f"{name}_gb"] = f"(sum({selector}))[{days}d:{step}]"
        queries[f"{name}_count"] = f"(count({selector}))[{days}d:{step}]"
    return queries


def _points(client, query, at):
    series = client.query_matrix(query, ndigits=3, time=at)
    # sum() and count() return at most one series
    return series[0][1] if series else []


def get_footprint(client, project_matcher, days, at=None):
    """
    Volume and snapshot GB of the project, one point per day.

    Args:
        days: days to query (at most the metrics retention)
        at: evaluation time (epoch), the time the chart grid is built from

    Returns:
        dict: ``{"volumes": [[ts, gb], ...], "snapshots": [...]}``
    """
    return {
        name: _points(client, query, at)
        for name, query in footprint_queries(project_matcher, days).items()
    }


def _fill(points, timestamps):
    """``points`` on ``timestamps``, 0 where missing."""
    by_ts = dict((t, v) for t, v in points)
    return [[t, by_ts.get(t, 0)] for t in timestamps]


def get_trend(client, project_matcher, days, at=None):
    """
    History for the quota forecasts, every TREND_STEP over ``days``.

    At a time where the project had volumes but no snapshots (or the
    reverse), the missing one counts as 0; times without either are left
    out, so the history starts with the first reported volume or snapshot.

    Returns:
        dict: ``{"gigabytes": [[ts, gb], ...], "volumes": [[ts, n], ...],
        "snapshots": [[ts, n], ...]}``
    """
    found = {
        name: _points(client, query, at)
        for name, query in trend_queries(project_matcher, days).items()
    }
    timestamps = sorted({t for points in found.values() for t, _v in points})
    volumes_gb = _fill(found["volumes_gb"], timestamps)
    snapshots_gb = _fill(found["snapshots_gb"], timestamps)
    return {
        "gigabytes": [
            [t, round(v + s, 3)]
            for (t, v), (_t, s) in zip(volumes_gb, snapshots_gb)
        ],
        "volumes": _fill(found["volumes_count"], timestamps),
        "snapshots": _fill(found["snapshots_count"], timestamps),
    }
