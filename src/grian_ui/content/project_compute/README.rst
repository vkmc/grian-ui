======================
Project Compute panel
======================

**Project > Telemetry > Compute**: the current project's compute quota, what
holds it, and the health and sizing of its instances. See
``doc/source/project-telemetry-compute.rst`` for the user guide and the
operator notes.

Sections (each an HTMX partial, refreshed every 60 seconds):

* summary cards: highest quota use, instances running, RAM held by idle
  instances, instances to resize; notices when the metrics are unavailable
  or cover less than 7 days
* compute quota: one bar per quota (instances, vCPUs, RAM) split into
  running, shut-off and errored instances, with a forecast of when it is
  full
* quota usage over time: what running instances held (6h to 30d) against
  Nova's count and the limit
* holding quota without running: ERROR, SHUTOFF and shelved instances
  (offloaded ones included, as Nova counts them by default), largest first
* allocated vs used (rightsizing): 7-day CPU and memory against the flavor,
  undersized / oversized / right-sized, with per-instance charts
* instances needing attention: ERROR state, CPU wait, swap, disk latency

Files:

* ``views.py``: the index and the partials; cached data through
  ``grian_ui.cache.project_cache_key`` (never ``cache_key``)
* ``grian_ui/project_compute.py``: the logic (quota rows, forecast,
  assessments, symptoms)
* ``grian_ui/project_compute_inventory.py``: servers, flavors and limits
  from the Compute API (``openstack_dashboard.api.nova``, user's token)
* ``grian_ui/datasource/prometheus_project_compute.py``: the PromQL; every
  selector names its metric and holds ``project_matcher()``
* ``grian_ui/datasource/fake_project_compute.py``: sample data (an invented
  "web-shop" project) for the ``fake`` datasource and the unit tests
* ``templates/project/telemetry_compute/``: ``index.html`` and the partials

Settings: ``GRIAN_PLUGIN['project_compute_thresholds']``,
``GRIAN_PLUGIN['project_rightsizing_thresholds']`` and
``GRIAN_PLUGIN['metrics_retention_days']``.

Instance names link to the Instance metrics page
(``horizon:project:instance_metrics:index?instance=<uuid>``) when it is
installed, and are plain text otherwise.
