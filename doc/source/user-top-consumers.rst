=====================================
Using the Top Consumers dashboard
=====================================

The Top Consumers dashboard answers one question for cloud administrators:
*who is using the cloud's resources?*

It ranks projects and instances by CPU, memory, disk I/O or network use,
shows their share of the total and compares what they use with what their
instances hold. The page stays the same size whether your cloud has 10 or
10,000 instances.

Open it from **Admin > Telemetry > Top Consumers**. You need the admin role.

For how the dashboard is configured and where the data comes from, see
:doc:`admin-top-consumers`.


Controls
========

* **Resource**: CPU, Memory, Disk I/O or Network. The table and the chart
  follow the choice; the summary cards always show all four.
* **Group by**: by project or by instance.
* **1h / 6h / 24h / 7d**: the period of the chart and of the CPU
  sparklines (24h by default). It never changes the current values.
* Clicking a **project name** lists that project's instances. **All
  instances** goes back to the whole cloud.

Current values use the last 10 minutes. The page refreshes itself every 60
seconds while its browser tab is visible.


Summary cards
=============

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Card
     - Meaning
   * - CPU in use
     - Cores in use by every running instance, out of the vCPUs those
       instances hold.
   * - Memory in use
     - Memory used inside the instances, out of their flavor RAM.
   * - Disk I/O
     - Read plus write throughput of every instance disk.
   * - Network
     - Inbound plus outbound traffic of every instance interface.

"Allocated" counts running instances only. It is not the capacity of your
hypervisors.


Top 10 table
============

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Column
     - Meaning
   * - Instances / Project
     - Running instances of the project, or the instance's project.
   * - *Resource* now
     - Current use.
   * - Share of total in use
     - The row's part of everything in use now, in the whole cloud (or in
       the project whose instances are listed). The swatch colour matches
       the chart; grey rows are part of *Other*.
   * - vCPUs / Flavor RAM
     - What its running instances hold (CPU and memory only).
   * - Used ÷ allocated
     - **Busy** at 80% or more; **mostly idle** under 10% (both
       configurable), judged on the percentage as shown. Mostly idle
       projects are candidates for a quota or rightsizing conversation.
   * - CPU, *range*
     - CPU trend of the project over the selected range (by project only).

Projects with nothing running are listed by name under the table. The
footer says when the project names were read from the Identity service;
they are refreshed every 5 minutes, so a new project can show its ID for a
few minutes.
Instance names link to the instance's page in **Admin > Compute >
Instances**.


Over time chart
===============

The top 5 rows of the table, stacked over the selected range, plus *Other*
(everything else). The top edge is the total. Saw-tooth shapes are typical
of batch or CI projects. Gaps mean no data was received.


Common tasks
============

Capacity planning
   Look at CPU and memory by project: which projects drive growth, and
   whether the total is steady or rising over the day.

A noisy neighbour
   Choose **Disk I/O** (or **Network**) and **By instance** to find one
   instance saturating storage or the network.

Waste
   A project holding many vCPUs at a low *used ÷ allocated* is a candidate
   for a quota or rightsizing conversation.


When data is missing
====================

* *No instance usage available*: no running instance reported in the last
  10 minutes. Check the Ceilometer compute agent and the telemetry pipeline.
* *Showing project IDs*: the Identity service could not be reached (or the
  names are still loading); the table shows project IDs, or the last names
  received, until the names load.
* *Could not be refreshed*: the last data received is shown, with its time.
* *May be out of date*: the data is being refreshed and the copy shown is
  older than usual.
* *Number of running instances unknown*: the footer cannot count
  the running instances (``ceilometer_vcpus`` is missing for some project);
  the top instances are still listed.
