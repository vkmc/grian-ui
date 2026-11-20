#
# SPDX-License-Identifier: Apache-2.0

"""
Centralized configuration management for Grian UI.
Validates and provides typed access to Django settings.
"""

import math

from django.conf import settings


# Valid datasource types
DATASOURCE_FAKE = "fake"
DATASOURCE_PROMETHEUS = "prometheus"
VALID_DATASOURCE_TYPES = (DATASOURCE_FAKE, DATASOURCE_PROMETHEUS)


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""

    pass


def _get_grian_plugin_config():
    """Get and validate GRIAN_PLUGIN configuration."""
    config = getattr(settings, "GRIAN_PLUGIN", None)

    if config is None:
        # Return default configuration
        return {"datasource": DATASOURCE_FAKE}

    if not isinstance(config, dict):
        raise ConfigurationError(
            f"GRIAN_PLUGIN must be a dict, got {type(config).__name__}"
        )

    return config


def get_datasource_type():
    """
    Get the configured datasource type.

    Returns:
        str: The datasource type (e.g., 'fake', 'prometheus')

    Raises:
        ConfigurationError: If datasource type is invalid
    """
    config = _get_grian_plugin_config()
    datasource = config.get("datasource", DATASOURCE_FAKE)

    if not isinstance(datasource, str):
        raise ConfigurationError(
            f"GRIAN_PLUGIN['datasource'] must be a string, got {type(datasource).__name__}"
        )

    if datasource not in VALID_DATASOURCE_TYPES:
        raise ConfigurationError(
            f"Invalid datasource type '{datasource}'. "
            f"Valid types: {', '.join(VALID_DATASOURCE_TYPES)}"
        )

    return datasource


def get_thresholds(setting, defaults, maximum=None):
    """
    Get a dashboard's status thresholds, with per-key overrides.

    Defaults can be overridden per key with ``GRIAN_PLUGIN[setting]``, for
    example::

        GRIAN_PLUGIN = {
            "datasource": "prometheus",
            "example_thresholds": {"cpu_warning": 80},
        }

    Every value must be a finite number of at least 0 (and less than
    ``maximum`` when given). For each ``<name>_warning`` /
    ``<name>_critical`` pair in ``defaults``, the warning value must not be
    greater than the critical one, so higher values must be worse; express
    "lower is worse" measures (such as days until full) as a value that
    grows with the problem, or extend this function.

    Args:
        setting: key in GRIAN_PLUGIN holding the overrides
        defaults: dict of default thresholds; its keys are the valid keys
        maximum: exclusive upper bound for every value, or None

    Returns:
        dict: thresholds keyed like ``defaults``

    Raises:
        ConfigurationError: If an override is unknown or out of range
    """
    overrides = _get_grian_plugin_config().get(setting)
    thresholds = dict(defaults)
    if overrides is None:
        return thresholds

    if not isinstance(overrides, dict):
        raise ConfigurationError(
            f"GRIAN_PLUGIN['{setting}'] must be a dict, "
            f"got {type(overrides).__name__}"
        )

    for key, value in overrides.items():
        if key not in defaults:
            raise ConfigurationError(
                f"Unknown key '{key}' in GRIAN_PLUGIN['{setting}']. "
                f"Valid keys: {', '.join(defaults)}"
            )
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
            or (maximum is not None and value >= maximum)
        ):
            bound = (
                f"from 0 to less than {maximum}"
                if maximum is not None
                else "of at least 0"
            )
            raise ConfigurationError(
                f"GRIAN_PLUGIN['{setting}']['{key}'] must be a number "
                f"{bound}, got {value!r}"
            )
        thresholds[key] = value

    for key in defaults:
        if not key.endswith("_warning"):
            continue
        name = key[: -len("_warning")]
        critical = f"{name}_critical"
        if critical in defaults and thresholds[key] > thresholds[critical]:
            raise ConfigurationError(
                f"GRIAN_PLUGIN['{setting}']['{key}'] must not be greater "
                f"than '{critical}'"
            )

    return thresholds


#: Largest value (pickled size, bytes) the Telemetry dashboards store in the
#: shared Horizon cache. memcached refuses items over 1 MiB by default (its
#: -I option), and some clients hide the refusal; bigger values are cached
#: per Horizon process instead.
DEFAULT_CACHE_MAX_ITEM_BYTES = 1_000_000


def get_cache_max_item_bytes():
    """
    Get the largest value stored in the shared cache, in bytes.

    Override with ``GRIAN_PLUGIN['cache_max_item_bytes']`` when memcached
    runs with a different item size limit (``-I``).

    Raises:
        ConfigurationError: If the value is not a positive integer
    """
    value = _get_grian_plugin_config().get(
        "cache_max_item_bytes", DEFAULT_CACHE_MAX_ITEM_BYTES
    )
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(
            "GRIAN_PLUGIN['cache_max_item_bytes'] must be a positive "
            f"integer, got {value!r}"
        )
    return value


#: Prometheus keeps 15 days of samples unless --storage.tsdb.retention.time
#: says otherwise.
DEFAULT_METRICS_RETENTION_DAYS = 15


def get_metrics_retention_days():
    """
    Get how many days of metric history the backend keeps.

    Project users cannot read the retention from Aetos (its status API is
    admin-only), so pages that look back days or months read it from
    ``GRIAN_PLUGIN['metrics_retention_days']`` to say how much history
    exists. Set it to the Prometheus retention
    (``--storage.tsdb.retention.time``).

    Raises:
        ConfigurationError: If the value is not a positive whole number
    """
    value = _get_grian_plugin_config().get(
        "metrics_retention_days", DEFAULT_METRICS_RETENTION_DAYS
    )
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(
            "GRIAN_PLUGIN['metrics_retention_days'] must be a positive "
            f"whole number of days, got {value!r}"
        )
    return value


def get_openstack_keystone_url():
    """Get OpenStack Keystone URL from settings."""
    url = getattr(settings, "OPENSTACK_KEYSTONE_URL", None)
    if not url:
        raise ConfigurationError(
            "OPENSTACK_KEYSTONE_URL is not configured in settings"
        )
    return url


def get_openstack_endpoint_type():
    """Get OpenStack endpoint type (interface) from settings."""
    return getattr(settings, "OPENSTACK_ENDPOINT_TYPE", "publicURL")


#: Default Project Network thresholds (the mockup's values).
DEFAULT_PROJECT_NETWORK_THRESHOLDS = {
    # an interface is quiet when its inbound packet rate fell below this
    # share of its usual rate, percent (less than 100)
    "quiet_percent": 10,
    # interfaces whose usual inbound rate is lower are too bursty to call
    # quiet: only drops, errors and port state are checked, packets/s
    "quiet_min_pps": 0.5,
    # dropped + failed packets (both directions) above this rate are a
    # warning, and at or above the critical rate critical, packets/s
    "packet_loss_warning": 0,
    "packet_loss_critical": 10,
    # quota bars turn amber and red at these percentages of the limit
    "quota_warning": 75,
    "quota_critical": 90,
}


def get_project_network_thresholds():
    """
    Get the Project Network thresholds.

    Defaults (DEFAULT_PROJECT_NETWORK_THRESHOLDS) can be overridden per key
    with ``GRIAN_PLUGIN['project_network_thresholds']``. See
    get_thresholds().

    Raises:
        ConfigurationError: If an override is unknown or out of range
    """
    thresholds = get_thresholds(
        "project_network_thresholds", DEFAULT_PROJECT_NETWORK_THRESHOLDS
    )
    if thresholds["quiet_percent"] >= 100:
        raise ConfigurationError(
            "GRIAN_PLUGIN['project_network_thresholds']['quiet_percent'] "
            f"must be less than 100, got {thresholds['quiet_percent']!r}"
        )
    return thresholds
