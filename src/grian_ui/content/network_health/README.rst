================================
Testing the Network Health panel
================================

.. note::
    These are development instructions. See
    ``doc/source/admin-network-health.rst`` for the panel documentation.

Prerequisites
=============

1.  **Telemetry**

    Ceilometer (compute agent polling the instance interfaces), sg-core,
    Prometheus and Aetos must be deployed (see the DevStack sample
    ``devstack/local.conf-all-in-one``). Prometheus should keep at least
    seven days of history (routers are compared with the same time a week
    before).

2.  **Enable the panels**

    Copy the files under ``src/grian_ui/local/enabled`` to the
    ``local/enabled`` directory of your Horizon installation. The panel
    group file ``_1000_admin_telemetry_panel_group.py`` is required: it adds
    the ``grian_ui`` application.

3.  **Configure settings**

    Copy the settings file from ``src/grian_ui/local/local_settings.d`` to
    the ``local/local_settings.d`` directory of your Horizon installation.
    Optionally override ``GRIAN_PLUGIN['network_health_thresholds']`` and
    ``GRIAN_PLUGIN['network_health_rate_window']``.

4.  **Fetch the vendored JavaScript**

    Run ``src/grian_ui/static/vendor/get_vendor.sh`` (Chart.js and htmx),
    then ``collectstatic``.

After completing these steps, restart your web server.

Checking the data
=================

Each running instance interface should have one
``ceilometer_network_incoming_packets`` series, whose ``resource`` label ends
with the interface name (``tap`` + the first 11 characters of the port
ID)::

    openstack metric query \
        'count by (project) (ceilometer_network_incoming_packets)'

The agents, routers and floating IPs come from the Networking API, read
once per region every 2 minutes (the full port list every 5 minutes) and
shared by every section and administrator::

    openstack network agent list
    openstack router list --long
    openstack floating ip list

With the ``fake`` datasource (``GRIAN_PLUGIN = {"datasource": "fake"}``) the
panel shows a sample cloud: five projects, three gateway nodes, the router
of ``ci-runners`` gone quiet, two interfaces of ``data-lake`` dropping
packets and one associated floating IP DOWN.
