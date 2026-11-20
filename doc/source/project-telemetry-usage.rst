===============
Project Usage
===============

The Usage page is a monthly report of what your project held and what it
actually used: CPU, memory, network and volume storage, per day and per
instance. Open it from **Project > Telemetry > Usage**. You need the
reader or member role in the project; the page only ever shows your current
project (switch project in the top bar to see another one).

**This is a usage report, not a bill.** If your cloud charges for use,
prices come from its billing system.

The first part of this page explains how to read the report. The second
part, `Operator notes`_, is for cloud operators: settings, the metrics and
APIs used, and the load the page adds.


Using the report
================

Period and export
-----------------

The buttons at the top select a calendar month (UTC). The current month is
shown "to date". Months whose metrics have all been deleted are greyed out
with a lock and cannot be selected: hover over them to see why.

Next to the buttons, **Covers ...** gives the exact time range the totals
add up, for example *11/6 00:00 – 11/20 15:40 UTC (15 of 20 days)*. When
your project's first usage is later than the start of the range (a new
project, or instances created this month), the time of that first usage is
added.

**Export CSV** downloads the by-instance table of the selected period: one
row per instance, in machine-readable units, with ``period_start`` and
``period_end`` columns (ISO 8601, UTC). The file name gives the range, for
example ``usage-demo-2026-11-06_to_2026-11-20.csv``. Columns:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Column
     - Content
   * - ``period_start``, ``period_end``
     - The range the row covers (the same for every row)
   * - ``instance``, ``instance_id``
     - Instance name and UUID
   * - ``flavor``
     - Flavor name, or the size reported by the metrics for instances
       deleted since
   * - ``hours_running``
     - Hours the instance reported metrics in the period
   * - ``vcpu_hours_held``, ``cpu_hours_used``
     - vCPUs of the flavor × hours running; CPU time actually used, in hours
   * - ``ram_gb_hours_held``, ``ram_gb_hours_used``
     - Flavor RAM (GiB) × hours running; memory used (GiB) × hours
   * - ``network_in_bytes``, ``network_out_bytes``
     - Bytes received and sent, all interfaces

The page refreshes itself every 5 minutes while its browser tab is visible.

How far back it goes
--------------------

The report can only cover what the cloud's metric store keeps. Prometheus
keeps **15 days** unless your operator changed it, so a full month is often
not available. The page says so openly:

* A **yellow "Incomplete month" warning** appears when the start of the
  month has already been deleted. It names the missing days and how many
  days the totals cover.
* A **blue "Metrics are kept for N days" note** appears in the current
  month while nothing has been deleted yet: a report run at the end of the
  month will only cover its last N days.
* In the daily chart, **days without data are a grey band with a label**
  ("not kept: older than 15 days"), never zero-height bars.
* **Months entirely outside the retention** are locked in the period
  selector.

The totals always start at the beginning of a day (the first full day still
kept), so days can be compared. If you need complete monthly figures, ask
your operator to keep metrics longer or to keep daily totals, or export the
report before the oldest days age out.

Summary cards
-------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Card
     - Meaning
   * - CPU-hours used
     - CPU time your instances used, out of the vCPU-hours they held, with
       the percentage used.
   * - RAM GB-hours used
     - Memory used inside the instances (GiB × hours), out of the flavor
       RAM they held.
   * - Network traffic
     - Bytes received plus sent by every instance interface, with the split
       in / out.
   * - Volume GB-hours
     - Size of your volumes plus snapshots (GiB) × hours they existed.

When no instance or volume of the project reported metrics in the kept part
of the period, a single grey card says so.

Daily usage
-----------

Bars per UTC day: **held** (light blue, flavor size × hours running) next to
**used** (dark blue). The **CPU**, **Memory** and **Network** buttons switch
the metric (network has no "held" bar). A large gap between the two bars
every day is capacity held in quota but not used; compare with the *Use of
held CPU* column below and with *Oversized* on the Compute page. Kept days
where nothing ran are zero; days whose metrics are missing are grey bands.

By instance
-----------

Every instance that ran in the period, the busiest (most vCPU-hours held)
first, with a total row:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Column
     - Meaning
   * - Instance
     - Name; links to the instance's metrics page when that panel is
       installed. Instances deleted since are marked *(deleted)*; instances
       not ACTIVE now show their state.
   * - Flavor
     - Flavor name from the Compute API, or the vCPUs and RAM reported by
       the metrics for deleted instances.
   * - Hours running
     - Hours the instance reported metrics in the period (in 5-minute
       steps).
   * - vCPU-hours held
     - vCPUs of the flavor × hours running.
   * - CPU-hours used
     - Actual CPU time, all vCPUs together.
   * - Use of held CPU
     - Used ÷ held. Values under 10% (configurable) are marked *low*: the
       instance is probably oversized.
   * - RAM GB-hours held / used
     - The same for memory.
   * - Network in / out
     - Bytes received and sent.

Instances that hold quota without running in the period (ERROR, SHUTOFF,
SHELVED...) report no metrics, so they cannot appear in the table. In the
current month they are listed under the table with their state. Nova's own
usage summary on **Project > Overview** counts them for their whole
lifetime.

*Example (demo project, November 2026 to date):* the two instances ran
53.8 hours each. They held 268.8 vCPU-hours and used 224.3 (83%), almost
all of it ``grian-ih-cpu-2``. They held 40.3 RAM GB-hours and used 4.8
(12%).

States of a section
-------------------

* **Loading** (blue, spinner): the data is being computed for your
  project by another request; the section retries after a few seconds.
* **Error loading telemetry data** (red): the metric store could not be
  queried; the section tries again at the next refresh. Details are in the
  Horizon logs, not on the page.
* **Could not be refreshed** (yellow): the last refresh failed; the last
  data received is shown, with its time.
* **Refreshing** (blue): the data shown may be a few minutes old while a
  slow refresh runs.
* If only the Compute API fails, the table still shows: names and sizes
  then come from the metrics, and a note says so.

"How this page is calculated", at the bottom of the page, lists the queries
and the thresholds.


Operator notes
==============

Requirements
------------

* The Ceilometer compute agent polling the instance meters ``vcpus``,
  ``memory``, ``memory.usage``, ``cpu``, ``network.incoming.bytes`` and
  ``network.outgoing.bytes``, and the volume meters ``volume.size`` and
  ``volume.snapshot.size`` (all in the default configuration).
* sg-core, Prometheus and Aetos (``service_type: metric-storage``), as for
  the other Telemetry panels. The panel is shown to users allowed
  ``telemetry:query`` by the Aetos policy.
* The Compute API, for the instance names, flavors and states.

Enable the panel by copying
``src/grian_ui/local/enabled/_1104_project_telemetry_usage_panel.py``,
together with the panel group files ``_1000_admin_telemetry_panel_group.py``
(adds the application) and ``_1100_project_telemetry_panel_group.py`` (the
Project > Telemetry group), to Horizon's ``local/enabled`` directory, then
restart the web server.

Configuration
-------------

.. code-block:: python

   GRIAN_PLUGIN = {
       "datasource": "prometheus",
       # Prometheus --storage.tsdb.retention.time, in days (default 15)
       "metrics_retention_days": 15,
       # optional: "use of held CPU" below this percentage is "low"
       "project_usage_thresholds": {"cpu_use_low": 10},
   }

``metrics_retention_days``
    Project users cannot read the retention from Aetos (its status endpoint
    needs ``telemetry:status``, limited to admins and services), so the page
    takes it from this setting to compute what a month covers, which
    months are locked and where the grey bands go. Set it to the Prometheus
    retention. A value larger than the real retention makes the page show
    deleted days as days without usage. For complete monthly reports, keep
    more than 31 days, or keep daily totals with recording rules.

``project_usage_thresholds``
    ``cpu_use_low`` (percent, from 0 to less than 100, default 10). The
    label follows the percentage as shown, rounded to a whole number.

Invalid values are shown at the top of the page and logged once; the
sections then show "invalid configuration".

Data and queries
----------------

Every query names its metrics (Aetos refuses selectors without a metric
name for project users) and holds the current project
(``project="<id>"``): Aetos filters queries by the token's project only for
users without an admin role, so an administrator browsing the Project
dashboard would otherwise see every project.

Only polled instance samples are used: ``resource_name=~".+:instance-.+"``
for the instance meters (Nova notifications publish the same meters with a
plain ``resource_name``, which would count every instance twice), and
``resource_name=~".+:.+"`` (``<instance>:<vNIC>``) for the network meters.
Each series is reduced to one value per resource (``max by (resource)``)
before summing, so duplicated series are not counted twice either. Network
interfaces are attributed to their instance by the UUID in their
``resource`` label.

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Value
     - Query (over the covered range)
   * - Hours running
     - ``count_over_time((max by (resource) (ceilometer_vcpus{…}))[…:5m])``
       × 5 minutes: an instance counts as running for a 5-minute step when
       it has a sample in it (see `Limits`_ on sparse samples).
   * - vCPU-hours held
     - ``sum_over_time(… ceilometer_vcpus …[…:5m])`` × 5 minutes.
   * - RAM GB-hours held / used
     - The same with ``ceilometer_memory`` / ``ceilometer_memory_usage``
       (MiB), ÷ 1024.
   * - CPU-hours used
     - ``increase(ceilometer_cpu{…}[…]) / 1e9 / 3600`` (handles counter
       resets when an instance reboots).
   * - Network in / out
     - ``increase(ceilometer_network_{incoming,outgoing}_bytes{…}[…])``.
   * - Volume GB-hours
     - ``sum_over_time(sum(max by (resource) (ceilometer_volume_size{…}))
       […:5m])`` × 5 minutes, plus the same for
       ``ceilometer_volume_snapshot_size``.
   * - Names and sizes of deleted instances
     - ``last_over_time(ceilometer_vcpus{…}[…])`` and
       ``last_over_time(ceilometer_memory{…}[…])``; the name is the
       ``resource_name`` with the latest sample,
       ``max_over_time(timestamp(ceilometer_vcpus{…})[…:5m])`` (a renamed
       instance has one series per name), and the sizes are those of that
       same series.
   * - Daily chart
     - The same per hour, as one subquery ``(…)[…:1h]`` ending at the end
       of the current hour, summed per UTC day by Horizon.

The covered range starts at the later of the start of the month and the
first full UTC day still kept (now − ``metrics_retention_days``, rounded up
to midnight), and ends at the end of the month or now. Subquery ranges are
one second short of the covered range, so a step on the boundary is never
counted twice (Prometheus 2 includes a point at the start of a range).

Calls to other services and load
--------------------------------

* **Two Prometheus queries per report** (project and month), whatever the
  number of instances or the length of the period: one instant query for
  the per-instance totals and the volume GB-hours, one subquery for the
  hourly totals. The subqueries evaluate about 12 points per hour and
  series (8,900 for a 31-day month); a month of a project with many
  instances is the heaviest case.
* **One Compute API call** (``GET /servers/detail``, with the user's token,
  so only the current project) per project and minute, plus a flavor list
  only with old compute API microversions that do not embed the flavor
  name.
* Results are cached in the Horizon cache with a key that holds the project
  ID (``grian_ui.cache.project_cache_key``): the summary, chart, table and
  CSV of one project share one report, computed once while every member of
  the project watching the page shares it. Two projects never share a
  value. The current month is cached for 295 seconds and refreshed with the
  page every 5 minutes; a past month for an hour (its covered range only
  changes when a day ages out, which changes the cache key). A failed
  refresh shows the last good copy for up to 10 minutes.
* The page shell itself queries nothing; each section loads separately and
  only while its browser tab is visible.

Limits
------

* Months are calendar months in UTC, and days are UTC days.
* Instances that never ran have no metrics. They appear only in the note
  under the table, and only for the current month (the Compute API gives
  the current state, not the history).
* Instances deleted since the period keep their metrics name and size, but
  no flavor name.
* Values depend on the polling interval: an instance that ran for less than
  one polling interval may not appear.
* Held hours are counted on a 5-minute grid. They do not depend on the
  polling interval only because sg-core exposes each polled value until
  the next poll and Prometheus scrapes it every 15 seconds, so a running
  instance has samples in every 5-minute step. If the samples are sparser
  than that (for example polled samples stored as they arrive, with a
  polling interval longer than 5 minutes), steps without a sample are not
  counted and held hours are under-counted.
* An instance renamed during the period is listed under its latest name
  (the name of its latest sample), with the flavor size of that same
  series (its latest size, even if it was also resized).
