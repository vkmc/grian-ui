#
# SPDX-License-Identifier: Apache-2.0

"""
Storage health evaluation.

Pure functions shared by the Storage Health views (the Volume Metrics
panel). They turn two inputs into a verdict, summary cards and bounded
tables, so the amount of data rendered does not grow with the size of the
cloud:

* the **pool metrics** (``DatasourceClient.get_storage_pools`` and
  ``get_capacity_predictions``): capacity, last report and forecast of
  every Cinder pool;
* the **Block Storage API inventory** (see
  :mod:`grian_ui.storage_inventory`), read with the administrator's
  token::

    volumes: [
        {
            "id",
            "name",
            "project_id",
            "status",
            "size",
            "host",
            "attached",
            "created",
            "updated",
        }
    ]
    snapshots: [
        {
            "id",
            "name",
            "project_id",
            "status",
            "size",
            "volume_id",
            "created",
            "updated",
        }
    ]

  Times are epoch seconds (or None), sizes GiB, ``host`` is the volume's
  ``host@backend#pool``.
"""

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy


#: Maximum attention rows kept in the cached summary (worst first). Counts
#: stay exact.
ATTENTION_STORE_LIMIT = 500
#: Maximum rows rendered in the "needing attention" list.
LIST_LIMIT = 25
#: Maximum names listed in the verdict, and volumes listed as getting
#: close to the clean-up rule.
NAME_LIMIT = 5
SOON_LIMIT = 3

#: Volume and snapshot states that need an operator.
VOLUME_ERROR_STATES = (
    "error",
    "error_deleting",
    "error_restoring",
    "error_extending",
)
SNAPSHOT_ERROR_STATES = ("error", "error_deleting")
#: Transitional states: a problem when they last longer than the
#: ``stuck_minutes`` threshold.
VOLUME_TRANSITIONAL_STATES = ("creating", "attaching", "detaching", "deleting")
SNAPSHOT_TRANSITIONAL_STATES = ("creating", "deleting")

LEVEL_OK = "ok"
LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"
_LEVEL_ORDER = {
    LEVEL_CRITICAL: 3,
    LEVEL_WARNING: 2,
    LEVEL_INFO: 1,
    LEVEL_OK: 0,
}

#: Pool status: severity, plus "stale" for pools that stopped reporting.
POOL_STALE = "stale"
POOL_LABELS = {
    POOL_STALE: gettext_lazy("Stale"),
    LEVEL_CRITICAL: gettext_lazy("Critical"),
    LEVEL_WARNING: gettext_lazy("Warning"),
    LEVEL_OK: gettext_lazy("Healthy"),
}

#: Why a volume or snapshot is listed.
RULE_ERROR = "error"
RULE_STUCK = "stuck"
RULE_UNATTACHED = "unattached"
RULE_OLD_SNAPSHOT = "old_snapshot"
RULE_LEVELS = {
    RULE_ERROR: LEVEL_CRITICAL,
    RULE_STUCK: LEVEL_WARNING,
    RULE_UNATTACHED: LEVEL_INFO,
    RULE_OLD_SNAPSHOT: LEVEL_INFO,
}

VERDICT_POOLS_FULL = "pools_full"
VERDICT_POOLS_STALE = "pools_stale"
VERDICT_VOLUMES_ERROR = "volumes_error"
VERDICT_POOLS_FILLING = "pools_filling"
VERDICT_VOLUMES_STUCK = "volumes_stuck"
VERDICT_HEALTHY = "healthy"


def backend_of(host):
    """``host@backend#pool`` -> ``host@backend`` (or None)."""
    if not host:
        return None
    return host.split("#", 1)[0]


def percent(part, whole):
    """``part`` as a percentage of ``whole``, or None."""
    if part is None or not whole:
        return None
    return 100.0 * part / whole


def _level(value, warning, critical):
    if value is None:
        return LEVEL_OK
    if value >= critical:
        return LEVEL_CRITICAL
    if value >= warning:
        return LEVEL_WARNING
    return LEVEL_OK


def _forecast_level(days, thresholds):
    if days is None:
        return LEVEL_OK
    if days <= thresholds["forecast_critical_days"]:
        return LEVEL_CRITICAL
    if days <= thresholds["forecast_warning_days"]:
        return LEVEL_WARNING
    return LEVEL_OK


def worst(*levels):
    """The most severe of ``levels``."""
    return max(levels, key=lambda level: _LEVEL_ORDER[level])


def evaluate_pool(pool, prediction, thresholds):
    """
    One pool's use, forecast and status.

    Physical use is allocated ÷ total; virtual use is provisioned ÷ the
    over-subscription limit, where limit = provisioned + virtual free.
    A pool is critical (or warning) when either use reaches the critical
    (warning) threshold or its forecast is that close; a stale pool keeps
    its last values and is at least a warning.

    Args:
        pool: a ``get_storage_pools`` entry
        prediction: its ``get_capacity_predictions`` entry, or None
        thresholds: config.get_storage_thresholds()
    """
    virtual_free = pool.get("virtual_free")
    limit = (
        pool["provisioned"] + virtual_free
        if virtual_free is not None
        else None
    )
    physical = percent(pool["allocated"], pool["total"])
    virtual = percent(pool["provisioned"], limit)
    # the values of a stale pool are frozen: no forecast
    prediction = prediction if not pool["stale"] else None
    days = prediction.get("days_until_full") if prediction else None
    physical_level = _level(
        physical,
        thresholds["physical_warning"],
        thresholds["physical_critical"],
    )
    virtual_level = _level(
        virtual, thresholds["virtual_warning"], thresholds["virtual_critical"]
    )
    forecast_level = _forecast_level(days, thresholds)
    level = worst(physical_level, virtual_level, forecast_level)
    if pool["stale"]:
        level = worst(level, LEVEL_WARNING)
    return dict(
        pool,
        limit=limit,
        physical_percent=physical,
        virtual_percent=virtual,
        physical_level=physical_level,
        virtual_level=virtual_level,
        forecast=prediction,
        forecast_level=forecast_level,
        level=level,
        status=POOL_STALE if pool["stale"] else level,
    )


def evaluate_pools(pools, predictions, thresholds):
    """evaluate_pool() for every pool; ``predictions`` may be None."""
    by_name = {p["name"]: p for p in predictions or []}
    return [
        evaluate_pool(pool, by_name.get(pool["name"]), thresholds)
        for pool in pools
    ]


def _attention_row(item, kind, rule, since, now, backend):
    return {
        "id": item["id"],
        "name": item.get("name") or item["id"],
        "kind": kind,
        "project_id": item.get("project_id"),
        "status": item.get("status"),
        "size": item.get("size"),
        "backend": backend,
        "rule": rule,
        "level": RULE_LEVELS[rule],
        "for_seconds": max(0.0, now - since) if since else None,
    }


def _sort_key(row):
    return (-_LEVEL_ORDER[row["level"]], -(row["for_seconds"] or 0), row["id"])


def summarize_inventory(
    volumes, snapshots, thresholds, now, limit=ATTENTION_STORE_LIMIT
):
    """
    Volumes and snapshots needing attention, worst first.

    * critical: error states;
    * warning: a transitional state for more than ``stuck_minutes``
      (judged on the time of the last update);
    * clean-up (info): volumes ``available`` and unattached since at
      least ``unattached_days`` (the time of their last update, which
      detaching sets), and snapshots at least ``old_snapshot_days`` old
      with a newer snapshot of the same volume.

    Returns:
        dict: ``rows`` (at most ``limit``), exact ``counts`` per level
        (critical, warning, info), ``volumes_total``, ``snapshots_total``,
        ``volumes_attention`` (volumes and snapshots needing an operator:
        critical or warning) and ``unattached_soon``: up to SOON_LIMIT
        unattached volumes not yet old enough for clean-up, longest first
        (``{"name", "days"}``), and ``projects``: project_usage().
    """
    stuck = thresholds["stuck_minutes"] * 60
    unattached = thresholds["unattached_days"] * 86400
    old_snapshot = thresholds["old_snapshot_days"] * 86400
    hosts = {v["id"]: v.get("host") for v in volumes}
    rows = []
    soon = []

    for volume in volumes:
        status = volume.get("status")
        updated = volume.get("updated") or volume.get("created")
        age = now - updated if updated else None
        backend = backend_of(volume.get("host"))
        if status in VOLUME_ERROR_STATES:
            rule = RULE_ERROR
        elif status in VOLUME_TRANSITIONAL_STATES:
            rule = RULE_STUCK if age is not None and age > stuck else None
        elif status == "available" and not volume.get("attached"):
            if age is not None and age >= unattached:
                rule = RULE_UNATTACHED
            else:
                rule = None
                if age is not None:
                    soon.append(
                        {
                            "name": volume.get("name") or volume["id"],
                            "days": round(age / 86400.0, 1),
                        }
                    )
        else:
            rule = None
        if rule:
            rows.append(
                _attention_row(volume, "volume", rule, updated, now, backend)
            )

    newest = {}
    for snapshot in snapshots:
        created = snapshot.get("created")
        volume_id = snapshot.get("volume_id")
        if created and volume_id and snapshot.get("status") == "available":
            newest[volume_id] = max(newest.get(volume_id, created), created)

    for snapshot in snapshots:
        status = snapshot.get("status")
        created = snapshot.get("created")
        updated = snapshot.get("updated") or created
        backend = backend_of(hosts.get(snapshot.get("volume_id")))
        since = updated
        if status in SNAPSHOT_ERROR_STATES:
            rule = RULE_ERROR
        elif status in SNAPSHOT_TRANSITIONAL_STATES:
            stuck_long = updated is not None and now - updated > stuck
            rule = RULE_STUCK if stuck_long else None
        elif (
            status == "available"
            and created
            and now - created >= old_snapshot
            and newest.get(snapshot.get("volume_id"), created) > created
        ):
            rule = RULE_OLD_SNAPSHOT
            since = created
        else:
            rule = None
        if rule:
            rows.append(
                _attention_row(snapshot, "snapshot", rule, since, now, backend)
            )

    rows.sort(key=_sort_key)
    counts = {LEVEL_CRITICAL: 0, LEVEL_WARNING: 0, LEVEL_INFO: 0}
    for row in rows:
        counts[row["level"]] += 1
    soon.sort(key=lambda s: -s["days"])
    return {
        "rows": rows[:limit],
        "counts": counts,
        "total": len(rows),
        "volumes_total": len(volumes),
        "snapshots_total": len(snapshots),
        "volumes_attention": counts[LEVEL_CRITICAL] + counts[LEVEL_WARNING],
        "unattached_soon": soon[:SOON_LIMIT],
        "projects": project_usage(volumes, snapshots),
    }


def summarize(pools, inventory, thresholds, services=()):
    """
    Verdict and summary cards.

    Args:
        pools: evaluate_pools() rows
        inventory: summarize_inventory() result, or None when the Block
            Storage API data is not available
        thresholds: config.get_storage_thresholds()
        services: ``get_volume_services()`` entries

    Returns:
        dict: totals, card levels and ``verdict`` (``key``, ``level``,
        ``names``: up to NAME_LIMIT pool names)
    """
    total = sum(p["total"] for p in pools)
    allocated = sum(p["allocated"] for p in pools)
    provisioned = sum(p["provisioned"] for p in pools)
    limited = [p for p in pools if p["limit"] is not None]
    limit = sum(p["limit"] for p in limited)
    physical = percent(allocated, total)
    virtual = percent(sum(p["provisioned"] for p in limited), limit)
    stale = [p for p in pools if p["stale"]]
    fresh = [p for p in pools if not p["stale"]]
    full = [p for p in fresh if p["level"] == LEVEL_CRITICAL]
    filling = [p for p in fresh if p["level"] == LEVEL_WARNING]
    counts = inventory["counts"] if inventory else {}

    down = services_down(services)
    if down:
        verdict = (VERDICT_SERVICE_DOWN, LEVEL_CRITICAL, [])
    elif full:
        verdict = (VERDICT_POOLS_FULL, LEVEL_CRITICAL, full)
    elif stale:
        verdict = (VERDICT_POOLS_STALE, LEVEL_WARNING, stale)
    elif counts.get(LEVEL_CRITICAL):
        verdict = (VERDICT_VOLUMES_ERROR, LEVEL_WARNING, [])
    elif filling:
        verdict = (VERDICT_POOLS_FILLING, LEVEL_WARNING, filling)
    elif counts.get(LEVEL_WARNING):
        verdict = (VERDICT_VOLUMES_STUCK, LEVEL_WARNING, [])
    else:
        verdict = (VERDICT_HEALTHY, LEVEL_OK, [])
    key, level, named = verdict
    names = [p["name"] for p in named[:NAME_LIMIT]]
    if key == VERDICT_SERVICE_DOWN:
        named = down
        names = [_("%(binary)s on %(host)s") % s for s in down[:NAME_LIMIT]]
    # forecasts need FORECAST_HISTORY: say how much the pools have
    short = [
        p["forecast"]["history_days"]
        for p in fresh
        if p["forecast"] and not p["forecast"].get("enough_history")
    ]

    return {
        "total": total,
        "allocated": allocated,
        "provisioned": provisioned,
        "physical_percent": physical,
        "virtual_percent": virtual,
        "pools_total": len(pools),
        "pools_stale": len(stale),
        "allocated_level": worst(
            _level(
                physical,
                thresholds["physical_warning"],
                thresholds["physical_critical"],
            ),
            _level(
                virtual,
                thresholds["virtual_warning"],
                thresholds["virtual_critical"],
            ),
        ),
        "attention": inventory,
        "forecast_history_days": max(short) if short else None,
        "verdict": {
            "key": key,
            "level": level,
            "names": names,
            "count": len(named),
        },
        "services_total": len(services),
        "services_down": len(down),
        "soonest": _soonest(fresh),
    }


def _soonest(pools):
    """The pool forecast to be full first (reporting now), or None."""
    forecast = [
        p
        for p in pools
        if p["forecast"] and p["forecast"].get("days_until_full") is not None
    ]
    if not forecast:
        return None
    pool = min(forecast, key=lambda p: p["forecast"]["days_until_full"])
    return {
        "name": pool["name"],
        "days": pool["forecast"]["days_until_full"],
        "limiting": pool["forecast"]["limiting"],
    }


def format_duration(seconds):
    """Short duration: '47 min', '2 h', '3 d' (or '' when unknown)."""
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return _("%(n)s min") % {"n": max(1, seconds // 60)}
    if seconds < 2 * 86400:
        return _("%(n)s h") % {"n": seconds // 3600}
    return _("%(n)s d") % {"n": seconds // 86400}


# Capacity over time, services, backends, pools table, projects

#: Ranges of the capacity chart: key -> (range, step), 84 to 168 points.
#: 14 days needs a Prometheus retention of at least 14 days.
RANGES = {
    "24h": ("24h", "15m"),
    "7d": ("7d", "1h"),
    "14d": ("14d", "2h"),
}
DEFAULT_RANGE = "7d"
#: Capacity chart views: the whole cloud, or the fullest pools.
VIEW_CLOUD = "cloud"
VIEW_POOLS = "pools"
VIEWS = (VIEW_CLOUD, VIEW_POOLS)
#: Pools drawn in the "fullest pools" view.
CHART_POOLS = 5
#: Cinder service history: (range, step) of the availability strip.
HISTORY = ("24h", "15m")
#: Rows of the pools, backends and projects tables.
POOL_LIMIT = 10
BACKEND_LIMIT = 10
PROJECT_LIMIT = 10
#: Pool name suggestions returned for the look-up box.
SUGGESTION_LIMIT = 10
#: Sort orders of the pools table.
SORT_SOON = "soon"
SORT_FULL = "full"
SORT_STALE = "stale"
SORTS = (SORT_SOON, SORT_FULL, SORT_STALE)

VERDICT_SERVICE_DOWN = "service_down"


def get_range(key):
    """Return ``(key, range, step)``, falling back to the default range."""
    if key not in RANGES:
        key = DEFAULT_RANGE
    window, step = RANGES[key]
    return key, window, step


def _seconds(duration):
    units = {"m": 60, "h": 3600, "d": 86400}
    return int(duration[:-1]) * units[duration[-1]]


def grid(window, step, now):
    """
    Timestamps of a subquery ``expr[window:step]`` evaluated at ``now``.

    Steps are aligned to multiples of the step, ending at the last step
    boundary at or before ``now``.
    """
    step_s = _seconds(step)
    count = _seconds(window) // step_s
    end = int(now // step_s * step_s)
    return [end - (count - 1 - i) * step_s for i in range(count)]


def fill(points, timestamps):
    """Values of ``[[ts, value], ...]`` on ``timestamps``; gaps are None."""
    if len(timestamps) < 2:
        return [None] * len(timestamps)
    step = timestamps[1] - timestamps[0]
    by_slot = {int(round(ts / step)) * step: value for ts, value in points}
    return [by_slot.get(ts) for ts in timestamps]


def service_rows(services, history, now):
    """
    Cinder services with their 24-hour availability strip.

    Args:
        services: ``get_volume_services()`` entries
        history: ``get_volume_service_history()`` (resource -> points), or
            None when it is not available
        now: evaluation time of the history

    Returns:
        list: services, down first, each with ``strip`` (1 up, 0 down,
        None no report, one per HISTORY step), ``up_percent`` (of the
        steps with a report) and ``down_since`` (epoch of the first step
        of the current outage, or None)
    """
    timestamps = grid(*HISTORY, now)
    rows = []
    for service in services:
        strip = None
        up_percent = None
        down_since = None
        if history is not None:
            strip = [
                None if v is None else (1 if v >= 1 else 0)
                for v in fill(history.get(service["resource"], []), timestamps)
            ]
            reported = [v for v in strip if v is not None]
            if reported:
                up_percent = 100.0 * sum(reported) / len(reported)
            if service["up"] is False:
                for ts, value in zip(reversed(timestamps), reversed(strip)):
                    if value == 1:
                        break
                    if value == 0:
                        down_since = ts
        rows.append(
            dict(
                service,
                strip=strip,
                up_percent=up_percent,
                down_since=down_since,
            )
        )
    order = {False: 0, None: 1, True: 2}
    rows.sort(key=lambda r: (order[r["up"]], r["binary"], r["host"]))
    return rows


def services_down(services):
    """Services that report "down", or stopped reporting."""
    return [s for s in services if s["up"] is not True]


def backend_rows(pools, services, limit=BACKEND_LIMIT):
    """
    Pools summed per backend (``host@backend``), most provisioned first.

    Over-subscription is provisioned ÷ total. A backend whose
    ``cinder-volume`` service is down (or silent) is flagged.

    Returns:
        tuple: (at most ``limit`` rows, number of backends)
    """
    down = {
        s["host"]
        for s in services_down(services)
        if s["binary"] == "cinder-volume"
    }
    backends = {}
    for pool in pools:
        name = backend_of(pool["name"])
        row = backends.setdefault(
            name,
            {
                "name": name,
                "pools": 0,
                "stale": 0,
                "total": 0.0,
                "allocated": 0.0,
                "provisioned": 0.0,
            },
        )
        row["pools"] += 1
        row["stale"] += 1 if pool["stale"] else 0
        row["total"] += pool["total"]
        row["allocated"] += pool["allocated"]
        row["provisioned"] += pool["provisioned"]
    rows = []
    for row in backends.values():
        ratio = row["provisioned"] / row["total"] if row["total"] else None
        rows.append(
            dict(
                row,
                oversubscription=ratio,
                service_down=row["name"] in down,
            )
        )
    rows.sort(key=lambda r: (not r["service_down"], -r["provisioned"]))
    return rows[:limit], len(rows)


def _fullness(row):
    return max(
        (
            v
            for v in (row["physical_percent"], row["virtual_percent"])
            if v is not None
        ),
        default=0.0,
    )


def sort_pools(rows, by):
    """Evaluated pools sorted for the pools table (see SORTS)."""
    if by == SORT_FULL:
        return sorted(rows, key=lambda r: (-_fullness(r), r["name"]))
    if by == SORT_STALE:
        return sorted(
            rows,
            key=lambda r: (
                not r["stale"],
                r["last_seen"] or 0 if r["stale"] else 0,
                -_fullness(r),
                r["name"],
            ),
        )

    def soon(row):
        forecast = row["forecast"] or {}
        days = forecast.get("days_until_full")
        return (days is None, days or 0, -_fullness(row), row["name"])

    return sorted(rows, key=soon)


def find_pool(rows, name):
    """The pool named ``name`` (case-insensitive), or None."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    for row in rows:
        if row["name"].lower() == wanted:
            return row
    return None


def suggest_pools(pools, text, limit=SUGGESTION_LIMIT):
    """Up to ``limit`` pool names containing ``text`` (case-insensitive)."""
    text = (text or "").strip().lower()
    names = sorted(p["name"] for p in pools)
    if text:
        names = [n for n in names if text in n.lower()]
    return names[:limit]


def fullest_pools(rows, limit=CHART_POOLS):
    """Names of the pools with the highest physical use (reporting now)."""
    fresh = [
        r for r in rows if not r["stale"] and r["physical_percent"] is not None
    ]
    fresh.sort(key=lambda r: (-r["physical_percent"], r["name"]))
    return [r["name"] for r in fresh[:limit]]


def project_usage(volumes, snapshots, limit=PROJECT_LIMIT):
    """
    Volumes and snapshots per project, largest first.

    Returns:
        dict: ``rows`` (at most ``limit``: project_id, volumes, volume_gib,
        snapshots, snapshot_gib, share percent of all volume and snapshot
        GiB), ``total`` (projects) and ``gib`` (all projects)
    """
    projects = {}

    def row(project_id):
        return projects.setdefault(
            project_id,
            {
                "project_id": project_id,
                "volumes": 0,
                "volume_gib": 0,
                "snapshots": 0,
                "snapshot_gib": 0,
            },
        )

    for volume in volumes:
        entry = row(volume.get("project_id"))
        entry["volumes"] += 1
        entry["volume_gib"] += volume.get("size") or 0
    for snapshot in snapshots:
        entry = row(snapshot.get("project_id"))
        entry["snapshots"] += 1
        entry["snapshot_gib"] += snapshot.get("size") or 0
    gib = sum(p["volume_gib"] + p["snapshot_gib"] for p in projects.values())
    rows = sorted(
        projects.values(),
        key=lambda p: (
            -(p["volume_gib"] + p["snapshot_gib"]),
            str(p["project_id"]),
        ),
    )[:limit]
    for entry in rows:
        entry["share"] = percent(
            entry["volume_gib"] + entry["snapshot_gib"], gib
        )
    return {"rows": rows, "total": len(projects), "gib": gib}
