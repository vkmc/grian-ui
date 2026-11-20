#
# SPDX-License-Identifier: Apache-2.0

"""
Project -> Telemetry -> Storage: quota usage, footprint and clean-up.

Pure functions, no request handling. Their inputs are the compact dicts of
:mod:`grian_ui.project_storage_inventory` (Block Storage, Image and Compute
APIs, which know what holds quota, attachment state and ages) and the
series of :mod:`grian_ui.datasource.prometheus_project_storage`
(Ceilometer ``volume_size`` and ``volume_snapshot_size`` history, for the
footprint chart and the forecasts).

Compact formats (times are epoch seconds, sizes in GB unless noted):

* block storage: ``{"limits": {"gigabytes", "volumes", "snapshots",
  "backups", "backups_used"}, "volumes": [...], "snapshots": [...],
  "backup_enabled": bool}``; a limit is None when unlimited
* volume: ``{"id", "name", "size", "status", "server_ids", "created",
  "updated", "image_id"}`` (``image_id``: the image the volume was created
  from, or None)
* snapshot: ``{"id", "name", "size", "status", "volume_id", "created"}``
* server: ``{"id", "name", "image_id"}`` (``image_id`` None when booted
  from a volume)
* image: ``{"id", "name", "size" (bytes), "created", "visibility"}``
"""

import math
import time

from django.utils.translation import gettext as _
from django.utils.translation import ngettext


#: Footprint chart ranges: key -> days. One point per day (subquery step).
FOOTPRINT_RANGES = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_FOOTPRINT_RANGE = "30d"
DAY_SECONDS = 86400

#: The forecasts extend the trend of this many days (fewer when the metrics
#: retention is shorter), sampled every TREND_STEP.
FORECAST_LOOKBACK_DAYS = 30
TREND_STEP = "6h"
TREND_STEP_SECONDS = 6 * 3600
#: Least history, in days, a forecast needs ("Not enough history" below).
MIN_FORECAST_HISTORY_DAYS = 3
#: Forecasts further away than this many days are shown as "not soon".
FORECAST_HORIZON_DAYS = 365

#: Bounded sections.
CLEANUP_LIMIT = 10
VOLUMES_LIMIT = 25
IMAGES_LIMIT = 25

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"

#: Clean-up candidate severities (tags): review > consider > keep?
SEVERITY_REVIEW = 2
SEVERITY_CONSIDER = 1
SEVERITY_KEEP = 0

#: The only image visibility no other project can boot from: a shared,
#: community or public image may be used by other projects, whose
#: instances this project cannot see.
VISIBILITY_PRIVATE = "private"

STATUS_AVAILABLE = "available"
STATUS_IN_USE = "in-use"

#: Forecast states.
FORECAST_NO_HISTORY = "no_history"
FORECAST_FLAT = "flat"
FORECAST_FULL = "full"
FORECAST_FAR = "far"
FORECAST_DAYS = "days"
FORECAST_UNLIMITED = "unlimited"

GIB = 1024**3


def footprint_range(key):
    """``(key, days)``, falling back to the default range."""
    if key not in FOOTPRINT_RANGES:
        key = DEFAULT_FOOTPRINT_RANGE
    return key, FOOTPRINT_RANGES[key]


def history_days(days, retention_days):
    """
    Days of metrics a look-back of ``days`` can find.

    Prometheus keeps ``retention_days`` (config.get_metrics_retention_days):
    queries never ask for more, and pages say how much history exists.
    """
    return max(1, min(days, retention_days))


def day_grid(days, now=None):
    """
    Timestamps a daily subquery ``expr[<days>d:1d]`` returns at ``now``.

    Subquery steps are aligned to multiples of the step (midnight UTC), so
    the last point is the last midnight at or before ``now``.
    """
    end = int((now if now is not None else time.time()) // DAY_SECONDS)
    end *= DAY_SECONDS
    return [end - (days - 1 - i) * DAY_SECONDS for i in range(days)]


def _by_slot(points, step):
    return {int(round(t / step)) * step: v for t, v in points}


def footprint_chart(data, range_key, retention_days, now):
    """
    The stacked footprint chart: volumes, then snapshots, per day.

    A day where only one of the two series has data counts the other as
    0 (a project with volumes but no snapshots). Days without any data are
    None (gaps), so a range longer than the history shows where it starts.

    Args:
        data: ``{"volumes": [[ts, gb], ...], "snapshots": [...]}``
        now: the evaluation time of the queries

    Returns:
        dict: ``range``, ``timestamps``, ``volumes``, ``snapshots`` (lists
        of the same length), ``days``, ``history_days`` (what the retention
        allows), ``beyond_retention``, ``first_ts`` (first day with data,
        or None), ``has_data``, ``latest`` (``{"volumes", "snapshots",
        "total"}`` of the last day with data, or None)
    """
    range_key, days = footprint_range(range_key)
    grid = day_grid(days, now)
    volumes = _by_slot(data.get("volumes") or [], DAY_SECONDS)
    snapshots = _by_slot(data.get("snapshots") or [], DAY_SECONDS)
    vol_values, snap_values = [], []
    first_ts, latest = None, None
    for ts in grid:
        vol, snap = volumes.get(ts), snapshots.get(ts)
        if vol is None and snap is None:
            vol_values.append(None)
            snap_values.append(None)
            continue
        vol, snap = vol or 0, snap or 0
        vol_values.append(vol)
        snap_values.append(snap)
        if first_ts is None:
            first_ts = ts
        latest = {"volumes": vol, "snapshots": snap, "total": vol + snap}
    return {
        "range": range_key,
        "timestamps": grid,
        "volumes": vol_values,
        "snapshots": snap_values,
        "days": days,
        "history_days": history_days(days, retention_days),
        "beyond_retention": days > retention_days,
        "first_ts": first_ts,
        "has_data": first_ts is not None,
        "latest": latest,
    }


# Quota ---------------------------------------------------------------------


def quota_percent(used, limit):
    """
    Whole percentage of ``limit`` used; None when unlimited.

    A limit of 0 is full (100) whatever the use.
    """
    if limit is None or (isinstance(limit, float) and math.isinf(limit)):
        return None
    if limit <= 0:
        return 100
    return int(round(100.0 * used / limit))


def quota_level(percent, thresholds):
    """``ok``, ``warning`` or ``critical`` for a quota percentage."""
    if percent is None:
        return LEVEL_OK
    if percent >= thresholds["quota_critical"]:
        return LEVEL_CRITICAL
    if percent >= thresholds["quota_warning"]:
        return LEVEL_WARNING
    return LEVEL_OK


def is_attached(volume):
    return volume.get("status") == STATUS_IN_USE or bool(
        volume.get("server_ids")
    )


def usage(block):
    """
    What the gigabytes, volumes and snapshots quotas count.

    Every volume and snapshot of the API lists holds quota, whatever its
    state (error volumes too).
    """
    volumes = block.get("volumes") or []
    snapshots = block.get("snapshots") or []
    attached = [v for v in volumes if is_attached(v)]
    unattached = [v for v in volumes if not is_attached(v)]
    attached_gb = sum(v.get("size") or 0 for v in attached)
    unattached_gb = sum(v.get("size") or 0 for v in unattached)
    snapshots_gb = sum(s.get("size") or 0 for s in snapshots)
    return {
        "attached_gb": attached_gb,
        "unattached_gb": unattached_gb,
        "snapshots_gb": snapshots_gb,
        "gigabytes": attached_gb + unattached_gb + snapshots_gb,
        "attached": len(attached),
        "unattached": len(unattached),
        "volumes": len(volumes),
        "snapshots": len(snapshots),
    }


def _row(key, label, segments, limit, thresholds, unit=""):
    used = sum(s["value"] for s in segments)
    percent = quota_percent(used, limit)
    scale = None
    if percent is not None:
        # an over-quota row (possible after a quota decrease) fills the bar
        scale = max(limit, used) or None
    for segment in segments:
        segment["width"] = (
            round(100.0 * segment["value"] / scale, 2) if scale else 0
        )
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "used": used,
        "limit": limit,
        "percent": percent,
        "level": quota_level(percent, thresholds),
        "segments": [s for s in segments if s["value"] > 0],
    }


def quota_rows(block, thresholds):
    """
    The four quota rows: gigabytes, volumes, snapshots, backups.

    Each row: ``key``, ``label``, ``unit``, ``used``, ``limit`` (None:
    unlimited), ``percent``, ``level``, ``segments`` (``key``, ``label``,
    ``value``, ``width`` in percent of the bar). The backups row has
    ``enabled`` (False: no backup service in this cloud).
    """
    use = usage(block)
    limits = block.get("limits") or {}
    rows = [
        _row(
            "gigabytes",
            _("Gigabytes"),
            [
                {
                    "key": "attached",
                    "label": _("Attached volumes"),
                    "value": use["attached_gb"],
                },
                {
                    "key": "unattached",
                    "label": _("Unattached volumes"),
                    "value": use["unattached_gb"],
                },
                {
                    "key": "snapshots",
                    "label": _("Snapshots"),
                    "value": use["snapshots_gb"],
                },
            ],
            limits.get("gigabytes"),
            thresholds,
            unit="GB",
        ),
        _row(
            "volumes",
            _("Volumes"),
            [
                {
                    "key": "attached",
                    "label": _("Attached"),
                    "value": use["attached"],
                },
                {
                    "key": "unattached",
                    "label": _("Unattached"),
                    "value": use["unattached"],
                },
            ],
            limits.get("volumes"),
            thresholds,
        ),
        _row(
            "snapshots",
            _("Snapshots"),
            [
                {
                    "key": "snapshots",
                    "label": _("Snapshots"),
                    "value": use["snapshots"],
                }
            ],
            limits.get("snapshots"),
            thresholds,
        ),
    ]
    enabled = bool(block.get("backup_enabled"))
    backups = _row(
        "backups",
        _("Backups"),
        [
            {
                "key": "backups",
                "label": _("Backups"),
                "value": (limits.get("backups_used") or 0) if enabled else 0,
            }
        ],
        limits.get("backups"),
        thresholds,
    )
    backups["enabled"] = enabled
    rows.append(backups)
    return rows


# Forecast ------------------------------------------------------------------


def slope_per_day(points):
    """Least-squares slope of ``[[ts, value], ...]`` per day, or None."""
    points = [(t, v) for t, v in points if v is not None]
    if len(points) < 2:
        return None
    n = len(points)
    mean_t = sum(t for t, _v in points) / n
    mean_v = sum(v for _t, v in points) / n
    var = sum((t - mean_t) ** 2 for t, _v in points)
    if var == 0:
        return None
    cov = sum((t - mean_t) * (v - mean_v) for t, v in points)
    return cov / var * DAY_SECONDS


def forecast(points, used, limit, thresholds):
    """
    When a quota is full at the trend of ``points``.

    ``used`` and ``limit`` come from the API (what the quota counts now);
    the metrics only give the growth rate, so quota held by resources
    Ceilometer does not report still counts.

    Returns:
        dict: ``state`` (FORECAST_*), ``days`` (whole days, for
        FORECAST_DAYS), ``level`` (LEVEL_*), ``history_days`` (whole days
        the points span)
    """
    points = sorted((t, v) for t, v in points or [] if v is not None)
    span = (points[-1][0] - points[0][0]) / DAY_SECONDS if points else 0
    result = {"days": None, "level": LEVEL_OK, "history_days": int(span)}
    if limit is None:
        return dict(result, state=FORECAST_UNLIMITED)
    if used >= limit:
        return dict(result, state=FORECAST_FULL, level=LEVEL_CRITICAL)
    if span < MIN_FORECAST_HISTORY_DAYS:
        return dict(result, state=FORECAST_NO_HISTORY)
    slope = slope_per_day(points)
    if slope is None or slope <= 0:
        return dict(result, state=FORECAST_FLAT)
    days = (limit - used) / slope
    if days > FORECAST_HORIZON_DAYS:
        return dict(result, state=FORECAST_FAR)
    days = max(1, int(round(days)))
    if days <= thresholds["forecast_critical_days"]:
        level = LEVEL_CRITICAL
    elif days <= thresholds["forecast_warning_days"]:
        level = LEVEL_WARNING
    else:
        level = LEVEL_OK
    return dict(result, state=FORECAST_DAYS, days=days, level=level)


def quota_forecasts(rows, trend, thresholds):
    """
    Forecast of the gigabytes, volumes and snapshots rows (by key).

    Args:
        trend: ``{"gigabytes": [[ts, v]], "volumes": [...],
            "snapshots": [...]}`` (see the datasource)
    """
    return {
        row["key"]: forecast(
            trend.get(row["key"]) or [], row["used"], row["limit"], thresholds
        )
        for row in rows
        if row["key"] in ("gigabytes", "volumes", "snapshots")
    }


# Ages and names ------------------------------------------------------------


def days_since(epoch, now):
    """Whole days since ``epoch`` (None when unknown)."""
    if epoch is None:
        return None
    return max(0, int((now - epoch) // DAY_SECONDS))


def display_name(resource):
    """The resource's name, or the start of its ID."""
    return resource.get("name") or (resource.get("id") or "")[:8]


def image_users(servers, volumes):
    """
    Image ID -> number of the project's instances using it.

    An instance uses its boot image or, booted from a volume, the image
    its attached volumes were created from.
    """
    volume_images = {}
    for volume in volumes or []:
        if volume.get("image_id"):
            for server_id in volume.get("server_ids") or []:
                volume_images.setdefault(server_id, set()).add(
                    volume["image_id"]
                )
    users = {}
    for server in servers or []:
        images = set(volume_images.get(server.get("id"), ()))
        if server.get("image_id"):
            images.add(server["image_id"])
        for image_id in images:
            users[image_id] = users.get(image_id, 0) + 1
    return users


def is_private(image):
    """Whether only this project can use ``image`` (see VISIBILITY_PRIVATE)."""
    return image.get("visibility") == VISIBILITY_PRIVATE


def image_gb(image):
    return round((image.get("size") or 0) / GIB, 1)


# Clean-up candidates -------------------------------------------------------


def _candidate(kind, resource, gb, reason, severity, in_quota, title=""):
    return {
        "kind": kind,
        "id": resource.get("id"),
        "name": display_name(resource),
        "gb": gb,
        "reason": reason,
        "severity": severity,
        "in_quota": in_quota,
        "title": title,
    }


def _days(n):
    return ngettext("%(n)d day", "%(n)d days", n) % {"n": n}


def clean_up_candidates(volumes, snapshots, images, users, thresholds, now):
    """
    Resources that could be deleted, most likely and largest first.

    * a volume ``available`` (unattached) for ``unattached_days`` or more:
      *review* from ``unattached_review_days``, *consider* before. Cinder
      does not record detach times: the last update time is used;
    * a snapshot ``snapshot_age_days`` or older with a newer snapshot of
      the same volume: *consider*; the newest (or only) snapshot of its
      volume: *keep?*, shown so the user decides knowingly;
    * a private image ``image_age_days`` or older that no instance of the
      project uses: *consider*; it does not count against the volume
      quota. Shared (and community or public) images are never
      candidates: instances of other projects may use them, and this
      project cannot see those.

    Args:
        images: the project's images, or None when not known (not checked)
        users: image ID -> instances using it, or None when not known
            (images not checked)

    Returns:
        list: every candidate (``kind``, ``id``, ``name``, ``gb``,
        ``reason``, ``severity``, ``in_quota``, ``title``), sorted
    """
    candidates = []
    names = {v.get("id"): display_name(v) for v in volumes}
    for volume in volumes:
        if volume.get("status") != STATUS_AVAILABLE or is_attached(volume):
            continue
        idle = days_since(volume.get("updated") or volume.get("created"), now)
        if idle is None or idle < thresholds["unattached_days"]:
            continue
        severity = (
            SEVERITY_REVIEW
            if idle >= thresholds["unattached_review_days"]
            else SEVERITY_CONSIDER
        )
        candidates.append(
            _candidate(
                "volume",
                volume,
                volume.get("size") or 0,
                _("unattached for %(days)s") % {"days": _days(idle)},
                severity,
                True,
            )
        )

    by_volume = {}
    for snapshot in snapshots:
        by_volume.setdefault(snapshot.get("volume_id"), []).append(snapshot)
    for volume_id, group in by_volume.items():
        newest = max(group, key=lambda s: s.get("created") or 0)
        volume_name = names.get(volume_id) or (volume_id or "")[:8]
        for snapshot in group:
            age = days_since(snapshot.get("created"), now)
            if age is None or age < thresholds["snapshot_age_days"]:
                continue
            values = {"days": _days(age), "volume": volume_name}
            if snapshot is not newest:
                reason = (
                    _("%(days)s old; a newer snapshot of %(volume)s exists")
                    % values
                )
                severity, title = SEVERITY_CONSIDER, ""
            elif len(group) == 1:
                reason = (
                    _("%(days)s old; the only snapshot of %(volume)s") % values
                )
                severity = SEVERITY_KEEP
                title = _("Only copy of this volume's data")
            else:
                reason = (
                    _("%(days)s old; the newest snapshot of %(volume)s")
                    % values
                )
                severity = SEVERITY_KEEP
                title = _("Newest copy of this volume's data")
            candidates.append(
                _candidate(
                    "snapshot",
                    snapshot,
                    snapshot.get("size") or 0,
                    reason,
                    severity,
                    True,
                    title,
                )
            )

    if images is not None and users is not None:
        for image in images:
            age = days_since(image.get("created"), now)
            if age is None or age < thresholds["image_age_days"]:
                continue
            if not is_private(image) or users.get(image.get("id")):
                continue
            candidates.append(
                _candidate(
                    "image",
                    image,
                    image_gb(image),
                    _("not used by any instance, %(days)s old")
                    % {"days": _days(age)},
                    SEVERITY_CONSIDER,
                    False,
                )
            )

    candidates.sort(key=lambda c: (-c["severity"], -c["gb"], c["name"]))
    return candidates


def reclaimable(candidates):
    """``(gb, count)`` of the candidates that would free quota."""
    freeing = [c for c in candidates if c["in_quota"] and c["severity"] > 0]
    return sum(c["gb"] for c in freeing), len(freeing)


# Inventories ---------------------------------------------------------------


def volume_rows(block, server_names, now, limit=VOLUMES_LIMIT):
    """
    Volumes, then snapshots, each largest first, at most ``limit`` rows.

    Args:
        server_names: server ID -> name, or None when not known

    Returns:
        tuple: (rows, total number of volumes and snapshots)
    """
    volumes = block.get("volumes") or []
    snapshots = block.get("snapshots") or []
    names = {v.get("id"): display_name(v) for v in volumes}
    rows = []
    for volume in sorted(
        volumes, key=lambda v: (-(v.get("size") or 0), display_name(v))
    ):
        attached = is_attached(volume)
        servers = [
            {
                "id": server_id,
                "name": (server_names or {}).get(server_id) or server_id[:8],
            }
            for server_id in volume.get("server_ids") or []
        ]
        idle = None
        if not attached:
            idle = days_since(
                volume.get("updated") or volume.get("created"), now
            )
        rows.append(
            {
                "kind": "volume",
                "id": volume.get("id"),
                "name": display_name(volume),
                "size": volume.get("size") or 0,
                "status": volume.get("status"),
                "attached": attached,
                "servers": servers,
                "idle_days": idle,
                "age_days": days_since(volume.get("created"), now),
            }
        )
    for snapshot in sorted(
        snapshots, key=lambda s: (-(s.get("size") or 0), display_name(s))
    ):
        volume_id = snapshot.get("volume_id") or ""
        rows.append(
            {
                "kind": "snapshot",
                "id": snapshot.get("id"),
                "name": display_name(snapshot),
                "size": snapshot.get("size") or 0,
                "status": snapshot.get("status"),
                "volume_name": names.get(volume_id) or volume_id[:8],
                "age_days": days_since(snapshot.get("created"), now),
            }
        )
    return rows[:limit], len(rows)


def image_rows(images, users, now, limit=IMAGES_LIMIT):
    """
    Private images, largest first, at most ``limit``.

    ``users`` counts this project's instances only: a row of an image
    other projects may use (not private, see is_private) has ``shared``
    set, and its use by other projects is unknown.

    Args:
        users: image ID -> instances using it, or None when not known

    Returns:
        tuple: (rows, total number, total GB of every image)
    """
    rows = [
        {
            "id": image.get("id"),
            "name": display_name(image),
            "gb": image_gb(image),
            "age_days": days_since(image.get("created"), now),
            "users": None if users is None else users.get(image.get("id"), 0),
            "shared": not is_private(image),
        }
        for image in images
    ]
    rows.sort(key=lambda r: (-r["gb"], r["name"]))
    total_gb = round(sum((i.get("size") or 0) for i in images) / GIB, 1)
    return rows[:limit], len(rows), total_gb
