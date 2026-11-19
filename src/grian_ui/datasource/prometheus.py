#
# SPDX-License-Identifier: Apache-2.0

"""
Prometheus datasource implementation via Aetos.
Provides access to Prometheus metrics through OpenStack observability service.
"""

import logging
import math
import time

from keystoneauth1 import session
from keystoneauth1.identity import v3
from observabilityclient.utils import metric_utils as obs_client_utils

from grian_ui import config
from grian_ui.datasource.api import DatasourceClient


LOG = logging.getLogger(__name__)

#: Timeout of the requests to Aetos, in seconds (per connection attempt and
#: per read, as for every keystoneauth session), so a hung backend fails the
#: query instead of holding a Horizon worker thread indefinitely. A
#: computation running several queries can still take longer than
#: grian_ui.cache.MAX_COMPUTE_SECONDS; another request then takes the lock
#: over.
QUERY_TIMEOUT = 30

#: Storage pool capacity metrics, one per field.
POOL_METRIC_PREFIX = "ceilometer_volume_provider_pool_capacity_"
POOL_FIELDS = ("total", "allocated", "free", "provisioned", "virtual_free")
#: Pools that reported within this window are listed; those silent for
#: longer than GRIAN_PLUGIN['storage_stale_minutes'] (see
#: config.get_storage_stale_minutes) are marked stale, with their last
#: values.
POOL_LOOKBACK = "24h"
#: Capacity forecasts: linear trend over FORECAST_HISTORY, which a pool
#: must cover (with one hour of tolerance) to get a forecast.
FORECAST_HISTORY = "7d"
FORECAST_HORIZON_DAYS = 30
FORECAST_MIN_HISTORY_SECONDS = 7 * 86400 - 3600


def number(value, ndigits=None):
    """
    Convert a Prometheus sample value to a finite float or None.

    Values used for status decisions should be kept unrounded; pass
    ``ndigits`` only for display data such as chart points.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result if ndigits is None else round(result, ndigits)


def stale_window():
    """The storage staleness window as a PromQL duration, e.g. ``"10m"``."""
    return f"{config.get_storage_stale_minutes()}m"


def pool_last_seen_query():
    """
    Time of each pool's last report (epoch), within POOL_LOOKBACK.

    ``timestamp()`` inside a subquery gives the time of the sample each
    step sees; the latest one is the last report. The fine-grained part
    covers recent reports (up to 30 s late) and the staleness window, the
    coarse one, evaluated when the first returns nothing, pools silent for
    longer.
    """
    metric = f"{POOL_METRIC_PREFIX}total"
    recent = max(15, config.get_storage_stale_minutes() + 5)
    return (
        f"max by (resource) (max_over_time(timestamp({metric})"
        f"[{recent}m:30s]))"
        f" or max by (resource) (max_over_time(timestamp({metric})"
        f"[{POOL_LOOKBACK}:5m]))"
    )


def pool_history_query():
    """Seconds since each pool's first report, within FORECAST_HISTORY."""
    metric = f"{POOL_METRIC_PREFIX}total"
    return (
        f"max by (resource) (time() - min_over_time(timestamp({metric})"
        f"[{FORECAST_HISTORY}:1h]))"
    )


def days_until_zero(current, predicted):
    """
    Days until a capacity reaches zero at its trend, or None.

    ``predicted`` is the value FORECAST_HORIZON_DAYS ahead. None when the
    capacity is not shrinking or a value is missing; 0 when it is already
    used up.
    """
    if current is None or predicted is None:
        return None
    decline_per_day = (current - predicted) / FORECAST_HORIZON_DAYS
    if decline_per_day <= 0:
        return None
    if current <= 0:
        return 0.0
    return round(current / decline_per_day, 1)


class PrometheusTelemetryClient(DatasourceClient):
    """
    Prometheus telemetry client to retrieves data from the OSP deployment
    """

    def __init__(self, request):
        """
        Initialize Prometheus Telemetry client via Aetos.

        Args:
            request: Django HTTP request containing user authentication

        Note:
            Uses get_prom_client_from_keystone for proper Aetos/Prometheus
            client initialization with Keystone session.
            A new client instance should be created per page request,
            but can be reused for multiple metric queries within that request.
        """
        auth_url = config.get_openstack_keystone_url()
        token = request.user.token.id
        project_id = request.user.project_id
        domain_id = request.user.domain_id
        interface = config.get_openstack_endpoint_type()

        auth = v3.Token(
            auth_url=auth_url,
            token=token,
            project_id=project_id,
            domain_id=domain_id,
        )

        sess = session.Session(auth=auth, timeout=QUERY_TIMEOUT)

        adapter_options = {
            "service_type": "metric-storage",
            "region_name": request.user.services_region,
            "interface": interface,
        }

        # discovers the Aetos endpoint via service catalog
        self.client = obs_client_utils.get_prom_client_from_keystone(
            sess, adapter_options=adapter_options
        )

    def get_volumes_count(self) -> int:
        """
        Get telemetry volumes count.

        Returns:
            int: Current count of volumes from Ceilometer metrics
        """
        result = self.client.query(
            query=f"count(last_over_time(ceilometer_volume_size[{stale_window()}]))"
        )
        if result and len(result) > 0:
            return int(float(result[0].value))
        return 0

    def get_snapshots_count(self) -> int:
        """
        Get telemetry snapshots count.

        Returns:
            int: Current count of snapshots from Ceilometer metrics
        """
        result = self.client.query(
            query=f"count(last_over_time(ceilometer_volume_snapshot_size[{stale_window()}]))"
        )
        if result and len(result) > 0:
            return int(float(result[0].value))
        return 0

    def get_storage_pools(self) -> list:
        """
        Get every storage pool's capacity from Ceilometer metrics.

        One query per metric for all pools, whatever their number, plus
        one for the time of each pool's last report. Pools that reported
        in the last POOL_LOOKBACK are returned, those silent for more than
        the staleness window (config.get_storage_stale_minutes) with
        ``stale`` set: their values are frozen at
        their last report.

        Returns:
            list: one dict per pool: ``name``, ``total``, ``allocated``,
            ``free``, ``provisioned``, ``virtual_free`` (GiB; None when the
            pool does not report it), ``last_seen`` (epoch) and ``stale``.
            Errors are raised to the caller.
        """
        values = {}
        for field in POOL_FIELDS:
            query = (
                f"max by (resource) (last_over_time("
                f"{POOL_METRIC_PREFIX}{field}[{POOL_LOOKBACK}]))"
            )
            for metric in self.client.query(query=query) or []:
                name = metric.labels.get("resource")
                if name:
                    values.setdefault(name, {})[field] = number(metric.value)

        last_seen = {}
        for metric in self.client.query(query=pool_last_seen_query()) or []:
            name = metric.labels.get("resource")
            if name:
                last_seen[name] = number(metric.value)

        now = time.time()
        stale_seconds = config.get_storage_stale_minutes() * 60
        pools = []
        for name in sorted(values):
            fields = values[name]
            if fields.get("total") is None:
                continue
            seen = last_seen.get(name)
            pool = {
                "name": name,
                "total": fields["total"],
                "allocated": fields.get("allocated") or 0.0,
                "free": fields.get("free") or 0.0,
                "provisioned": fields.get("provisioned") or 0.0,
                "virtual_free": fields.get("virtual_free"),
                "last_seen": seen,
                "stale": seen is None or now - seen > stale_seconds,
            }
            pools.append(pool)
        return pools

    def get_capacity_predictions(self) -> list:
        """
        Predict when storage pools run out of space.

        Thin pools usually run out of *virtual* capacity (Cinder's
        over-subscription limit) long before free space drops: free only
        shrinks when data is written. Both free and virtual free are
        therefore forecast with ``predict_linear`` over the last 7 days,
        and the one that reaches zero first is reported. One query per
        metric for all pools, plus one for the length of their history.

        Returns:
            list: one dict per pool reporting now:

                - name: pool name
                - current_free, current_virtual_free: GiB (None if not
                  reported)
                - days_physical, days_virtual: days until each reaches
                  zero at the 7-day trend, or None if it is not shrinking
                - days_until_full: the smaller of the two, or None
                - limiting: "physical" or "virtual" (the one reaching zero
                  first), or None
                - will_run_out: True when a forecast exists
                - history_days: days of history (at most 7)
                - enough_history: True with 7 days of history; the
                  forecast fields are only set then

            Errors are raised to the caller.
        """
        current = {"free": {}, "virtual_free": {}}
        predicted = {"free": {}, "virtual_free": {}}
        for field in ("free", "virtual_free"):
            metric_name = f"{POOL_METRIC_PREFIX}{field}"
            query = (
                f"max by (resource) (last_over_time("
                f"{metric_name}[{stale_window()}]))"
            )
            for metric in self.client.query(query=query) or []:
                name = metric.labels.get("resource")
                if name:
                    current[field][name] = number(metric.value)
            query = (
                f"max by (resource) (predict_linear("
                f"{metric_name}[{FORECAST_HISTORY}], "
                f"{FORECAST_HORIZON_DAYS} * 86400))"
            )
            for metric in self.client.query(query=query) or []:
                name = metric.labels.get("resource")
                if name:
                    predicted[field][name] = number(metric.value)

        history = {}
        for metric in self.client.query(query=pool_history_query()) or []:
            name = metric.labels.get("resource")
            if name:
                history[name] = number(metric.value)

        predictions = []
        for name in sorted(current["free"].keys() | current["virtual_free"]):
            seconds = history.get(name) or 0.0
            enough = seconds >= FORECAST_MIN_HISTORY_SECONDS
            prediction = {
                "name": name,
                "current_free": current["free"].get(name),
                "current_virtual_free": current["virtual_free"].get(name),
                "days_physical": None,
                "days_virtual": None,
                "days_until_full": None,
                "limiting": None,
                "will_run_out": False,
                "history_days": round(min(seconds / 86400.0, 7.0), 1),
                "enough_history": enough,
            }
            if enough:
                prediction["days_physical"] = days_until_zero(
                    current["free"].get(name), predicted["free"].get(name)
                )
                prediction["days_virtual"] = days_until_zero(
                    current["virtual_free"].get(name),
                    predicted["virtual_free"].get(name),
                )
                candidates = [
                    (days, limiting)
                    for days, limiting in (
                        (prediction["days_physical"], "physical"),
                        (prediction["days_virtual"], "virtual"),
                    )
                    if days is not None
                ]
                if candidates:
                    days, limiting = min(candidates)
                    prediction["days_until_full"] = days
                    prediction["limiting"] = limiting
                    prediction["will_run_out"] = True
            predictions.append(prediction)
        return predictions

    def query_matrix(self, query, ndigits=None, time=None):
        """
        Run an instant query that returns a range vector (e.g. a subquery).

        Aetos only exposes the instant query endpoint, and
        ``PrometheusAPIClient.query`` only parses instant vectors, so the
        raw response is decoded here. Non-finite values are dropped.

        Args:
            ndigits: round values (for display data), or None
            time: evaluation time (epoch seconds), or None for "now". Pass
                the time the chart grid is built from
                (:func:`grian_ui.timeseries.range_grid`), so the points and
                the grid line up even if the clocks differ.

        Returns:
            list: ``[(labels, [[timestamp, value], ...]), ...]``
        """
        params = {"query": query}
        # PrometheusAPIClient has no public call that returns the raw
        # response; _get is the method its own query() uses.
        if time is not None:
            params["time"] = f"{time:.3f}"
        decoded = self.client._get("query", params)
        data = decoded.get("data", {})
        if data.get("resultType") != "matrix":
            LOG.warning(
                "Expected a range vector, got %s, for query %s",
                data.get("resultType"),
                query,
            )
            return []
        series = []
        for result in data.get("result", []):
            points = []
            for timestamp, value in result.get("values", []):
                sample = number(value, ndigits)
                if sample is not None:
                    points.append([float(timestamp), sample])
            series.append((result.get("metric", {}), points))
        return series


def get_client(request) -> DatasourceClient:
    """
    Get a Prometheus telemetry client instance.

    Args:
        request: Django HTTP request with user authentication

    Returns:
        PrometheusTelemetryClient: New client instance for this request
    """
    return PrometheusTelemetryClient(request)
