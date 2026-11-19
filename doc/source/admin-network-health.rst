========================
Network Health Dashboard
========================

The Network Health dashboard tells cloud administrators whether the network
is healthy and, when it is not, whether the problem is the platform's or
one project's. It is accessible under **Admin > Telemetry > Network Health**
and requires the ``openstack.roles.admin`` permission.

Every panel renders a bounded amount of data (a verdict, four cards, at
most 25 agents, 25 routers and 10 projects, two traffic series) whatever
the size of the cloud.

The dashboard joins two sources:

* the **Networking API** (Neutron), read with the administrator's token
  through Horizon: agents, routers, router gateway ports, floating IPs and
  the ports that place instances behind routers. Ceilometer has no Neutron
  service metric, and its interface metrics carry neither the router nor
  the subnet of an interface;
* the **interface telemetry**: the Ceilometer ``network.*`` meters of every
  instance interface, stored in Prometheus via
  `sg-core <https://github.com/infrawatch/sg-core>`_ and queried through
  the `Aetos <https://opendev.org/openstack/aetos>`_ observability proxy
  (``service_type: metric-storage``).

This page covers requirements, data sources and configuration. For a guide
to using the dashboard, see :doc:`user-network-health`.


Requirements
============

* The Ceilometer compute agent polls the instance interfaces (the
  ``network.incoming.*`` and ``network.outgoing.*`` pollsters, enabled by
  default in ``polling.yaml``).
* Prometheus keeps at least seven days of history (its default retention
  is 15 days): routers are judged against the same time a week before.
* The Networking API is reachable from Horizon. Listing agents needs the
  admin role (Neutron's default policy).


Metrics
=======

.. list-table::
   :header-rows: 1
   :widths: 50 15 35

   * - Prometheus metric
     - Unit
     - Used for
   * - ``ceilometer_network_incoming_packets``
     - packet (counter)
     - Routers gone quiet (now and one week earlier)
   * - ``ceilometer_network_{incoming,outgoing}_packets_{drop,error}``
     - packet (counter)
     - Drops and errors (four metrics, each named explicitly: Aetos rejects
       ``__name__`` regular expressions)
   * - ``ceilometer_network_{incoming,outgoing}_bytes``
     - B (counter)
     - Cloud traffic card and chart

Every series is one instance interface, labelled ``project`` and
``resource="<instance>-<instance id>-<interface>"``. Values are combined per
``(resource, project)`` with ``max`` first, so duplicated series (several
publishers or sg-core replicas) are not counted twice. All counters go
through ``rate()``.

**Interfaces and ports.** libvirt names an instance interface after the
first 11 characters of its Neutron port ID (``tap87745239-00`` for port
``87745239-0013-…``; ``vhu…`` with vhost-user). The dashboard matches
interfaces to ports by that key. Interfaces named differently are still
counted in the drops table and the traffic, but never placed behind a
router.


Networking API calls
====================

The Networking API is never called by a page refresh. Its data is read
into two shared values, computed once per region (by whichever request
finds them expired) and used by every section and every administrator:

* the **inventory snapshot**: agents, routers, router gateway ports and
  floating IPs, refreshed every **2 minutes**;
* the **port list** (every port of every project, by far the largest
  listing), refreshed every **5 minutes**: it only places instances behind
  routers, which changes slowly.

Agent, router and floating IP changes can therefore show up to 2 minutes
late, and newly attached instances up to 5 minutes late. Only the
telemetry is queried with every refresh (every 30 seconds). Each section
says when the data it shows was fetched ("Networking API data from …
(refreshed every 2 minutes)"); a copy older than its refresh interval
plus 30 seconds, served while another request is refreshing it, is
flagged as possibly out of date, and a copy kept after a failed refresh
says so. When one source fails, the verdict and cards are built from the
other and say which checks were not done.

.. list-table::
   :header-rows: 1
   :widths: 40 15 45

   * - Call
     - Shared for
     - Used for
   * - ``GET /v2.0/agents``
     - 120 s
     - Agents table and card; gateway hosts marked down
   * - ``GET /v2.0/routers``
     - 120 s
     - Routers table, status in Neutron
   * - ``GET /v2.0/ports?device_owner=network:router_gateway``
     - 120 s
     - Gateway host of each router (the port's ``binding:host_id``; with
       ML2/OVN, Neutron moves it to the chassis where the gateway is
       active)
   * - ``GET /v2.0/floatingips``
     - 120 s
     - Floating IPs panel
   * - ``GET /v2.0/ports``
     - 300 s
     - Which instance ports sit behind which router: router interface
       ports give each router's subnets, instance ports
       (``device_owner=compute:*``) on those subnets are behind it. One
       paginated listing whatever the number of routers.
   * - Identity ``GET /v3/projects``
     - 300 s
     - Project names


Status rules
============

**Gone quiet.** An interface behind a router is *quiet* when its inbound
packet rate fell below ``quiet_percent`` (10%) of its rate at the same time
a week before. Only interfaces reporting now whose usual rate is at least
``quiet_min_pps`` (0.5 packets/s) are judged: below that, traffic is too
bursty. A router has *gone quiet* when every judged interface behind it is
quiet. Interfaces that stopped reporting (stopped instances, a dead
Ceilometer compute agent) are never counted as quiet.

**Verdict**, first match wins:

.. list-table::
   :header-rows: 1
   :widths: 40 15 45

   * - Condition
     - Level
     - Verdict
   * - An agent not alive and routers of two or more projects gone quiet
     - Critical
     - Platform problem (names the gateway hosts of the quiet routers that
       lost an agent)
   * - Routers of two or more projects gone quiet, every agent alive
     - Critical
     - Probably the platform (gateway nodes, OVN databases)
   * - An agent not alive
     - Warning
     - Network agents not alive
   * - Routers of one project gone quiet
     - Warning
     - Probably that project's setup
   * - A router (administratively up) not ACTIVE
     - Warning
     - Routers not ACTIVE
   * - An interface losing more than ``packet_loss`` (0) packets/s
     - Warning
     - Packet drops or errors
   * - An associated floating IP not ACTIVE
     - Warning
     - Associated floating IPs DOWN
   * - Otherwise
     - OK
     - No network problems detected

A disabled agent (admin state DOWN) is listed but not counted as down.


Configuration
=============

Thresholds can be overridden per key in ``local_settings``:

.. code-block:: python

   GRIAN_PLUGIN = {
       "datasource": "prometheus",
       "network_health_thresholds": {
           "quiet_percent": 10,   # percent, less than 100
           "quiet_min_pps": 0.5,  # packets/s
           "packet_loss": 0,      # packets/s
       },
       # window of every rate(), at least twice the Ceilometer polling
       # interval (minutes or hours, at most 6h)
       "network_health_rate_window": "10m",
   }

With the default Ceilometer polling interval (300 s) keep the rate window
at ``10m`` or more; DevStack polls more often. Invalid values are shown at
the top of the page.


Scaling
=======

* **Telemetry**: three queries per refresh (per-interface rates, cloud
  traffic, and the traffic chart, shared for 60 s or 300 s depending on
  the range). The per-interface query returns three series per interface;
  its result is cached once for every administrator of a region. Very
  large clouds may exceed memcached's item size, in which case the value is
  cached per Horizon process (see ``GRIAN_PLUGIN['cache_max_item_bytes']``).
* **Networking API**: four listings every 2 minutes and the full port
  listing every 5 minutes per region, whatever the number of
  administrators or open pages; the verdict, cards and router table are
  joined once per process for each set of inputs.
* **Browser**: the tables are capped (problems first) and say how many
  rows they leave out.


Troubleshooting
===============

**"Too little traffic to judge" on every router.** Instance traffic is
under ``quiet_min_pps``. Agents, drops, errors and floating IPs are still
checked.

**Routers show "No traffic data".** No interface behind the router reports
``ceilometer_network_incoming_packets``: the instances are stopped, or the
Ceilometer compute agent on their host stopped polling. Check with::

    openstack metric query \
        'count by (project) (ceilometer_network_incoming_packets)'

**"Instance ports behind" is higher than the number of instances
reporting.** Ports of stopped instances are behind the router but have no
telemetry.

**Gateway host is empty.** The router has no external gateway, or its
gateway port is not bound to a host.
