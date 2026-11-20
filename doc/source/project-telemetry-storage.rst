=========================
Project Storage Dashboard
=========================

**Project > Telemetry > Storage** shows your project's volume quota, how
your storage grows, and what could be deleted. It only shows the current
project: other projects and the cloud's own storage are never visible here.

The page refreshes every 5 minutes. Each section loads on its own, so a slow
or failing service only affects its own section.


User guide
==========

Time range
----------

The 7d / 30d / 90d buttons choose the range of the footprint chart. Storage
changes slowly, so ranges are in days, with one point per day. The rest of
the page always shows the current state. Dates and times are shown in your
browser's time zone.

Summary cards
-------------

* **Volume gigabytes quota**: the share of the gigabytes quota used, by
  volumes and snapshots together. Amber from 75%, red from 90%.
* **Volumes** and **Volume snapshots**: how many you have, against their
  quotas, with the number of unattached volumes and the snapshots' size.
* **Could be freed**: the total size of the clean-up candidates that count
  against your quota (*review* and *consider*; images and *keep?* snapshots
  are not counted).

Storage quota
-------------

One bar per quota:

* **Gigabytes**, split into attached volumes (blue), unattached volumes
  (grey; volumes in an error state count here too) and snapshots
  (magenta). Snapshots count against the same gigabytes quota as volumes,
  which is easy to forget.
* **Volumes**, **Snapshots** and **Backups**: counts. *Backup service not
  enabled* means this cloud has no volume backup service.

The column on the right is a **forecast**: at the growth rate of the last 30
days (or of the history the cloud keeps, if shorter), when this quota will
be full.

* *Full in ~N days*: red within 14 days, amber within 30.
* *Not growing*: usage is flat or falling.
* *Not enough history*: less than 3 days of metrics, for example on a new
  project.
* *Full*: the quota is used up now.

The bars count what the Block Storage API counts now; the forecast only
takes the growth rate from the metrics.

Storage footprint over time
---------------------------

Stacked areas: volumes, then snapshots on top. The top edge is the total
counted by the gigabytes quota; the dashed line is the quota limit. Private
images are listed separately: they do not use the volume quota.

Metric history is limited. When the cloud keeps fewer days than the chosen
range (15 days by default), the start of the chart is empty and a note says
"This cloud keeps N days of metrics: the chart starts on ...". A project
whose storage is younger than the range shows "History starts on ...".
Without volumes, snapshots or private images the chart is replaced by a
short explanation.

Clean-up candidates
-------------------

Suggestions, most likely and largest first, at most 10:

.. list-table::
   :header-rows: 1
   :widths: 22 48 30

   * - Candidate
     - Rule
     - Tag
   * - Unattached volume
     - Status *available* for 7 days or more
     - *review* from 30 days, *consider* before
   * - Old snapshot
     - 30 days or older, and a newer snapshot of the same volume exists
     - *consider*
   * - Newest or only snapshot of a volume
     - 30 days or older, but the newest (or only) copy
     - *keep?*, shown so you decide knowingly
   * - Unused private image
     - No instance in the project uses it, 30 days or older; never a
       shared image (other projects may use it)
     - *consider* (images don't use volume quota: *not in quota*)

**Check before deleting.** "Unattached for" uses the volume's last update
time: Cinder doesn't record detach times, so the real idle time can be
longer. An instance uses an image when it booted from it, or from a volume
created from it.

Volumes and snapshots, Private images
-------------------------------------

Inventories, largest first, at most 25 rows each: size, status, what each
volume is attached to, and age in days. Names link to the volume and
snapshot pages, and instance names to the Instance metrics page, when those
panels are installed. Private images are the images your project owns that
are not public; public images shared by the cloud are not listed. *Used by*
says how many of your instances use the image (*none*: a clean-up
candidate once it is 30 days old). Images you share with other projects
also say *shared, use by other projects unknown*: their instances are not
visible to your project, so these images are never clean-up candidates.
*unknown* means the Compute or Block Storage API could not be read
(instances booted from a volume use the image of their volume); shared
images keep their *shared* note then.

When something cannot be loaded
-------------------------------

* A section that cannot load its data says so ("Error loading ..."); the
  details are in the Horizon log. It tries again at the next refresh.
* When a service fails for part of a section, the rest still shows, with a
  note: for example, instance IDs instead of names when the Compute API
  fails, or no image candidates when the Image API fails.
* After a failed refresh, the last data received stays on the page with
  "Could not be refreshed" and the time it was received.

*Real data:* the DevStack ``demo`` project has no volumes, snapshots or
private images, so it shows the empty states. The ``fake`` datasource shows
the sample project of the mockup.


Operator notes
==============

Requirements
------------

* The Block Storage, Compute and Image APIs, called with the user's token
  through Horizon's API layer. The panel needs the same service and policy
  as Horizon's **Project > Volumes** panel (``volume:get_all``).
* Ceilometer reporting ``volume.size`` and ``volume.snapshot.size`` (central
  agent pollsters, in the default ``polling.yaml``), sg-core, Prometheus and
  Aetos, as for the other Telemetry panels. Without them, only the footprint
  chart and the forecasts are missing.

Enabling the panel
------------------

Copy ``src/grian_ui/local/enabled/_1102_project_telemetry_storage_panel.py``
to Horizon's ``local/enabled`` directory together with the panel group files
``_1000_admin_telemetry_panel_group.py`` (it adds the ``grian_ui``
application) and ``_1100_project_telemetry_panel_group.py`` (the
**Project > Telemetry** group), then restart the web server.

Configuration
-------------

``GRIAN_PLUGIN['metrics_retention_days']`` (default 15) must match the
Prometheus retention (``--storage.tsdb.retention.time``). Project users
cannot read the retention from Aetos, so the panel never queries further
back than this setting, and says so on the 30d and 90d ranges when they are
longer. The forecasts use the last 30 days, or the retention if shorter.

The thresholds can be overridden per key:

.. code-block:: python

   GRIAN_PLUGIN = {
       "datasource": "prometheus",
       "metrics_retention_days": 15,
       "project_storage_thresholds": {
           "quota_warning": 75,  # percent of a quota (amber)
           "quota_critical": 90,  # percent of a quota (red)
           "unattached_days": 7,  # unattached volume: "consider"
           "unattached_review_days": 30,  # unattached volume: "review"
           "snapshot_age_days": 30,  # old snapshot
           "image_age_days": 30,  # unused private image
           "forecast_critical_days": 14,  # forecast in red
           "forecast_warning_days": 30,  # forecast in amber
       },
   }

Values are numbers of at least 0. ``quota_warning`` must not be greater than
``quota_critical``, ``unattached_days`` not greater than
``unattached_review_days``, and ``forecast_critical_days`` not greater than
``forecast_warning_days``. An invalid value is shown at the top of the page
and logged once.

*Backup service not enabled* follows Horizon's
``OPENSTACK_CINDER_FEATURES['enable_backup']``, like Horizon's own volume
backup pages: Cinder does not report whether its backup service runs.

Metrics and queries
-------------------

Every selector carries ``project="<current project ID>"``: Aetos adds the
project filter only for users without an admin role, so an administrator
browsing the Project dashboard would otherwise see every project. Every
metric is named, as Aetos rejects ``{__name__=~...}`` selectors for project
users. Aetos has no range query endpoint: histories are subqueries.

.. list-table::
   :header-rows: 1
   :widths: 20 55 25

   * - Item
     - Query (``{p}`` is the project matcher)
     - When
   * - Footprint
     - ``(sum(max by (resource) (last_over_time(ceilometer_volume_size{p}[10m]))))[Nd:1d]`` and
       the same for ``ceilometer_volume_snapshot_size``, N = the range, at
       most the retention
     - per range, every 5 minutes
   * - Forecast history
     - the same with ``sum`` (GB) and ``count`` (number), ``[Nd:6h]``,
       N = 30 or the retention
     - every 15 minutes

``last_over_time(...[10m])`` takes the last sample of each volume before
each point, so a deleted volume stops counting within 10 minutes; keep the
Ceilometer polling interval of these meters below 10 minutes. ``max by
(resource)`` keeps one series per volume or snapshot ID, so a volume whose
labels changed in the window (a rename changes ``resource_name``) counts
once. Ceilometer
reports ``image.size`` too, but also for the public images a project owns,
so image sizes come from the Image API instead.

APIs and load
-------------

Per project, whatever the number of members, partials and refreshes:

* Block Storage API: absolute limits, the volume list and the snapshot list
  (detailed; every page), at most every 60 seconds;
* Compute API: the instance list (attachment names, image use), at most
  every 60 seconds, and only when needed;
* Image API: the images the project owns (``owner=<project>``), at most
  every 60 seconds;
* Aetos: two footprint queries per range every 5 minutes and four forecast
  queries every 15 minutes.

Every value is cached in the Horizon cache with a per-project key
(``grian_ui.cache.project_cache_key``), so projects never share data. A new
or deleted volume shows up within a minute plus the page refresh. Every
section renders a bounded amount of data: four cards, four quota rows, at
most 10 clean-up candidates, 25 volumes and snapshots, 25 images and one
chart point per day.

Limits
------

* Cinder does not record detach times; "unattached for" is the time since
  the volume's last update.
* The forecast is a straight line through the history; it does not know
  about planned growth or deletions.
* Your project cannot see the instances of other projects: whether they
  use an image you share with them is unknown, so shared images are never
  clean-up candidates, even when no other project uses them.
