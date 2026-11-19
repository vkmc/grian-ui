========================
Storage Health Dashboard
========================

The Storage Health dashboard (formerly Volume Metrics) tells cloud
administrators whether Cinder block storage is healthy and when it runs
out, across the deployment. It is accessible under
**Admin > Telemetry > Storage Health** and requires the
``openstack.roles.admin`` permission. Its URL is still
``/admin/volume_metrics/`` and its enabled file
``_1001_admin_volume_metrics_panel.py``, so existing links, bookmarks and
deployer configuration keep working. For a guide to reading the page, see
:doc:`user-storage-health`.

All metrics originate from `Ceilometer <https://docs.openstack.org/ceilometer/latest/>`_
volume pollster data, stored in Prometheus via the
`sg-core <https://github.com/infrawatch/sg-core>`_ telemetry pipeline, and
queried through the `Aetos <https://opendev.org/openstack/aetos>`_
observability proxy (``service_type: metric-storage``).

Ceilometer meters follow a naming convention when exposed as Prometheus
metrics: dots are replaced with underscores and a ``ceilometer_`` prefix is
added (e.g. ``volume.size`` becomes ``ceilometer_volume_size``).

The state of volumes and snapshots (errors, stuck operations, clean-up
candidates) is not in Ceilometer: it is read from the Block Storage API
(Cinder) with the administrator's token, see
`Volumes and snapshots needing attention`_.


Panels
======

Verdict
-------

One sentence at the top sums the page up, the first that applies:

.. list-table::
   :header-rows: 1
   :widths: 35 15 50

   * - Verdict
     - Level
     - Rule
   * - *N* pools nearly full
     - critical
     - A pool reporting now is critical: physical or virtual use at the
       critical threshold, or forecast full within the critical number of
       days.
   * - *N* pools stopped reporting
     - warning
     - A pool is stale (no report within the staleness window, 10
       minutes by default): usually its
       ``cinder-volume`` service is down.
   * - *N* volumes or snapshots in an error state
     - warning
     - See `Volumes and snapshots needing attention`_.
   * - *N* pools filling up
     - warning
     - A pool reporting now is at the warning level.
   * - *N* volume operations stuck
     - warning
     - A volume or snapshot has been in a transitional state for longer
       than ``stuck_minutes``.
   * - Storage is healthy
     - ok
     - None of the above. Adds how many days of history the forecasts
       have while it is less than 7.

Summary Cards
-------------

Four metric cards displayed under the verdict, auto-refreshing every
**60 seconds** (while the browser tab is visible). The allocated storage
card is coloured by the worse of the cloud's physical use (allocated ÷
total) and virtual use (provisioned ÷ over-subscription limit), the volumes
card by the volumes and snapshots needing attention (red: errors, orange:
stuck operations); it says how many need attention, and the snapshots card
how many clean-up candidates there are.

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


Volumes and snapshots needing attention
---------------------------------------

Replaces the former "Volume and Snapshot Distribution" chart, which
repeated the card counts. At most **25 rows**, worst first, from the Block
Storage API, all projects:

.. list-table::
   :header-rows: 1
   :widths: 15 55 30

   * - Severity
     - Rule
     - Typical action
   * - Critical
     - Volumes in ``error``, ``error_deleting``, ``error_restoring`` or
       ``error_extending``; snapshots in ``error`` or ``error_deleting``.
     - Read the ``cinder-volume`` log; reset the state and retry, or
       delete.
   * - Warning
     - Volumes in ``creating``, ``attaching``, ``detaching`` or
       ``deleting``, snapshots in ``creating`` or ``deleting``, for more
       than ``stuck_minutes`` (30) since their last update.
     - Usually a backend or service problem.
   * - Clean-up
     - Volumes ``available``, unattached and unchanged (last update) for
       ``unattached_days`` (30) or more; snapshots older than
       ``old_snapshot_days`` (90) with a newer snapshot of the same volume.
     - Ask the owning project.

Within a severity, the oldest come first. The backend is the volume's
``host@backend`` (a snapshot's comes from its volume). The footer counts
each severity and names up to three unattached volumes that are not old
enough for the clean-up rule yet.

**Block Storage API calls.** The same limits as the other Telemetry
dashboards (Instance Health reads the Compute API the same way):

.. list-table::
   :header-rows: 1
   :widths: 35 20 45

   * - Call
     - How often
     - Kept
   * - ``GET /v3/{project}/volumes/detail?all_tenants=1`` and
       ``.../snapshots/detail?all_tenants=1``, following every page (one
       request per ``osapi_max_limit`` items, 1000 by default)
     - once per region every **2 minutes**
     - a bounded summary: up to 500 attention rows, worst first, with
       exact counts (25 rows shown), three names
   * - ``GET /v3/projects`` (Identity API, project names)
     - every **5 minutes**
     - project ID to name

Every section and administrator shares these values through the Horizon
cache; page refreshes never call the Block Storage API themselves, so
state changes can show up to 2 minutes late. A copy older than its
lifetime plus 30 seconds is flagged as possibly out of date. When the
Block Storage API fails, the verdict and cards
are built from the metrics alone and say that volumes were not checked.

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
  label (by default green below 75%, orange from 75%, red from 90%; see
  `Configuration`_).

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

Status labels (with the default thresholds):

- **Full in N days** (red within 14 days, orange within 30 days): a
  capacity reaches zero.
- **Declining: full in N days** (grey): a capacity is decreasing but will
  not reach zero within 30 days.
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

Override these settings in ``local_settings``, for example:

.. code-block:: python

   GRIAN_PLUGIN = {
       "datasource": "prometheus",
       "storage_stale_minutes": 20,
       "storage_thresholds": {"physical_warning": 80},
   }

``storage_stale_minutes`` is the **staleness window** (``[10m]`` in the
queries above): only reports within it count as current, and a pool
without a report for longer is stale. It must be a whole number of minutes
from 1 to 360 (default 10), at least twice the Ceilometer polling
interval.

``storage_thresholds`` overrides the status thresholds per key:

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Key
     - Default
     - Meaning
   * - ``physical_warning``, ``physical_critical``
     - 75, 90
     - Physical use of a pool (allocated ÷ total), percent
   * - ``virtual_warning``, ``virtual_critical``
     - 75, 90
     - Virtual use of a pool (provisioned ÷ over-subscription limit),
       percent
   * - ``forecast_warning_days``, ``forecast_critical_days``
     - 30, 14
     - A pool forecast to be full within this many days
   * - ``stuck_minutes``
     - 30
     - A volume or snapshot in a transitional state for longer is stuck
   * - ``unattached_days``
     - 30
     - Clean-up: volumes unattached and unchanged for this many days
   * - ``old_snapshot_days``
     - 90
     - Clean-up: snapshots this old with a newer one of the same volume

Percentages go up to 100, each warning must not be greater than its
critical value, and ``forecast_critical_days`` must not be greater than
``forecast_warning_days``. An invalid value is shown at the top of the
page and logged.


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
