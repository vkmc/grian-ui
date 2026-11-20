==========================================
Testing the project Instance metrics panel
==========================================

.. note::
    These are development instructions. See
    ``doc/source/project-instance-metrics.rst`` for the panel documentation.

Prerequisites
=============

1.  **Deploy the telemetry pipeline**

    The Ceilometer compute agent (polling), sg-core, Prometheus and Aetos
    (see the DevStack sample ``devstack/local.conf-all-in-one``). At least
    one running instance in the project is needed for data.

2.  **Enable the panels**

    Copy ``_1000_admin_telemetry_panel_group.py`` (adds the ``grian_ui``
    application), ``_1100_project_telemetry_panel_group.py`` and
    ``_1105_project_instance_metrics_panel.py`` from
    ``src/grian_ui/local/enabled`` to the ``local/enabled`` directory of
    your Horizon installation.

3.  **Fetch the vendored JavaScript**

    Run ``src/grian_ui/static/vendor/get_vendor.sh`` (Chart.js and htmx),
    then ``collectstatic``, and restart the web server.

The panel is not in the sidebar. Open
``/dashboard/project/instance_metrics/`` and pick an instance, or add
``?instance=<server uuid>``.

Checking the data
=================

As a project member (``source openrc demo demo``), each running instance
has one polled ``ceilometer_cpu`` series::

    openstack metric query \
        'ceilometer_cpu{resource_name=~".+:instance-.+"}'

The panel's own queries are subqueries (``(…)[1h:1m]``), which the
``openstack metric query`` command cannot print; send them to the Aetos
``/api/v1/query`` endpoint with a token instead.

Try an administrator scoped to another project with
``?instance=<uuid of a demo instance>``: the page must refuse it.

With the ``fake`` datasource (``GRIAN_PLUGIN = {"datasource": "fake"}``) the
panel shows four sample instances of the current project: ``web-01`` and
``db-01`` running, ``build-01`` shut off and ``old-test`` in ERROR.
