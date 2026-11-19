==========================
Using Network Health
==========================

**Is the network healthy, and is a problem the platform's or one
project's?** Network Health is under **Admin > Telemetry > Network Health**
and shows every project. For requirements and configuration, see
:doc:`admin-network-health`.


Reading the page
================

* The page refreshes every 30 seconds, and as soon as the browser tab
  becomes visible again. The metrics are new at every refresh; the data
  from the Networking API (agents, routers, floating IPs) is refreshed
  every 2 minutes, and the list of instances behind each router every 5
  minutes. Each section says when its data was fetched, and says
  "Refreshing: the data shown may be out of date" when a refresh is late.
* When the Networking API or the telemetry cannot be read, the page shows
  what the other source says and lists the checks that were not done.
* The **range buttons** (6h, 24h) only change the traffic chart. Problems
  always use the last rate window (10 minutes by default), stated next to
  the buttons.
* Colours always come with an icon and a word: green is fine, orange worth
  a look, red act now, blue information, grey not checked.
* **Networking API** tags mark panels read from Neutron; the rest is
  Ceilometer telemetry. Green badges say how many rows a table shows at
  most; the table footers say how many were left out.
* **How this page is calculated**, at the bottom, lists the queries and
  thresholds in use.


Verdict
=======

The box at the top sums the page up:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Verdict
     - What to do
   * - No network problems detected
     - Nothing. On a quiet cloud it adds that traffic is too low to judge
       routers; agents, drops and errors are still checked.
   * - Platform problem: gateway *X* is down and *N* routers in *M*
       projects lost traffic
     - Check the network agents on that host first (``ovn-controller``
       with ML2/OVN). Expect tickets from the projects listed.
   * - *N* routers in *M* projects lost traffic at the same time
     - Several projects at once points to the platform even with every
       agent alive: check the gateway nodes and the network back end (OVN
       databases, ``ovn-northd``).
   * - Network agents not alive
     - An agent stopped reporting, with no traffic impact visible yet.
       Check the agent on that host.
   * - Routers of one project went quiet
     - Probably that project's setup: its routers, instances or security
       groups.
   * - Routers in ERROR
     - Neutron reports the router in ERROR. Check the Neutron server log
       for that router.
   * - Packet drops or errors
     - See *Drops and errors by project*.
   * - Associated floating IPs DOWN
     - See *Floating IPs*; usually the router's gateway.


Summary cards
=============

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Card
     - Meaning
   * - Network agents alive
     - Alive agents out of all agents. Red when one is not alive.
   * - Routers gone quiet
     - Routers whose judged interfaces all lost their traffic. Red when
       two or more projects are affected, orange for one. Grey ("not
       checked") when the port list or the telemetry is not available.
   * - Interfaces dropping packets
     - Interfaces with dropped or failed packets, out of all interfaces
       reporting, and the packets lost per second.
   * - Cloud traffic in
     - Bytes per second received by all instances, and sent below.


Panels
======

Network agents
   Type, host, alive, admin state and last heartbeat, from the Networking
   API. Agents that are not alive come first, then disabled ones. The
   footer counts the agents of each type.

Cloud traffic
   Inbound and outbound traffic of all instances over 6 or 24 hours.
   A sudden drop of inbound traffic across the cloud often matches a
   gateway failure.

Routers, all projects
   Status in Neutron, gateway host (with *agent down* when the gateway
   agent on that host is not alive), instance ports behind the router,
   how many judged interfaces went quiet, and traffic now compared with
   the same time a week before. **Neutron keeps reporting a router
   ACTIVE when its gateway node is down; the Quiet column is what
   reveals it.** The Check
   column says *Gone quiet*, *Gateway host down*, *In error*, *Normal*,
   *Too little traffic to judge*, *No traffic data* (no interface behind
   it reports metrics), *No instances behind*, or *Not checked*.

Drops and errors by project
   The ten projects losing the most packets: their interfaces, interfaces
   with drops or errors, and packets lost per second.

Floating IPs
   Active, not associated (DOWN by design) and associated but DOWN (a real
   problem).


Limits
======

* **Traffic floor.** Below 0.5 packets/s per interface traffic is too
  bursty to call quiet. Small test clouds are often like that.
* **One week of history.** Routers are compared with the same time a week
  before: a router created less than a week ago cannot be judged yet, and a
  holiday, or a batch job that ran last week but not this week, can look
  quiet.
* **Ports and instances.** The list of ports behind each router is read
  every five minutes, so a newly attached instance can take that long to
  appear.
