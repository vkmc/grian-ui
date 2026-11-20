================
Instance metrics
================

**Project → Telemetry → Instance metrics** shows what one instance of the
current project is doing: CPU, CPU wait, memory, disk throughput and
latency, and network traffic, now and over a chosen time range. Horizon has
had no per-instance metrics for project users since its Ceilometer panel was
removed in 2016; this page fills that gap.

The page is not in the sidebar. Open it by clicking an instance name on the
Compute, Network or Usage page of the Telemetry group, or use the drop-down
next to the title to pick or switch instance.

Using the page
==============

Header
------

The instance's status, flavor (vCPUs, RAM and disk), ID, and networks with
their IP addresses. The buttons open Horizon's instance details, its
console (while it runs) and the Resize dialog.

The breadcrumb links back to the Compute page.

Time range and refresh
----------------------

The **1h**, **6h**, **24h** (default) and **7d** buttons set the range of
every chart. Ranges longer than the metric history the cloud keeps are not
offered. If the instance was created inside the selected range, a note says
when, and how many hours of data there can be: the charts start late, they
are not broken.

The page refreshes every 30 seconds while it is visible, and as soon as you
come back to the browser tab. Times are shown in your browser's time zone.

Tiles
-----

The latest values, always read from the last hour whatever the range:

**CPU**
    vCPUs busy, out of the flavor's vCPUs. Red from 80% of the flavor.
**CPU wait**
    Share of the vCPU time the guest wanted to run but the host had no free
    CPU. Amber from 5%, red from 20%.
**Memory**
    Memory in use, out of the flavor's RAM, and whether the guest swaps.
**Disk**
    Bytes written and read per second, all disks together.
**Network**
    Bytes received and sent per second, all interfaces together.

Charts
------

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Chart
     - How to read it
   * - CPU used
     - The dashed line is the flavor's vCPU count. A line pinned to it means
       the instance needs more vCPUs.
   * - CPU wait
     - Time the guest wanted CPU but the host had none. Dashed lines:
       warning 5%, critical 20%. Caused by the host (other instances on it),
       not by your workload: resizing does not help, ask your cloud operator.
   * - Memory used
     - Against the flavor's RAM (dashed), as the guest's balloon driver
       reports it. Close to the line, or any swap, means a bigger flavor is
       needed.
   * - Disk throughput
     - Read (blue) and write (magenta) across all disks.
   * - Disk latency per write
     - Milliseconds per write request. Shown only while the instance writes
       at least 1 MiB/s: with very few writes the value jumps around and
       means nothing. Gaps mean no (or too few) writes. Dashed lines:
       warning 20 ms, critical 100 ms.
   * - Network traffic
     - Inbound (blue) and outbound (magenta) across all interfaces. For
       drops, errors and neighbours, go to the Network page.

Hover a chart to read the values at one time. Gaps mean that no data was
received for that interval.

*Real example:* ``grian-ih-disk`` uses 0.64 of 1 vCPU, writes about
850 MiB/s (a ``dd`` loop) at about 2.7 ms per write, and waits for a host
CPU about 10% of the time.

Instances that are not running
------------------------------

An instance that is not running has no metrics: the page says why instead
of showing empty charts.

* **ERROR**: the fault reported by the Compute service, and what the
  instance still holds of your quota (vCPUs and RAM). Delete it, or rebuild
  it, to release the quota.
* **SHUTOFF** (also suspended or shelved): metrics stop while an instance is
  stopped; the history from when it ran is kept for the retention period.
* Other states (building, paused, migrating, ...): metrics are shown again
  once it is active.

The Compute page lists these instances under "Holding quota without
running".

When something is missing
-------------------------

**"This instance does not exist, or it is not in the current project"**
    The instance was deleted, the ID is wrong, or it belongs to another
    project. Switch to that project first: the page only shows instances of
    the current project, also for administrators.
**"Invalid instance ID"**
    The address does not hold an instance UUID. Pick the instance in the
    list.
**"No metrics for this instance in the last 1h"**
    A new instance reports after its first polling interval. If it stays
    empty, the Ceilometer compute agent may not be polling its host: tell
    your cloud operator.
**"Still loading telemetry data…"**
    Another request is computing the same data; the section retries in a
    few seconds.
**"Could not be refreshed: showing the last data received"**
    The telemetry service did not answer; the last data is shown, with its
    time, until the next refresh works.

"How this page is calculated", at the bottom of the page, lists the
metrics, queries and thresholds the page uses.

Operator notes
==============

Requirements
------------

* The Ceilometer compute agent polling the instances, with sg-core,
  Prometheus and Aetos, as for the Admin Telemetry panels. Project users
  need the ``reader`` role in their project (Aetos policy
  ``telemetry:query``).
* The Compute API, called with the user's token through
  ``openstack_dashboard.api.nova`` like the rest of Horizon.

Enabling the panel
------------------

Copy ``_1105_project_instance_metrics_panel.py`` from
``src/grian_ui/local/enabled`` to Horizon's ``local/enabled`` directory,
with ``_1000_admin_telemetry_panel_group.py`` (it adds the ``grian_ui``
application; Horizon does not de-duplicate ``ADD_INSTALLED_APPS``, so no
other file adds it) and ``_1100_project_telemetry_panel_group.py`` (the
Project → Telemetry group).

The panel has ``nav = False``: Horizon leaves it out of the sidebar but
registers its URLs. Other panels link to it with::

    reverse("horizon:project:instance_metrics:index") + "?instance=<server uuid>"

and show a plain name when the URL does not resolve (panel not installed).

Configuration
-------------

All settings are optional, under ``GRIAN_PLUGIN`` in ``local_settings``:

``project_instance_metrics_thresholds``
    Per-key overrides of the thresholds (defaults shown)::

        GRIAN_PLUGIN = {
            "project_instance_metrics_thresholds": {
                "cpu_busy": 80,                 # CPU tile red, % of vCPUs
                "cpu_wait_warning": 5,          # % of vCPU time
                "cpu_wait_critical": 20,
                "disk_latency_warning": 20,     # ms per write
                "disk_latency_critical": 100,
                "latency_min_write_mib": 1,     # MiB/s written to show latency
            },
        }

    Values must be numbers of at least 0, warnings not above criticals.
``project_instance_metrics_rate_window``
    Shortest window of the ``rate()`` and ``last_over_time()`` calls,
    ``"10m"`` by default (minutes or hours, up to ``"6h"``). Use at least
    twice the Ceilometer polling interval (300 s by default, so ``"10m"``);
    with shorter windows the charts have gaps. Ranges whose step is longer
    use the step.
``metrics_retention_days``
    Shared setting: how many days Prometheus keeps (15 by default). Ranges
    longer than that are not offered, and the shut-off explanation quotes
    it. Project users cannot read the retention from Aetos.

Invalid settings are listed at the top of the page.

Metrics and queries
-------------------

Every selector names its metric (Aetos rejects ``{__name__=~...}`` for
project users) and carries ``project="<current project>"``: Aetos only adds
the project for users without an admin role, so an administrator browsing
the Project dashboard would otherwise see other projects' series.
Selectors also pick this instance's series:

* instance meters: ``resource="<uuid>"`` and
  ``resource_name=~".+:instance-.+"``, the polled samples (Nova
  notifications publish the same meters with a plain name and would count
  twice over a range);
* disk meters: ``resource=~"<uuid>-.+"`` (one series per device) and the
  same ``resource_name`` filter;
* network meters: ``resource=~".+-<uuid>-.+"`` (one series per vNIC). They
  are polled only, and their ``resource_name`` is ``<name>:<vNIC>``, so the
  ``:instance-`` filter would drop them.

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Series
     - Query (``{…}`` as above, ``w`` the rate window)
   * - CPU used
     - ``sum(rate(ceilometer_cpu{…}[w])) / 1e9`` (vCPUs)
   * - CPU wait
     - ``sum(rate(ceilometer_cpu_delay{…}[w])) / 1e9``, divided by the vCPUs
   * - vCPUs
     - ``max(last_over_time(ceilometer_vcpus{…}[w]))`` (follows a resize;
       the flavor from the Compute API when missing)
   * - Memory
     - ``max(last_over_time(ceilometer_memory_usage{…}[w]))`` and
       ``ceilometer_memory`` (flavor RAM), MiB
   * - Swap
     - ``rate()`` of ``ceilometer_memory_swap_in`` plus
       ``ceilometer_memory_swap_out``, MiB/s
   * - Disk
     - ``sum(rate(ceilometer_disk_device_{read,write}_bytes{…}[w]))``
   * - Write latency
     - ``sum(rate(ceilometer_disk_device_write_latency{…}[w])) /
       sum(rate(ceilometer_disk_device_write_requests{…}[w])) / 1e6``
       (the latency meter is in ns; ms per write)
   * - Network
     - ``sum(rate(ceilometer_network_{incoming,outgoing}_bytes{…}[w]))``

Aetos has no ``query_range`` endpoint: each group of charts (CPU, memory,
disk, network) is one instant query with a subquery, its series tagged
with ``label_replace(…, "series", …)`` and joined with ``or``:

======  ======  ===========================
Range   Step    Rate window (default)
======  ======  ===========================
1h      1m      10m
6h      5m      10m
24h     15m     15m
7d      1h      1h
======  ======  ===========================

Calls and load
--------------

Per open page, every 30 seconds while the page is visible:

* Compute API: one ``GET /servers/<id>`` (plus one flavor ``GET`` before
  microversion 2.47), shared by the four sections and by the members of the
  project for 25 seconds; one server list for the picker, when the page is
  loaded, shared for 60 seconds.
* Aetos: one query per chart group and range (four), plus four over the
  last hour for the tiles when another range is shown. Results are shared
  per project and instance for 25 seconds (1h), 55 seconds (6h), 4 minutes
  (24h) or 10 minutes (7d). At most 168 points per series.

Values are cached with ``grian_ui.cache.project_cache_key``: the key holds
the project, so projects never share data. Shut-off and ERROR instances
cost no Aetos query.

Security
--------

* ``?instance=`` must be a lowercase server UUID before it reaches the
  Compute API or a query; anything else is refused.
* Every section checks that the Compute API returns the instance and that
  it belongs to the current project. The Compute API returns other
  projects' instances to administrators whatever project their token is
  scoped to, so this check is what keeps them to the current project.
* User-controlled names (instance names) reach the charts' script through
  Django's ``json_script`` only, never inline.

Limits
------

* Only active instances have metrics. A stopped instance's earlier history
  is kept by Prometheus but not shown.
* Disks and interfaces are summed; per-device or per-network detail, and
  packet drops and errors, are on other pages.
* Latency is per write request, averaged over the rate window, and hidden
  below the write floor.
* The picker lists at most 500 instances, by name.
