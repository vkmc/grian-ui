=====================
Using Storage Health
=====================

**Is block storage healthy, and when does it run out?** Storage Health
(formerly Volume Metrics) is under **Admin > Telemetry > Storage Health**
and shows every project. Its address is still ``/admin/volume_metrics/``,
so existing bookmarks keep working. For requirements, queries and
configuration, see :doc:`admin-volume-metrics`.


Reading the page
================

* The page refreshes every 60 seconds, and as soon as the browser tab
  becomes visible again. The metrics are new at every refresh; the
  volumes and snapshots from the Block Storage API are refreshed every 2
  minutes and the forecasts every 5 minutes. Each section says when its
  data was fetched, in your browser's time zone.
* The **range buttons** (24h, 7d, 14d) only change the capacity chart.
  Problems always use the last 10 minutes (the staleness window, which the
  operator can change).
* When a source cannot be read, the page keeps showing the last good data
  with its age ("Could not be refreshed: showing the last data received,
  from 4 min ago") and retries at the next refresh. When the Block Storage
  API fails, the verdict and cards still use the metrics and say that
  volumes were not checked.
* Colours always come with an icon and a word: green is fine, orange worth
  a look, red act now, blue information.
* **Block Storage API** tags mark data read from Cinder; the rest is
  Ceilometer telemetry. Green badges say how many rows a table shows at
  most.
* **How this page is calculated**, at the bottom, lists the queries and
  thresholds in use.


Verdict
=======

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Verdict
     - What to do
   * - Storage is healthy
     - Nothing. While the pools have less than 7 days of history, it says
       so: forecasts are not available yet.
   * - Cinder service down: cinder-volume on *host@backend*
     - Volume operations on that backend fail or get stuck and its pools
       stop reporting; the verdict says how many. Check
       ``openstack volume service list`` and the service's log, and restart
       it. The attention list shows which projects are affected.
   * - *N* pools nearly full
     - Add capacity or move volumes away. When the virtual use is the
       limit (thin provisioning), raise the over-subscription ratio only if
       the physical space allows it.
   * - *N* pools stopped reporting
     - Their numbers are frozen at the last report. Usually their
       ``cinder-volume`` service is down, and volume operations on that
       backend fail: check the volume services.
   * - *N* volumes or snapshots in an error state
     - See the attention list below.
   * - *N* pools filling up
     - Plan capacity using the forecasts.
   * - *N* volume operations stuck
     - Usually a backend or service problem; check the volume service of
       the backend shown in the attention list.


Summary cards
=============

Total storage (GiB, with the number of pools and stale pools), allocated
storage (physical use, and use of the virtual limit), volumes (how many
need attention) and snapshots (clean-up candidates). The allocated card
turns orange or red with the thresholds, the volumes card with the
volumes and snapshots in error (red) or stuck (orange).


Volumes and snapshots needing attention
=======================================

The worst 25 rows from the Block Storage API, all projects (the footer
gives the exact counts):

* **Critical**: an error state (``error``, ``error_deleting``,
  ``error_restoring``, ``error_extending``). Read the ``cinder-volume``
  log; reset the state and retry, or delete.
* **Warning**: ``creating``, ``attaching``, ``detaching`` or ``deleting``
  for more than 30 minutes. Usually a backend or service problem.
* **Clean-up**: volumes unattached for 30 days or more, and snapshots
  older than 90 days with a newer snapshot of the same volume. Ask the
  owning project.

The footer lists unattached volumes that are not old enough for the
clean-up rule yet. Click a name to open the volume or snapshot in
**Admin > Volume**.


Capacity over time
==================

*Whole cloud*: allocated, provisioned and total capacity over the range.
Provisioned above total is normal with thin provisioning. *Fullest pools*:
physical use of the 5 fullest pools, with the 75% and 90% lines.


Pools (top 10, plus look-up)
============================

One row per pool; this table replaces the old Storage Pools, Capacity
Forecast and Detailed Pool Information panels. Sort it by *Soonest full*,
*Fullest* or *Stale first*, and use the look-up box (with suggestions) for
any pool outside the top 10.

* **Physical** use is space really used (allocated ÷ total). **Virtual**
  use is what thin provisioning promised, against the over-subscription
  limit (total × ``max_over_subscription_ratio``). On thin pools the
  virtual limit usually runs out first: free space stays equal to the
  total although volumes are provisioned.
* The **forecast** says when free *or* virtual free capacity reaches zero
  at the 7-day trend, whichever comes first, and which one ("Full in 24
  days (virtual)"). It needs 7 days of history.
* A pool without a report for 10 minutes (configurable) is **Stale**:
  its numbers are frozen at the last report, and *Last report* turns red.


Cinder services, backends, projects, images
===========================================

* **Cinder services**: each scheduler and volume service with a 24-hour
  strip (one mark per 15 minutes: green up, red down, grey no report) and
  the share of time up.
* **Backends**: pools summed per backend, with over-subscription
  (provisioned ÷ total). A backend whose volume service is down is flagged.
* **Usage by project** (top 10): volumes and snapshots per project. Click a
  project to open its volumes in **Admin > Volume > Volumes**, filtered by
  that project.
* **Images and backups**: image count and store size, images downloaded in
  24 hours, and volume backups ("not deployed" without the
  ``cinder-backup`` service, "unknown" when the service list cannot be
  loaded).


Common tasks
============

* **A pool says "Full in N days (virtual)"**: thin provisioning is close
  to its promise limit. Raise the over-subscription ratio only if the
  physical space allows it, or add capacity.
* **A pool is stale**: check ``openstack volume service list``; restart
  or repair the ``cinder-volume`` service of that backend.
* **A volume service is down**: its pools turn stale and volume
  operations on that backend fail; the attention list shows which projects
  are affected.
* **Clean-up before buying capacity**: work through the clean-up rows of
  the attention list with the owning projects, and check *Usage by project*
  for the largest consumers.
