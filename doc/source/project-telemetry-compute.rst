=============================
Project Compute Dashboard
=============================

**Project > Telemetry > Compute** shows your project's compute quota, what
holds it, and the health and sizing of your instances. Every project member
can open it; it only ever shows the current project.

The first part of this page explains how to read and use the dashboard. The
second part, `Operator notes`_, explains how it is configured, where its
data comes from and what load it puts on the cloud.


Using the dashboard
===================

The page refreshes itself every 60 seconds while its browser tab is
visible. Times are shown in your browser's time zone.

Controls
--------

* **6h / 24h / 7d / 30d**: the period of the *Quota usage over time* chart
  (24h by default). Ranges longer than the metric history kept by your
  cloud are disabled. Rightsizing always looks at the last 7 days.
* **vCPUs / RAM / Instances**, above the chart: the quota the chart shows.
* **All / Undersized / Oversized / Right-sized**, above the rightsizing
  table: show one group only.
* Clicking a **rightsizing row** opens CPU and memory charts for that
  instance; clicking an **instance name** opens its metrics page, when the
  Instance metrics page is installed.

Summary cards
-------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Card
     - Meaning
   * - Highest quota use
     - The fullest of your instances, vCPU and RAM quotas. Yellow from 75%,
       red from 90%.
   * - Instances running
     - Running instances out of all instances in the project.
   * - RAM quota held by idle instances
     - RAM held by instances that are not running (ERROR, SHUTOFF, paused,
       suspended, shelved or shelved-offloaded). Red when it is a quarter of
       your RAM quota or more.
   * - Instances to resize
     - How many running instances look undersized or oversized (see
       `Allocated vs used (rightsizing)`_). *Provisional* while there is
       less than 7 days of history.

Two notices can appear above the cards:

* **Could not load telemetry data**: the metric-storage service (Aetos) did
  not answer. The quota numbers come from the Compute API and are still up
  to date; the trends, rightsizing and symptoms are unavailable until it
  answers again.
* **About N hours of history**: your instances are younger than 7 days (or
  the metrics are not kept that long), so the rightsizing assessments are
  provisional and there is no forecast yet.

Compute quota
-------------

One bar per quota (instances, vCPUs, RAM), with the same numbers as
**Project > Overview**, split by what holds them:

* **blue**: running instances;
* **grey**: shut-off instances (also paused, suspended, shelved and
  shelved-offloaded ones);
* **red stripes**: instances in ERROR.

The column on the right is a **forecast**: at the growth rate of the last
30 days (fewer if your cloud keeps less history), when this quota will be
full. Red means within 14 days, yellow within 30. *Not growing* means usage
is flat or falling; *Not enough history* means there is less than 7 days of
metrics.

Quota usage over time
---------------------

* **Blue area**: what your running instances held over time (from
  telemetry).
* **Red dashed line**: what the quota counts now (from the Compute API).
* **Grey dashed line**: the quota limit.

A gap between blue and red means quota is held by instances that aren't
running. The note below the chart gives the size of the gap. Gaps in the
blue area mean nothing ran, or the metrics did not exist yet.

Holding quota without running
-----------------------------

Instances in ERROR or SHUTOFF state (and paused, suspended, shelved or
shelved-offloaded ones), largest RAM first, with how much quota each one
holds (at most 10 rows).

* **ERROR** instances never started (the fault is shown). They usually only
  need deleting.
* **SHUTOFF** instances can be started again. Shelving them frees the
  host, but on most clouds a shelved-offloaded instance still counts
  against your vCPU and RAM quotas: only deleting it frees them.

The **Delete…** and **Manage…** buttons open the instance in
**Project > Compute > Instances**.

*Real example (demo project, 2026-11-20):* ``grian-ih-error`` (flavor
``grian.toobig``, ERROR "No valid host was found.") holds 40 GiB, 80% of
the project's RAM quota. The project's two running instances hold 768 MiB
and use about 92 MiB.

Allocated vs used (rightsizing)
-------------------------------

The first line sums up how much of the vCPUs and RAM held by running
instances is actually used, on average over the last 7 days. Each row
compares one running instance with its flavor (at most 25 rows):

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Column
     - Meaning
   * - CPU used ÷ vCPUs
     - Average CPU in use over the last 7 days, in vCPUs. Red bar: busy
       (≥ 80%). Pale bar: nearly idle.
   * - Memory used ÷ flavor RAM
     - Average memory the guest reports as used. A *swap* tag means the
       guest swapped in the last 7 days.
   * - Root disk used
     - How full the instance's first disk is, now.
   * - Assessment
     - **Undersized:** CPU busy, memory full or swapping, so the instance
       needs a bigger flavor or more instances. **Oversized:** CPU (even at
       its peaks, the 95th percentile) and memory both mostly unused, so a
       smaller flavor would free quota. **Right-sized:** neither.
       **No data:** the instance reported no metrics in the last 7 days.
   * - CPU, last 7 days
     - A sparkline of the CPU in use, against the flavor's vCPUs.

Click a row for CPU and memory charts against the flavor limits, a
suggestion, and links to the instance's metrics and to Resize.

*Real example:* ``grian-ih-cpu-2`` uses 3.5 of its 4 vCPUs (88%), so it is
**undersized (provisional, 54 h of data)**. ``grian-ih-disk`` uses 64% of
its vCPU and 9% of its RAM: right-sized, with *memory mostly unused*.

Instances needing attention
---------------------------

The same checks as the admin Instance Health panel, limited to your
project, over the last 10 minutes, with advice for each (at most 25 rows):

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Issue
     - What it means for you
   * - ERROR state
     - The instance failed. Delete it, or rebuild it with a different
       flavor.
   * - CPU wait
     - The instance waits for physical CPU on its host. **This comes from
       the host, not your workload.** If it lasts, tell your operator and
       give them the instance ID.
   * - Swap
     - The guest is out of memory. Resize to a flavor with more RAM.
   * - Disk latency
     - Disk writes are slow (only judged while the disk writes at least
       1 MiB/s). If the guest isn't doing heavy I/O, tell your operator.

*Real example:* ``grian-ih-cpu-2`` shows 13% CPU wait and ``grian-ih-disk``
10%: the host's cores are shared with busy instances in other projects.

Common tasks
------------

* **"I can't launch an instance, quota exceeded."** Look at *Holding quota
  without running* first, then at *Oversized* instances.
* **"Do I need a bigger quota?"** If the forecast says *Full in ~N days*
  and the rightsizing summary shows little waste, ask your operator for
  more, with this page as evidence.
* **"My application is slow."** Check *Instances needing attention*. CPU
  wait points at the host, *Undersized* points at the flavor.


Operator notes
==============

Requirements
------------

* The Ceilometer compute agent on every compute node, polling the default
  instance meters: ``cpu``, ``cpu.delay``, ``vcpus``, ``memory``,
  ``memory.usage``, ``memory.swap.in``, ``memory.swap.out``,
  ``disk.device.usage``, ``disk.device.capacity``,
  ``disk.device.write.bytes``, ``disk.device.write.requests`` and
  ``disk.device.write.latency``. A polling interval of at most 5 minutes
  (current values use a 10-minute window and rates need two samples).
* sg-core, Prometheus and Aetos (``service_type: metric-storage``), as for
  the other Telemetry panels. Project users need the ``reader`` role in the
  project (Aetos's ``telemetry:query`` policy).
* The Compute API, read with the user's token.

Enabling the panel
------------------

Copy ``src/grian_ui/local/enabled/_1101_project_telemetry_compute_panel.py``
to Horizon's ``local/enabled`` directory together with
``_1100_project_telemetry_panel_group.py`` (the Project > Telemetry group)
and ``_1000_admin_telemetry_panel_group.py``, which adds the ``grian_ui``
application for both groups. Then restart the web server. The panel is
shown to users who may list the project's servers
(``os_compute_api:servers:index``).

Configuration
-------------

All settings are optional and go in ``GRIAN_PLUGIN`` in ``local_settings``:

.. code-block:: python

   GRIAN_PLUGIN = {
       "datasource": "prometheus",
       # days of history Prometheus keeps (--storage.tsdb.retention.time)
       "metrics_retention_days": 15,
       "project_rightsizing_thresholds": {
           "cpu_high": 80,     # 7-day average CPU, % of vCPUs: undersized
           "cpu_low": 10,      # 7-day 95th percentile CPU: oversized if below
           "memory_high": 90,  # 7-day average memory, % of RAM: undersized
           "memory_low": 30,   # oversized if below (with low CPU)
       },
       "project_compute_thresholds": {
           "quota_warning": 75,       # % of a quota
           "quota_critical": 90,
           "idle_ram_critical": 25,   # % of the RAM quota held by idle instances
           "cpu_wait_warning": 5,     # % of vCPU time
           "cpu_wait_critical": 20,
           "swap_warning": 0,         # MiB/s, a warning above it
           "swap_critical": 10,
           "disk_latency_warning": 20,    # ms per write request
           "disk_latency_critical": 100,
           "disk_write_floor": 1,     # MiB/s a disk must write to be judged
       },
   }

Values are numbers of at least 0; every ``_warning`` value must not be
greater than its ``_critical`` value, and every ``_low`` value not greater
than its ``_high`` value. Invalid values are shown at the top of the page
and logged once.

``metrics_retention_days`` matters here: project users cannot read the
Prometheus retention from Aetos. The forecast looks back 30 days, or the
retention if it is shorter; the 30d chart range is disabled when the
retention is shorter than 30 days; with less than 7 days there is never a
forecast and the assessments stay provisional.

Data sources
------------

**Compute API** (``openstack_dashboard.api.nova``, with the user's token):

* the project's servers, every page
  (``servers.list(detailed=True, search_opts={"project_id": ...},
  limit=-1)``, as Project > Compute > Instances lists them);
* the flavors (``flavor_list``, then ``flavor_get`` for a private or
  deleted flavor that is not in the list);
* the absolute limits (``tenant_absolute_limits(reserved=True)``, the
  numbers of Project > Overview).

What holds quota starts from this list: instances in ERROR or SHUTOFF
report no metrics. The quota totals are Nova's counts; the bar segments add
up the server list (a note says when they differ, while instances are being
created or deleted).

**Prometheus through Aetos.** Every selector names its metric (Aetos
rejects ``{__name__=~...}`` for project users) and holds
``project="<current project>"``: Aetos only filters by project for users
without an admin role, so an administrator on the Project dashboard would
otherwise see every project. Only the polled samples are used
(``resource_name=~".+:instance-.+"``): Nova notifications publish
``vcpus`` and ``memory`` again with a plain name, which would count every
instance twice over a range. Each series is reduced to one value per
resource (``max by (resource)``) before summing.

.. list-table::
   :header-rows: 1
   :widths: 22 50 28

   * - Value
     - Query (per instance, or per disk)
     - Used for
   * - Current values
     - ``rate(ceilometer_cpu[10m]) / 1e9``,
       ``rate(ceilometer_cpu_delay[10m]) / 1e9``, ``ceilometer_vcpus``,
       ``ceilometer_memory``, ``ceilometer_memory_usage``, swap in + out
       rates, disk usage and capacity, write bytes and
       ``rate(write_latency) / rate(write_requests) / 1e6`` (ms): one query
     - symptoms, root disk, the instance list when Nova fails
   * - 7-day statistics
     - ``avg_over_time`` and ``quantile_over_time(0.95, ...)`` of the
       10-minute CPU rate ``[7d:10m]``, ``avg_over_time`` of the memory
       used, ``increase()`` of swap over 7 days, hourly points with data:
       one query
     - rightsizing
   * - Forecast
     - ``deriv()`` of the vCPUs, RAM and instance count held by running
       instances ``[Nd:1h]`` (N = 30 or the retention; the slope
       ``predict_linear`` uses), and the hours with data: one query
     - forecast column
   * - Chart
     - ``sum`` (or ``count``) of ``last_over_time(ceilometer_vcpus[10m])``
       or ``ceilometer_memory``, as a subquery: one per range and quota
     - quota usage over time
   * - Sparklines
     - CPU rate of the listed instances (at most 25) ``[7d:2h]``
     - rightsizing table
   * - Detail charts
     - CPU and memory of one instance ``[7d:1h]``
     - an opened rightsizing row

Aetos has no ``query_range``: series are instant queries with a subquery,
evaluated at the time the chart grid is built from.

Refresh and load
----------------

Each value is computed once and shared by the partials of a refresh and by
every member of the project, in the Horizon cache
(``grian_ui.cache.project_cache_key``: the project ID is in every key, so
two projects never share a value). Values computed for one project are only
shown to that project's members.

.. list-table::
   :header-rows: 1
   :widths: 40 20 40

   * - Value
     - Cached for
     - Calls
   * - Servers, flavors and limits
     - 55 seconds
     - 3 Compute API calls (plus one per unlisted flavor)
   * - Current values
     - 55 seconds
     - 1 query
   * - 7-day statistics
     - 10 minutes
     - 1 query
   * - Forecast
     - 30 minutes
     - 1 query
   * - Chart (per range and quota)
     - 1 minute (6h), 5 minutes (24h), 15 minutes (7d), 30 minutes (30d)
     - 1 query
   * - Sparklines
     - 10 minutes
     - 1 query
   * - Detail charts (per instance)
     - 5 minutes
     - 1 query

Resulting load per project while someone watches the page, whatever the
number of members and instances: about 3 Compute API calls and 2 Prometheus
queries per minute, plus one query per cache lifetime of each slower value.
The number of queries never depends on the number of instances; the browser
receives at most 3 quota rows, 10 idle holders, 25 rightsizing rows (with
84-point sparklines) and 25 attention rows. When a refresh fails, the last
good copy is shown with a warning; a copy older than its lifetime plus 30
seconds is flagged as possibly out of date.

If one source fails, the page shows what the other provides: without
metrics, the quota, the idle holders and the ERROR instances; without the
Compute API, the rightsizing table (names from the metrics, no flavor
names) and the symptoms of running instances.

Limits
------

* An instance that stopped running drops out of the metrics about 10
  minutes later; the Compute API list is up to 55 seconds old.
* History is limited by the Prometheus retention (15 days by default).
* The forecast assumes linear growth of what running instances hold; it
  does not know about planned launches or deletions.
* Shelved-offloaded instances are counted with their flavor's vCPUs and
  RAM, as Nova does by default (``[quota]count_usage_from_placement =
  False``; the DevStack used for the examples has no ``[quota]`` section,
  so the default applies). A cloud that counts usage from placement leaves
  them out of the vCPU and RAM counts: the totals (Nova's) are then
  correct, the bars longer, and a note under the quota explains the
  difference.
