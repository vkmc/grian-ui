=======================
Top Consumers Dashboard
=======================

The Top Consumers dashboard shows which projects and instances use the most
CPU, memory, disk I/O and network across the cloud. It is accessible under
**Admin > Telemetry > Top Consumers** and requires the
``openstack.roles.admin`` permission.

All values come from the Ceilometer compute agent (polled instance meters),
stored in Prometheus by `sg-core <https://github.com/infrawatch/sg-core>`_
and queried through `Aetos <https://opendev.org/openstack/aetos>`_
(``service_type: metric-storage``) with the administrator's token, which
sees every project. Project names are read from the Identity service.

For how to read the page, see :doc:`user-top-consumers`.


Requirements
============

* The Ceilometer compute agent on every compute node, polling the
  ``cpu``, ``memory.usage``, ``disk.device.read.bytes``,
  ``disk.device.write.bytes``, ``network.incoming.bytes``,
  ``network.outgoing.bytes``, ``vcpus`` and ``memory`` meters (all in the
  default ``polling.yaml``).
* sg-core, Prometheus and Aetos, as for the other Telemetry panels.
* A polling interval shorter than the current-value window (10 minutes, see
  below). The Ceilometer default (300-600 seconds) works; rates need at
  least two samples in the window.


Enabling the panel
==================

Copy ``src/grian_ui/local/enabled/_1004_admin_top_consumers_panel.py`` with
the panel group file ``_1000_admin_telemetry_panel_group.py`` to Horizon's
``local/enabled`` directory, then restart the web server. To disable only
this panel, remove its file (not the panel group file).


Configuration
=============

The thresholds of the *Used ÷ allocated* column can be overridden in
``local_settings``:

.. code-block:: python

   GRIAN_PLUGIN = {
       "datasource": "prometheus",
       "top_consumers_thresholds": {
           "usage_idle": 10,  # below: "mostly idle" (percent)
           "usage_busy": 80,  # at or above: "busy" (percent)
       },
   }

Values are percentages of at least 0; ``usage_idle`` must not be greater
than ``usage_busy``. An invalid value is shown at the top of the page and
logged once.

The labels follow the value as shown, rounded to a whole percent: with the
defaults, a ratio shown as "80%" (79.5% or more) is busy, and one shown as
"10%" (9.5% or more) is not mostly idle.


Data and queries
================

Every value is summed per project (or per instance) after reducing each
series to one value per resource (instance, disk or vNIC), so duplicated
series from several notification agents or sg-core replicas are not counted
twice.

.. list-table::
   :header-rows: 1
   :widths: 18 42 40

   * - Resource
     - In use
     - Allocated
   * - CPU (cores)
     - ``rate(ceilometer_cpu[10m]) / 1e9``
     - ``ceilometer_vcpus`` (vCPUs of the running instances)
   * - Memory (MiB)
     - ``last_over_time(ceilometer_memory_usage[10m])``
     - ``ceilometer_memory`` (flavor RAM)
   * - Disk I/O (bytes/s)
     - ``rate(ceilometer_disk_device_read_bytes[10m])`` +
       ``rate(ceilometer_disk_device_write_bytes[10m])``
     - none
   * - Network (bytes/s)
     - ``rate(ceilometer_network_incoming_bytes[10m])`` +
       ``rate(ceilometer_network_outgoing_bytes[10m])``
     - none

**Polled samples only.** Nova notifications publish ``cpu``, ``vcpus``,
``memory`` and the disk sizes with a plain ``resource_name``, next to the
polled samples (``resource_name="<name>:instance-<uuid>"``). The instance
meters are filtered with ``resource_name=~".+:instance-.+"`` so instances are
not counted twice. Network samples are only polled; their
``resource_name`` is ``<name>:<vNIC>``.

**Instances.** Disks (``resource="<uuid>-vda"``) and vNICs
(``resource="<libvirt name>-<uuid>-<vNIC>"``) are attributed to their
instance by extracting the UUID from ``resource``. Instance names come from
``resource_name``; no Compute API call is made.

**"Allocated"** only counts running instances: Ceilometer has no hypervisor
capacity, so shares are of what is in use, not of the cloud's size. The
Placement API would be needed for that.

Queries per refresh (every 60 seconds), whatever the size of the cloud:

* one query for the per-project snapshot (every field of every project,
  shared by the summary, the table and the chart);
* two queries for a list of top instances: ``topk(10, ...)`` over the
  series whose ``resource`` label holds an instance UUID, then the names
  and allocation of those 10 instances;
* one subquery for the chart, and one for the CPU sparklines of the listed
  projects, over the selected range (see below).

The snapshot and the instance lists are cached for 55 seconds and the
trends per range (see below), in the Horizon cache shared by every
administrator of a region (``grian_ui.cache``). When a refresh fails, the
last good copy is shown with a warning; a copy older than its lifetime plus
30 seconds (still being refreshed) is flagged as possibly out of date.

Chart ranges
------------

The **1h / 6h / 24h / 7d** buttons (default 24h) choose the range of the
chart and of the CPU sparklines. Current values always use the last 10
minutes.

.. list-table::
   :header-rows: 1
   :widths: 15 20 25 40

   * - Range
     - Points
     - Rate window
     - Cached for
   * - 1h
     - 60 (every 1m)
     - 10m
     - 55 seconds
   * - 6h
     - 72 (every 5m)
     - 10m
     - 60 seconds
   * - 24h
     - 96 (every 15m)
     - 15m
     - 5 minutes
   * - 7d
     - 168 (every 1h)
     - 1h
     - 15 minutes

Rates are taken over the step, but never over less than 10 minutes, so each
point holds at least two polls of every meter.

Calls to other services
-----------------------

The only call outside Prometheus is the Identity service (Keystone) project
list, for the project names (``keystone.tenant_list``, all projects). It is
a separate value shared by every administrator of a region and refreshed at
most every **5 minutes**, whatever the number of administrators, partials
and refreshes; the page refresh only reads it from the cache. A Keystone
failure is cached like a result, so Keystone is not called more often while
it fails: the last names received (kept for a day) are shown, or the
project IDs, with a note. The table footer says when the names were read
("Project names from 10:42 (refreshed every 5 minutes)") and flags an old
copy. Instance names come from the metrics, and the links to the instance
pages are built locally: no Compute API call is made. Each Horizon process
joins the metrics with the names once per pair of inputs.

Resulting load per region while someone watches the page, whatever the
number of administrators: one snapshot query per minute; two queries per
minute for each top instance list on display; one chart and one sparkline
subquery per cache lifetime of the selected range (55 seconds to 15
minutes); and at most 12 Keystone calls per hour.


Limits
======

* Projects with nothing running are not in the metrics. They are listed by
  name under the table (at most 10 names) from the Identity service.
* An instance that stopped running drops out about 10 minutes later.
* The chart covers at most 7 days; history is limited by the Prometheus
  retention.
* Disk I/O and network have no allocation, so they have no *Used ÷
  allocated* column.
