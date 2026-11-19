#
# SPDX-License-Identifier: Apache-2.0

"""
Storage Health queries for the Prometheus datasource.

Kept apart from :mod:`grian_ui.datasource.prometheus`; its client calls
these functions. Every query returns one series per Cinder service or
pool shown, or a handful of series, whatever the size of the cloud.
"""

import re

from grian_ui import storage_health


SERVICE_HEALTH = "ceilometer_volume_service_health"
POOL_METRIC_PREFIX = "ceilometer_volume_provider_pool_capacity_"
IMAGE_SIZE = "ceilometer_image_size"
IMAGE_DOWNLOAD = "ceilometer_image_download"
BACKUP_SIZE = "ceilometer_volume_backup_size"
#: Images and backups are polled less often than pools.
INVENTORY_WINDOW = "1h"


def _number(value):
    from grian_ui.datasource.prometheus import number

    return number(value)


def _fresh():
    """
    Services and pools reporting within this window are current: the
    storage staleness window (``GRIAN_PLUGIN['storage_stale_minutes']``).
    """
    from grian_ui.datasource.prometheus import stale_window

    return stale_window()


def _tagged(query, label, value):
    return f'label_replace({query}, "{label}", "{value}", "", "")'


def split_service(resource):
    """``cinder-volume@host@backend`` -> ``("cinder-volume", "host@backend")``."""
    binary, _sep, host = (resource or "").partition("@")
    return binary, host


def get_services(client):
    """
    Current status of every Cinder service (one query).

    Returns:
        list: ``{"resource", "binary", "host", "up"}`` sorted by binary and
        host, where ``up`` is True or False for services reporting within
        the staleness window, and None for those that reported in the last 24 hours only.
    """
    query = " or ".join(
        (
            _tagged(
                f"max by (resource) (last_over_time({SERVICE_HEALTH}[{_fresh()}]))",
                "window",
                "now",
            ),
            _tagged(
                f"max by (resource) (last_over_time({SERVICE_HEALTH}[24h]))",
                "window",
                "day",
            ),
        )
    )
    current, seen = {}, set()
    for metric in client.client.query(query=query) or []:
        resource = metric.labels.get("resource")
        if not resource:
            continue
        seen.add(resource)
        if metric.labels.get("window") == "now":
            value = _number(metric.value)
            current[resource] = value is not None and value >= 1
    services = []
    for resource in seen:
        binary, host = split_service(resource)
        services.append(
            {
                "resource": resource,
                "binary": binary,
                "host": host,
                "up": current.get(resource),
            }
        )
    services.sort(key=lambda s: (s["binary"], s["host"]))
    return services


def get_service_history(client, at):
    """
    Each Cinder service's status per HISTORY_STEP over the last 24 hours.

    One subquery: the lowest value in each step (a step with one "down"
    report is down).

    Returns:
        dict: resource -> ``[[timestamp, value], ...]``
    """
    window, step = storage_health.HISTORY
    query = (
        f"min by (resource) (min_over_time({SERVICE_HEALTH}[{step}]))"
        f"[{window}:{step}]"
    )
    return {
        labels.get("resource"): points
        for labels, points in client.query_matrix(query, time=at)
        if labels.get("resource")
    }


def _pool_selector(names):
    """``resource=~"a|b"`` matching exactly these pool names."""
    escaped = "|".join(re.escape(n).replace("\\", "\\\\") for n in names)
    return f'resource=~"{escaped}"'


def _fresh_pools(field, selector=""):
    labels = f"{{{selector}}}" if selector else ""
    return (
        f"max by (resource) (last_over_time("
        f"{POOL_METRIC_PREFIX}{field}{labels}[{_fresh()}]))"
    )


def get_capacity_trend(client, range_key, names=None, at=None):
    """
    Capacity over a range (one subquery).

    Without ``names``: the whole cloud's allocated, provisioned and total
    capacity (GiB). With ``names``: the physical use (allocated ÷ total,
    percent) of each of these pools.

    Returns:
        dict: series name (field or pool name) -> ``[[ts, value], ...]``
    """
    _key, window, step = storage_health.get_range(range_key)
    if names:
        selector = _pool_selector(names)
        inner = (
            f"100 * {_fresh_pools('allocated', selector)} "
            f"/ {_fresh_pools('total', selector)}"
        )
        label = "resource"
    else:
        inner = " or ".join(
            _tagged(f"sum({_fresh_pools(field)})", "field", field)
            for field in ("allocated", "provisioned", "total")
        )
        label = "field"
    query = f"({inner})[{window}:{step}]"
    return {
        labels.get(label): points
        for labels, points in client.query_matrix(query, ndigits=2, time=at)
        if labels.get(label)
    }


def get_images_and_backups(client):
    """
    Image store and volume backups (one query).

    Returns:
        dict: ``images``, ``image_bytes``, ``image_projects``,
        ``images_downloaded`` (distinct images downloaded in 24 hours),
        ``backups`` and ``backup_gib``; None where nothing is reported.
    """
    images = f"max by (resource, project) (last_over_time({IMAGE_SIZE}[{INVENTORY_WINDOW}]))"
    backups = f"max by (resource) (last_over_time({BACKUP_SIZE}[{INVENTORY_WINDOW}]))"
    parts = {
        "images": f"count({images})",
        "image_bytes": f"sum({images})",
        "image_projects": f"count(count by (project) ({images}))",
        "images_downloaded": (
            f"count(count by (resource) (count_over_time({IMAGE_DOWNLOAD}[24h])))"
        ),
        "backups": f"count({backups})",
        "backup_gib": f"sum({backups})",
    }
    query = " or ".join(
        _tagged(part, "field", field) for field, part in parts.items()
    )
    result = dict.fromkeys(parts)
    for metric in client.client.query(query=query) or []:
        field = metric.labels.get("field")
        if field in result:
            result[field] = _number(metric.value)
    return result
