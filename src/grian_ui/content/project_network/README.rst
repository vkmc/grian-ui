Project Network panel
=====================

**Project > Telemetry > Network**
(``_1103_project_telemetry_network_panel.py``):
whether the project's instances' networking works and, if it doesn't, whether
to look at the project's own setup or call the operator.

Sections (each an htmx partial, refreshed every 30 s):

- **Verdict and summary cards** (``summary/``): every instance behind one
  router gone quiet (probably the cloud), some instances gone quiet (check
  them), resources not active, drops or errors, or no problem.
- **By router and network** (``topology/``): each router followed by its
  networks, with quiet interfaces, traffic now vs usual and drops; other
  projects' ports on the project's networks are counted, not judged.
- **Project traffic** (``traffic/``): inbound and outbound bytes/s over 1,
  6 or 24 hours.
- **Network quota** (``quota/``) and **Floating IPs** (``floating-ips/``).
- **Instance interfaces** (``interfaces/``): one row per port of a running
  instance, problems first, at most 25, with a sparkline; instance names
  link to the instance metrics page when it is installed.

Code:

- ``grian_ui.project_network``: the evaluation (pure functions).
- ``grian_ui.project_network_inventory``: Networking and Compute API data,
  read with the user's token.
- ``grian_ui.datasource.prometheus_project_network``: the queries, each
  with the project matcher and named metrics; ``fake_project_network``: the
  sample data of the ``fake`` datasource.
- ``views.py``: the partials; every cached value uses
  ``grian_ui.cache.project_cache_key``.

Settings: ``GRIAN_PLUGIN['project_network_thresholds']``. See
``doc/source/project-telemetry_network.rst``.
