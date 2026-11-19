=====================================
Testing the Top Consumers panel
=====================================

.. note::
    These are development instructions. See
    ``doc/source/admin-top-consumers.rst`` for the panel documentation.

Prerequisites
=============

1.  **Deploy the telemetry pipeline**

    The Ceilometer compute agent (polling), sg-core, Prometheus and Aetos
    (see the DevStack sample ``devstack/local.conf-all-in-one``). At least
    one running instance is needed for data.

2.  **Enable the panels**

    Copy the files under ``src/grian_ui/local/enabled`` to the
    ``local/enabled`` directory of your Horizon installation. The panel
    group file ``_1000_admin_telemetry_panel_group.py`` is required: it adds
    the ``grian_ui`` application.

3.  **Configure settings**

    Copy the settings file from ``src/grian_ui/local/local_settings.d`` to
    the ``local/local_settings.d`` directory of your Horizon installation.
    Optionally override the used ÷ allocated thresholds with
    ``GRIAN_PLUGIN['top_consumers_thresholds']``.

4.  **Fetch the vendored JavaScript**

    Run ``src/grian_ui/static/vendor/get_vendor.sh`` (Chart.js and htmx),
    then ``collectstatic``.

After completing these steps, restart your web server.

Checking the data
=================

Each running instance should have one polled ``ceilometer_cpu`` series::

    openstack metric query \
        'count by (project) (ceilometer_cpu{resource_name=~".+:instance-.+"})'

With the ``fake`` datasource (``GRIAN_PLUGIN = {"datasource": "fake"}``) the
panel shows an invented cloud of 12 projects with 186 instances, plus two
idle projects (``service`` and ``decommissioned``).
