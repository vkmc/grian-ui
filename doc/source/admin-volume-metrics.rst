========================
Volume Metrics Dashboard
========================

The Volume Metrics dashboard provides cloud administrators with a real-time
overview of Cinder block storage resources across the deployment. It is
accessible under **Admin > Telemetry > Volume Metrics** and requires the
``openstack.roles.admin`` permission.

All metrics originate from `Ceilometer <https://docs.openstack.org/ceilometer/latest/>`_
volume pollster data, stored in Prometheus via the
`sg-core <https://github.com/infrawatch/sg-core>`_ telemetry pipeline, and
queried through the `Aetos <https://opendev.org/openstack/aetos>`_
observability proxy (``service_type: metric-storage``).

Ceilometer meters follow a naming convention when exposed as Prometheus
metrics: dots are replaced with underscores and a ``ceilometer_`` prefix is
added (e.g. ``volume.size`` becomes ``ceilometer_volume_size``).


Panels
======

Summary Cards
-------------

Four metric cards displayed at the top of the dashboard, auto-refreshing
every **60 seconds** (while the browser tab is visible).

.. list-table::
   :header-rows: 1
   :widths: 25 30 45

   * - Card
     - Ceilometer Meter
     - PromQL
   * - Total Storage (GiB)
     - ``volume.provider.pool.capacity.total``
     - Sum of the pools (see `Storage Pools`_)
   * - Allocated Storage (GiB)
     - ``volume.provider.pool.capacity.allocated``
     - Sum of the pools (see `Storage Pools`_)
   * - Total Volumes
     - ``volume.size``
     - ``count(last_over_time(ceilometer_volume_size[10m]))``
   * - Total Snapshots
     - ``volume.snapshot.size``
     - ``count(last_over_time(ceilometer_volume_snapshot_size[10m]))``

**Total Storage** and **Allocated Storage** aggregate the capacity reported
by the Cinder volume backend(s) (e.g. LVM, Ceph) across all storage pools,
including stale pools at their last reported values; the Total Storage card
says how many pools there are and how many are stale.
**Total Volumes** and **Total Snapshots** count the number of distinct
Ceilometer samples, which corresponds to the number of active volumes and
snapshots in the deployment.


Volume and Snapshot Distribution (Doughnut Chart)
-------------------------------------------------

A Chart.js doughnut chart showing the proportion of volumes to snapshots.
Refreshed with the summary cards, from the same ``count()`` queries.

.. list-table::
   :header-rows: 1
   :widths: 25 30 45

   * - Segment
     - Ceilometer Meter
     - PromQL
   * - Volumes
     - ``volume.size``
     - ``count(ceilometer_volume_size)``
   * - Snapshots
     - ``volume.snapshot.size``
     - ``count(ceilometer_volume_snapshot_size)``

This chart gives a quick visual ratio of how storage objects are distributed
between volumes and snapshots.


Storage Pools
-------------

A per-pool breakdown with capacity bars and a detailed table. Rendered on
page load.

Every pool comes from one query per metric, whatever the number of pools
(six queries in all). The ``resource`` label holds the Cinder pool name;
``max by (resource)`` merges duplicate series (several publishers or
sg-core replicas).

.. list-table::
   :header-rows: 1
   :widths: 25 30 45

   * - Metric
     - Ceilometer Meter
     - PromQL
   * - Total capacity
     - ``volume.provider.pool.capacity.total``
     - ``max by (resource) (last_over_time(ceilometer_volume_provider_pool_capacity_total[24h]))``
   * - Allocated capacity
     - ``volume.provider.pool.capacity.allocated``
     - The same with ``..._allocated``
   * - Free capacity
     - ``volume.provider.pool.capacity.free``
     - The same with ``..._free``
   * - Provisioned capacity
     - ``volume.provider.pool.capacity.provisioned``
     - The same with ``..._provisioned``
   * - Virtual free capacity
     - ``volume.provider.pool.capacity.virtual_free``
     - The same with ``..._virtual_free``
   * - Last report
     - ``volume.provider.pool.capacity.total``
     - ``max by (resource) (max_over_time(timestamp(...total)[15m:30s]))``,
       or for pools silent for longer the same over ``[24h:5m]``

Each pool is displayed with:

- A **capacity bar** showing the percentage of allocated vs. total capacity.
- A **detail row** with free and provisioned values.
- A **collapsible table** with all numeric values, the virtual free
  capacity, the time of the last report and a color-coded usage percentage
  label (green < 75%, yellow 75-89%, red >= 90%).

**Stale pools.** A pool that has not reported for 10 minutes (the
staleness window, see `Configuration`_), usually because its
``cinder-volume`` service is down, is marked **Stale**: its values are
frozen at its last report, shown with the time of that report. Pools
silent for more than 24 hours are no longer listed. The staleness window
must be at least twice the Ceilometer polling interval (300 seconds by
default).

All values are in GiB, matching the units reported by Cinder's volume
backend statistics.


Capacity Forecast
-----------------

A prediction table that forecasts when each storage pool will run out of
space. It refreshes every **60 seconds**; the forecasts themselves are
recomputed every 5 minutes.

A pool runs out when either of two capacities reaches zero:

- **free** capacity, the physical space left. It only shrinks when data is
  written, so on thin-provisioned pools it can stay equal to the total
  while volumes are created;
- **virtual free** capacity, what Cinder can still provision under the
  over-subscription limit (total × ``max_over_subscription_ratio``, minus
  what is provisioned). On thin pools this usually runs out first, and
  Cinder then refuses new volumes although free space is left.

Both are forecast and the status shows whichever reaches zero first, with
its limiting factor (*physical* or *virtual*). One query per metric for all
pools:

.. list-table::
   :header-rows: 1
   :widths: 25 30 45

   * - Metric
     - Ceilometer Meter
     - PromQL
   * - Current free capacity
     - ``volume.provider.pool.capacity.free``
     - ``max by (resource) (last_over_time(ceilometer_volume_provider_pool_capacity_free[10m]))``
   * - Predicted free in 30 days
     - ``volume.provider.pool.capacity.free``
     - ``max by (resource) (predict_linear(ceilometer_volume_provider_pool_capacity_free[7d], 30 * 86400))``
   * - Current and predicted virtual free capacity
     - ``volume.provider.pool.capacity.virtual_free``
     - The same two queries with ``..._virtual_free``
   * - History
     - ``volume.provider.pool.capacity.total``
     - ``max by (resource) (time() - min_over_time(timestamp(...total)[7d:1h]))``

The ``predict_linear()`` function uses linear regression over the last
**7 days** of samples to project the value **30 days** into the future.
The dashboard computes the estimated number of days until each capacity
reaches zero using linear interpolation::

    decline_per_day = (current - predicted_value) / 30
    days_until_full = current / decline_per_day

Status labels:

- **Full in N days** (red): a capacity reaches zero within 30 days.
- **Declining: full in N days** (yellow): a capacity is decreasing but
  will not reach zero within 30 days.
- **Healthy** (green): both capacities are stable or increasing.
- **Insufficient data (N of 7 days)** (grey): the pool has less than 7
  days of history.

Only pools that reported within the staleness window are forecast: the values
of stale pools are frozen.

.. note::

   Predictions require at least 7 days of continuous Ceilometer data
   collection. On a fresh deployment, this panel will show "Insufficient
   data" until enough history accumulates.


Refresh, caching and errors
---------------------------

The pool metrics and the volume and snapshot counts (eight queries) are
read at most once per refresh interval, the forecasts (five queries) at
most every 5 minutes. Each result is computed once and shared through the
Horizon cache by the page sections and by every administrator of the
region (this needs a cache shared by the Horizon processes, such as
memcached, the Horizon default).

When Aetos cannot be reached, each section keeps showing the last good data
with the time it was fetched ("Could not be refreshed: showing the last
data received, from 4 min ago") and retries at the next refresh; error
details go to the Horizon log, not to the page. A section without any data
yet shows a plain error message.


Configuration
=============

``[10m]`` in the queries above is the **staleness window**: only reports
within it count as current, and a pool without a report for longer is
stale. Override it in ``local_settings``:

.. code-block:: python

   GRIAN_PLUGIN = {
       "datasource": "prometheus",
       "storage_stale_minutes": 20,
   }

It must be a whole number of minutes from 1 to 360 (default 10), at least
twice the Ceilometer polling interval. An invalid value is logged and the
page sections say that the configuration is invalid.


Data Pipeline
=============

The metrics flow through the following pipeline::

    Cinder → Ceilometer pollster → sg-core (STF) → Prometheus → Aetos → Horizon

1. **Ceilometer** polls Cinder every 600 seconds (default) for volume and
   pool statistics.
2. **sg-core** receives Ceilometer samples over AMQP/TCP and writes them as
   Prometheus metrics.
3. **Prometheus** stores the time-series data and serves PromQL queries.
4. **Aetos** acts as a Keystone-authenticated proxy to Prometheus, registered
   in the service catalog as ``metric-storage``.
5. **Horizon (grian-ui)** queries Aetos using ``python-observabilityclient``
   and renders the results.
