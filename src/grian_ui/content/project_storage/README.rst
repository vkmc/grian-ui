===================================
Testing the Project Storage panel
===================================

.. note::
    These are development instructions. See
    ``doc/source/project-telemetry-storage.rst`` for the panel
    documentation.

Prerequisites
=============

1.  **Deploy the telemetry pipeline**

    Ceilometer (central agent pollsters ``volume.size`` and
    ``volume.snapshot.size``), sg-core, Prometheus and Aetos (see the
    DevStack sample ``devstack/local.conf-all-in-one``). Cinder, Nova and
    Glance are read through Horizon's API layer.

2.  **Enable the panel**

    Copy ``_1000_admin_telemetry_panel_group.py`` (adds the ``grian_ui``
    application), ``_1100_project_telemetry_panel_group.py`` and
    ``_1102_project_telemetry_storage_panel.py`` from
    ``src/grian_ui/local/enabled`` to the ``local/enabled`` directory of
    your Horizon installation.

3.  **Configure settings**

    Copy the settings file from ``src/grian_ui/local/local_settings.d`` to
    the ``local/local_settings.d`` directory of your Horizon installation.
    Set ``GRIAN_PLUGIN['metrics_retention_days']`` to the Prometheus
    retention, and optionally override
    ``GRIAN_PLUGIN['project_storage_thresholds']``.

4.  **Fetch the vendored JavaScript**

    Run ``src/grian_ui/static/vendor/get_vendor.sh`` (Chart.js and htmx),
    then ``collectstatic``.

After completing these steps, restart your web server.

Checking the data
=================

As a member of the project, each volume should have one series (and each
snapshot one ``ceilometer_volume_snapshot_size`` series)::

    source ~/devstack/openrc demo demo
    openstack metric query \
        'count(ceilometer_volume_size{project="<project id>"})'

The panel's histories are subqueries, which ``openstack metric query``
cannot display; query Aetos directly with a token to see them::

    curl -s -G -H "X-Auth-Token: $(openstack token issue -f value -c id)" \
        --data-urlencode 'query=(sum(max by (resource) (last_over_time(ceilometer_volume_size{project="<project id>"}[10m]))))[15d:1d]' \
        http://<host>/prometheus/api/v1/query

With the ``fake`` datasource (``GRIAN_PLUGIN = {"datasource": "fake"}``) the
panel shows the invented ``web-shop`` project of the mockup: five volumes
(two unattached), three snapshots and three private images, 840 of 1000 GB
used, and five clean-up candidates.
