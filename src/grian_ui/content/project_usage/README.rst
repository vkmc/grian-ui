=============================
Testing the Usage panel
=============================

.. note::
    These are development instructions. See
    ``doc/source/project-telemetry-usage.rst`` for the panel documentation.

Prerequisites
=============

1.  **Deploy the telemetry pipeline**

    The Ceilometer compute agent (polling), sg-core, Prometheus and Aetos
    (see the DevStack sample ``devstack/local.conf-all-in-one``). At least
    one running instance in the project is needed for data; a volume or
    snapshot fills the Volume GB-hours card.

2.  **Enable the panel**

    Copy ``_1104_project_telemetry_usage_panel.py`` from
    ``src/grian_ui/local/enabled`` to the ``local/enabled`` directory of
    your Horizon installation, with the panel group files
    ``_1000_admin_telemetry_panel_group.py`` (adds the ``grian_ui``
    application) and ``_1100_project_telemetry_panel_group.py`` (the
    Project > Telemetry group).

3.  **Configure settings**

    Copy the settings file from ``src/grian_ui/local/local_settings.d`` to
    the ``local/local_settings.d`` directory of your Horizon installation.
    Set ``GRIAN_PLUGIN['metrics_retention_days']`` to the Prometheus
    retention (DevStack keeps the default, 15 days). Optionally set
    ``GRIAN_PLUGIN['project_usage_thresholds']``.

Checking the panel
==================

* Log in as a project user (``demo``/``demo`` on DevStack) and open
  **Project > Telemetry > Usage**. The current month is selected; earlier
  months are locked once they are outside the retention.
* Compare the table with the queries run as that user, for example::

    source ~/devstack/openrc demo demo
    openstack metric query 'count_over_time((max by (resource)
      (ceilometer_vcpus{project="<id>",resource_name=~".+:instance-.+"}))[1d:5m])'

  (5-minute steps: divide by 12 for hours). Every query of the panel is in
  ``grian_ui.datasource.prometheus_project_usage``.
* Log in as an administrator and switch to another project: the page shows
  that project only (the queries hold ``project="<id>"``, the cache key
  holds the project ID).
* Without a cloud, ``GRIAN_PLUGIN = {"datasource": "fake"}`` serves the
  sample project of ``grian_ui.datasource.fake_project_usage``.

Unit tests
==========

::

    DJANGO_SETTINGS_MODULE=grian_ui_tests.unit.settings tox -e py3

The panel's tests are ``tests/grian_ui_tests/unit/test_project_usage*.py``
and ``test_datasource_*_project_usage.py``.
