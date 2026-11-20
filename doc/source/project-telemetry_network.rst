======================================
Project Network dashboard
======================================

**Project > Telemetry > Network** tells the members of a project whether
their instances' networking works and, if it doesn't, whether to look at
their own setup or call their operator.

You need the reader role (or member) in the project. The page shows only
your current project: to look at another project, switch project in the
top bar.


Using the dashboard
===================

The verdict box
---------------

The coloured box at the top sums up the page in one sentence and says what
to do:

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Verdict
     - Means
     - Do
   * - No network problems detected in your project
     - Routers, floating IPs and interfaces look normal
     - Nothing
   * - *instance* stopped receiving traffic; its neighbours are fine
     - One instance went quiet; others on the same network didn't
     - Check your side: recent security group changes, the instance console
       (interface up? service running?), whether its port was disabled
   * - Likely a cloud network problem behind *router*
     - **Every** instance behind one router (at least two, not counting
       those with too little traffic to judge) went quiet at the same
       time, while Neutron still reports the router ACTIVE. The box says
       when it started, in your browser's time zone, and that instances
       on other routers are not affected only when one of them still
       receives its usual traffic. If several routers qualify, the one
       with the most instances judged is shown
     - Contact your operator with the router name, the start time and a
       link to the page
   * - Router *router* is not active and every instance behind it went
       quiet
     - The same, but the router is disabled or not ACTIVE
     - Check the router under Project > Network
   * - Network resources not active
     - A router, the port of a running instance or an associated floating
       IP isn't ACTIVE
     - Check the resource under Project > Network
   * - Packet drops or errors
     - Traffic flows but packets are lost
     - Watch it; if it lasts or grows, check the instance and tell your
       operator

This is the closest a project user can get to "is there an outage?".
Project users can't see the cloud's network agents, so the page compares
your instances with each other: one affected instance points to that
instance, all instances behind one router point to the cloud.

Summary cards
-------------

Instances gone quiet, interfaces dropping packets, routers, ports or
floating IPs not active, and current inbound and outbound traffic. A grey
card says the check could not be done (its data is not available).

By router and network
---------------------

Your routers, each followed by its networks. Networks attached to none of
your routers come last.

- **Status in Neutron:** what the Networking API reports.
- **Instances affected:** how many interfaces of your running instances on
  it are quiet.
- **Traffic now vs usual:** 100% means normal. A row for a whole router at
  0% is the outage pattern. "too little traffic" when the interfaces have
  too little traffic to judge (see below).
- **Drops + errors:** packets lost per second.

Two things you may notice:

- **"N ports of other projects":** Neutron shows you every port on networks
  you own, including other projects' instances attached to them. Their
  traffic belongs to those projects, so it isn't shown or counted.
- **Metadata ports:** OVN creates one per network. They are always DOWN by
  design and are ignored.

Project traffic
---------------

Inbound (blue) and outbound (magenta, dashed) traffic summed over all your
instances, over the range selected at the top (1, 6 or 24 hours). In an
outage the lines drop at the moment it started. Gaps mean no data.

Network quota and floating IPs
------------------------------

Ports, routers, floating IPs and security groups against their quotas. In
the floating IP list, an IP that isn't associated with an instance is DOWN
by design but still counts against your quota.

Instance interfaces
-------------------

One row per network interface (Neutron port) of your running instances,
problems first, at most 25 rows:

- **State:** *Normal*, *Quiet* (traffic below 10% of usual), *Drops*,
  *Port down*, or *No data* when the interface reports no metrics yet.
- **vs usual:** inbound packets in the last 15 minutes compared with the
  same time yesterday, or with the average of the last 6 hours when there
  is less than a day of history.
- **Too little traffic:** under 0.5 packets per second is too bursty to
  tell "stopped" from "quiet moment". Only drops, errors and port state are
  checked then. A small test instance (CirrOS) sends about one packet every
  20 seconds.
- **Inbound packets:** a sparkline over the selected range.

Instance names link to the instance metrics page when it is installed.
Interfaces of instances that are not running (shut off, in error) report
no metrics and are not checked; the table says how many.

Every section refreshes itself every 30 seconds and says when its data was
fetched. "How this page is calculated" at the bottom lists the queries and
thresholds.


Operator notes
==============

Requirements
------------

- The ``_1000_admin_telemetry_panel_group.py`` and
  ``_1100_project_telemetry_panel_group.py`` enabled files: the first adds
  the ``grian_ui`` app to Horizon (it must not be added twice), the second
  the Project > Telemetry group. The panel itself is
  ``_1103_project_telemetry_network_panel.py``.
- The Networking service; the Prometheus datasource
  (``GRIAN_PLUGIN['datasource'] = 'prometheus'``) reached through Aetos.

Settings
--------

Thresholds, overridable per key with
``GRIAN_PLUGIN['project_network_thresholds']`` (see the defaults in
``grian_ui.config.DEFAULT_PROJECT_NETWORK_THRESHOLDS``):

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Key
     - Default
     - Meaning
   * - ``quiet_percent``
     - 10
     - An interface is quiet below this percentage of its usual inbound
       packet rate (less than 100)
   * - ``quiet_min_pps``
     - 0.5
     - Interfaces whose usual rate is lower (packets/s) are not judged
   * - ``packet_loss_warning``
     - 0
     - Dropped + failed packets/s above this value are a warning
   * - ``packet_loss_critical``
     - 10
     - and at or above this value critical
   * - ``quota_warning`` / ``quota_critical``
     - 75 / 90
     - Quota bars turn amber / red at these percentages of the limit

Invalid values are shown at the top of the page.

Metrics
-------

Every query names its metrics and carries the current project's matcher
(``project="<id>"``): Aetos filters the queries of project users itself,
but not those of users with an admin role, who would otherwise see every
project. The page uses the Ceilometer interface meters:

- ``ceilometer_network_incoming_packets``: "now"
  (``rate(...[15m])``) and "usual" (the same ``offset 1d``, else
  ``avg_over_time(rate(...[15m])[6h:5m])``);
- ``ceilometer_network_{incoming,outgoing}_bytes``: traffic, rate over
  10 minutes (at least two samples with the default 300 s polling);
- ``ceilometer_network_{incoming,outgoing}_packets_{drop,error}``: each
  metric named explicitly (Aetos rejects ``{__name__=~...}`` for project
  users) and summed per interface.

Each interface has one series: ``resource_name`` is
``<instance>:tap<port key>``, so unlike the instance metrics these need no
``resource_name=~".+:instance-.+"`` filter. libvirt names an interface
``tap`` + the first 11 characters of its Neutron port ID, which joins the
metrics to the ports. The current values are one query per refresh; the
traffic chart and the sparklines are one subquery each
(``expr[range:step]``: Aetos has no ``query_range``).

APIs
----

With the user's token, through Horizon's API layer, from the Networking
API: the project's routers, floating IPs, ports and networks, the ports on
the networks the project owns, the shared networks its ports use, the
subnets of those networks, and (separately, so a failure there hides only
the quota) the network quota details (``/quotas/<project>/details``); the
project's instances from the Compute API. Every listing is filtered to the
project, also for users with the admin role, whose token would otherwise
list the whole cloud into a value shared by the members. The ports on the
project's networks include those of other projects' instances: they are
counted per network and otherwise ignored, as Aetos hides their traffic.

Refresh and load
----------------

Every value is shared through the Horizon cache by the sections and by the
members of a project, with a key holding the project ID
(``grian_ui.cache.project_cache_key``), so two projects never share data.
The Networking and Compute API data is refreshed every 60 seconds (at most
eight Networking API calls and one Compute API call), the current interface
rates with every refresh (30 seconds), the trends every 60 seconds (5
minutes for 24 hours). When one source fails, the sections build what they
can from the others and say which checks were not done; a section whose
own data is missing shows an error, or retries after 3 seconds while
another request is still computing it.
