#
# SPDX-License-Identifier: Apache-2.0

"""
Network health evaluation.

Pure functions shared by the datasources and the Network Health views.
They join three inputs into a verdict, summary cards and bounded tables,
so the amount of data rendered in the browser does not grow with the
size of the cloud:

* the **Networking API inventory** (see :mod:`grian_ui.network_inventory`),
  read with the administrator's token::

    {
        "agents": [
            {"id", "type", "binary", "host", "alive", "admin_up", "heartbeat"}
        ],  # heartbeat: epoch or None
        "routers": [
            {"id", "name", "project_id", "status", "admin_up", "gateway_host"}
        ],  # gateway_host: str or None
        "floating_ips": [{"status", "associated"}],
    }

* the **router topology** (which instance ports sit behind which router)::

    {"behind": {router_id: [port_key, ...]}}

  where ``port_key`` is the first 11 characters of the Neutron port ID,
  the part libvirt puts in the interface name (``tap<port_key>``);

* the **interface telemetry** (``DatasourceClient.get_network_interfaces``)::

    {
        "interfaces": [{"key", "project", "in_pps", "usual_pps",
                        "lost_pps"}],
        "traffic": {"in_bps", "out_bps"},
    }

  ``key`` is the port key when the interface name contains one, otherwise
  the metric's resource ID; ``usual_pps`` is the inbound rate at the same
  time a week before (USUAL_OFFSET). Missing values are None.
"""

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy


#: Window of the drop, error and "now" rates is configurable (see
#: config.get_network_health_rate_window); "usual" is the same window this
#: long before.
USUAL_OFFSET = "7d"

#: Maximum rows of the agents and routers tables.
AGENT_LIMIT = 25
ROUTER_LIMIT = 25
#: Maximum rows of the drops and errors by project table.
PROJECT_LIMIT = 10
#: Maximum names listed in the verdict.
NAME_LIMIT = 5

#: Neutron device owners of router interfaces (the ports that attach a
#: subnet to a router), per router flavour.
ROUTER_INTERFACE_OWNERS = (
    "network:router_interface",
    "network:router_interface_distributed",
    "network:ha_router_replicated_interface",
)
ROUTER_GATEWAY_OWNER = "network:router_gateway"
#: Agent types that carry router gateways: a host is a gateway host down
#: only when one of these is down (a dead metadata or DHCP agent on the
#: same host does not affect the gateway).
GATEWAY_AGENT_TYPES = (
    "OVN Controller Gateway agent",  # ML2/OVN gateway chassis
    "L3 agent",  # ML2/OVS, Linux bridge
)
#: Router status that is a problem. ALLOCATING is transient.
ROUTER_ERROR_STATUS = "ERROR"
#: Instance ports have device_owner "compute:<availability zone>".
COMPUTE_OWNER_PREFIX = "compute:"

VERDICT_OK = "ok"
VERDICT_PLATFORM = "platform"
VERDICT_MANY_PROJECTS_QUIET = "many_projects_quiet"
VERDICT_AGENTS_DOWN = "agents_down"
VERDICT_ONE_PROJECT_QUIET = "one_project_quiet"
VERDICT_ROUTERS_ERROR = "routers_error"
VERDICT_PACKET_LOSS = "packet_loss"
VERDICT_FLOATING_IPS_DOWN = "floating_ips_down"

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"

ROUTER_QUIET = "quiet"
ROUTER_ERROR = "error"
ROUTER_GATEWAY_DOWN = "gateway_down"
ROUTER_OK = "ok"
ROUTER_LOW_TRAFFIC = "low_traffic"
ROUTER_NO_DATA = "no_data"
ROUTER_EMPTY = "empty"
ROUTER_UNKNOWN = "unknown"
ROUTER_LABELS = {
    ROUTER_QUIET: gettext_lazy("Gone quiet"),
    ROUTER_ERROR: gettext_lazy("In error"),
    ROUTER_GATEWAY_DOWN: gettext_lazy("Gateway host down"),
    ROUTER_OK: gettext_lazy("Normal"),
    ROUTER_LOW_TRAFFIC: gettext_lazy("Too little traffic to judge"),
    ROUTER_NO_DATA: gettext_lazy("No traffic data"),
    ROUTER_EMPTY: gettext_lazy("No instances behind"),
    ROUTER_UNKNOWN: gettext_lazy("Not checked"),
}
_ROUTER_ORDER = {
    ROUTER_QUIET: 0,
    ROUTER_GATEWAY_DOWN: 1,
    ROUTER_ERROR: 2,
    ROUTER_OK: 3,
    ROUTER_LOW_TRAFFIC: 4,
    ROUTER_NO_DATA: 5,
    ROUTER_UNKNOWN: 6,
    ROUTER_EMPTY: 7,
}


def port_key(port_id):
    """The part of a port ID that libvirt uses in interface names."""
    return (port_id or "")[:11]


def build_topology(router_ports, instance_ports):
    """
    Map each router to the instance ports behind it.

    A port is behind a router when one of its subnets is attached to the
    router by a router interface port.

    Args:
        router_ports: ``[{"router_id", "subnets": [...]}]`` (interfaces)
        instance_ports: ``[{"key", "subnets": [...]}]``

    Returns:
        dict: ``{"behind": {router_id: [port_key, ...]}}`` (sorted keys)
    """
    routers_by_subnet = {}
    for port in router_ports:
        for subnet in port.get("subnets") or ():
            routers_by_subnet.setdefault(subnet, set()).add(port["router_id"])
    behind = {}
    for port in instance_ports:
        for subnet in port.get("subnets") or ():
            for router_id in routers_by_subnet.get(subnet, ()):
                behind.setdefault(router_id, set()).add(port["key"])
    return {"behind": {r: sorted(keys) for r, keys in behind.items()}}


def _interfaces_by_key(telemetry):
    if not telemetry:
        return {}
    return {i["key"]: i for i in telemetry.get("interfaces", [])}


def is_lossy(interface, thresholds):
    lost = interface.get("lost_pps")
    return lost is not None and lost > thresholds["packet_loss"]


def evaluate_router(router, keys, interfaces, thresholds, dead_hosts=()):
    """
    Evaluate one router from the interfaces behind it.

    Only interfaces reporting now and whose usual inbound rate is at least
    ``quiet_min_pps`` are judged. The router has gone quiet when every
    judged interface fell below ``quiet_percent`` of its usual rate.

    Args:
        keys: port keys behind the router, or None when the topology is
            not available
        interfaces: port key -> interface telemetry, or None when the
            telemetry is not available
        dead_hosts: hosts whose gateway agent is down (see
            dead_gateway_hosts)

    Returns:
        dict: the router with ``state``, ``instances``, ``reporting``,
        ``judged``, ``quiet``, ``percent`` (now vs usual, judged
        interfaces) and ``gateway_down``
    """
    row = dict(router)
    row["gateway_down"] = bool(
        router.get("gateway_host") and router["gateway_host"] in dead_hosts
    )
    row.update(instances=None, reporting=0, judged=0, quiet=0, percent=None)
    if keys is not None:
        row["instances"] = len(keys)
    if keys and interfaces is not None:
        reporting = [
            interfaces[k]
            for k in keys
            if k in interfaces and interfaces[k].get("in_pps") is not None
        ]
        judged = [
            i
            for i in reporting
            if (i.get("usual_pps") or 0) >= thresholds["quiet_min_pps"]
        ]
        limit = thresholds["quiet_percent"] / 100.0
        quiet = [i for i in judged if i["in_pps"] < i["usual_pps"] * limit]
        usual = sum(i["usual_pps"] for i in judged)
        row.update(
            reporting=len(reporting),
            judged=len(judged),
            quiet=len(quiet),
            percent=(
                round(100.0 * sum(i["in_pps"] for i in judged) / usual)
                if judged and usual
                else None
            ),
        )

    in_error = router.get("status") == ROUTER_ERROR_STATUS and bool(
        router.get("admin_up")
    )
    if row["judged"] and row["quiet"] == row["judged"]:
        state = ROUTER_QUIET
    elif row["gateway_down"]:
        state = ROUTER_GATEWAY_DOWN
    elif in_error:
        state = ROUTER_ERROR
    elif keys is None or (keys and interfaces is None):
        state = ROUTER_UNKNOWN
    elif not keys:
        state = ROUTER_EMPTY
    elif row["judged"]:
        state = ROUTER_OK
    elif row["reporting"]:
        state = ROUTER_LOW_TRAFFIC
    else:
        state = ROUTER_NO_DATA
    row["state"] = state
    return row


def is_down(agent):
    """
    An enabled agent that is not alive.

    Disabled agents (admin state DOWN, e.g. for maintenance or on a
    decommissioned host) are expected to stop reporting.
    """
    return not agent.get("alive") and bool(agent.get("admin_up"))


def dead_agents(agents):
    """Agents that are down (see is_down), sorted by host and type."""
    return sorted(
        (a for a in agents if is_down(a)),
        key=lambda a: (a.get("host") or "", a.get("type") or ""),
    )


def dead_gateway_hosts(agents):
    """Hosts with a gateway agent (GATEWAY_AGENT_TYPES) that is down."""
    return {
        a.get("host")
        for a in agents
        if is_down(a) and a.get("type") in GATEWAY_AGENT_TYPES
    }


def evaluate_routers(inventory, topology, telemetry, thresholds):
    """Evaluate every router (see evaluate_router)."""
    dead_hosts = dead_gateway_hosts(inventory["agents"])
    behind = topology.get("behind", {}) if topology is not None else None
    interfaces = (
        _interfaces_by_key(telemetry) if telemetry is not None else None
    )
    return [
        evaluate_router(
            router,
            behind.get(router["id"], []) if behind is not None else None,
            interfaces,
            thresholds,
            dead_hosts,
        )
        for router in inventory.get("routers", [])
    ]


def _router_sort_key(row):
    return (
        _ROUTER_ORDER[row["state"]],
        -row["quiet"],
        row["percent"] if row["percent"] is not None else float("inf"),
        (row.get("name") or "").lower(),
        row["id"],
    )


def select_routers(rows, limit=ROUTER_LIMIT):
    """Problems first, then the others by name; at most ``limit`` rows."""
    return sorted(rows, key=_router_sort_key)[:limit]


def floating_ip_counts(floating_ips):
    """
    Count floating IPs by state.

    Unassociated floating IPs are DOWN by design; an associated one that
    is not ACTIVE is a problem.
    """
    total = len(floating_ips)
    unassociated = sum(1 for f in floating_ips if not f.get("associated"))
    down = sum(
        1
        for f in floating_ips
        if f.get("associated") and f.get("status") != "ACTIVE"
    )
    return {
        "total": total,
        "active": total - unassociated - down,
        "unassociated": unassociated,
        "associated_down": down,
    }


def agent_rows(agents, limit=AGENT_LIMIT):
    """
    The agents table: down first, then disabled, then the others.

    Returns:
        tuple: (at most ``limit`` agents, per type counts
        ``[{"type", "total", "dead"}]`` sorted by type)
    """
    ordered = sorted(
        agents,
        key=lambda a: (
            not is_down(a),
            bool(a.get("admin_up")),
            a.get("type") or "",
            a.get("host") or "",
        ),
    )
    by_type = {}
    for agent in agents:
        counts = by_type.setdefault(
            agent.get("type") or "", {"total": 0, "dead": 0}
        )
        counts["total"] += 1
        counts["dead"] += 1 if is_down(agent) else 0
    types = [dict(type=t, **c) for t, c in sorted(by_type.items())]
    return ordered[:limit], types


def drops_by_project(telemetry, thresholds, limit=PROJECT_LIMIT):
    """
    Interfaces, interfaces with drops or errors and packets lost per
    project.

    Returns:
        tuple: (at most ``limit`` projects, most packets lost first, then
        most lossy interfaces, then most interfaces; total projects)
    """
    projects = {}
    for interface in telemetry.get("interfaces", []):
        row = projects.setdefault(
            interface.get("project") or "",
            {
                "project_id": interface.get("project") or "",
                "interfaces": 0,
                "lossy": 0,
                "lost_pps": 0.0,
            },
        )
        row["interfaces"] += 1
        if is_lossy(interface, thresholds):
            row["lossy"] += 1
            row["lost_pps"] += interface["lost_pps"]
    rows = sorted(
        projects.values(),
        key=lambda r: (
            -r["lost_pps"],
            -r["lossy"],
            -r["interfaces"],
            r["project_id"],
        ),
    )
    return rows[:limit], len(rows)


def summarize(inventory, topology, telemetry, thresholds, project_names=None):
    """
    Build the verdict and the summary cards.

    ``inventory``, ``topology`` and ``telemetry`` may be None when not
    available: the checks that need them are then reported as not done
    (``inventory_checked``, ``routers_checked`` and ``traffic_checked``
    are False).

    Returns:
        dict: ``verdict`` (``key``, ``level``) and the card values; lists
        of names are capped at NAME_LIMIT
    """
    names = project_names or {}
    inventory_checked = inventory is not None
    if inventory is None:
        inventory = {"agents": [], "routers": [], "floating_ips": []}
    agents = inventory.get("agents", [])
    dead = dead_agents(agents)
    routers = evaluate_routers(inventory, topology, telemetry, thresholds)
    quiet = [r for r in routers if r["state"] == ROUTER_QUIET]
    quiet_projects = sorted(
        {r.get("project_id") or "" for r in quiet},
        key=lambda p: names.get(p, p),
    )
    not_active = [r for r in routers if r["state"] == ROUTER_ERROR]
    judged = [r for r in routers if r["judged"]]
    # gateway hosts of the quiet routers whose gateway agent is down
    dead_gateways = sorted(
        {r["gateway_host"] for r in quiet if r.get("gateway_host")}
        & dead_gateway_hosts(agents)
    )

    interfaces = telemetry.get("interfaces", []) if telemetry else []
    lossy = [i for i in interfaces if is_lossy(i, thresholds)]
    lossy_projects = {i.get("project") or "" for i in lossy}
    fips = floating_ip_counts(inventory.get("floating_ips", []))

    if dead and len(quiet_projects) >= 2:
        key, level = VERDICT_PLATFORM, LEVEL_CRITICAL
    elif len(quiet_projects) >= 2:
        key, level = VERDICT_MANY_PROJECTS_QUIET, LEVEL_CRITICAL
    elif dead:
        key, level = VERDICT_AGENTS_DOWN, LEVEL_WARNING
    elif quiet_projects:
        key, level = VERDICT_ONE_PROJECT_QUIET, LEVEL_WARNING
    elif not_active:
        key, level = VERDICT_ROUTERS_ERROR, LEVEL_WARNING
    elif lossy:
        key, level = VERDICT_PACKET_LOSS, LEVEL_WARNING
    elif fips["associated_down"]:
        key, level = VERDICT_FLOATING_IPS_DOWN, LEVEL_WARNING
    else:
        key, level = VERDICT_OK, LEVEL_OK

    traffic = (telemetry or {}).get("traffic") or {}
    return {
        "verdict": {"key": key, "level": level},
        "agents_total": len(agents),
        "agents_alive": len(agents) - len(dead),
        "agents_dead": len(dead),
        "dead_agents": [
            _("%(type)s on %(host)s")
            % {"type": a.get("type") or "", "host": a.get("host") or ""}
            for a in dead[:NAME_LIMIT]
        ],
        "dead_gateways": dead_gateways[:NAME_LIMIT],
        "routers_total": len(routers),
        "routers_quiet": len(quiet),
        "routers_judged": len(judged),
        "routers_not_active": len(not_active),
        "not_active_routers": [
            r.get("name") or r["id"] for r in not_active[:NAME_LIMIT]
        ],
        "quiet_projects_total": len(quiet_projects),
        "quiet_projects": [
            names.get(p, p) for p in quiet_projects[:NAME_LIMIT]
        ],
        "interfaces_total": len(interfaces),
        "interfaces_lossy": len(lossy),
        "lossy_projects_total": len(lossy_projects),
        "lost_pps": sum(i["lost_pps"] for i in lossy),
        "in_bps": traffic.get("in_bps"),
        "out_bps": traffic.get("out_bps"),
        "floating_ips": fips,
        "inventory_checked": inventory_checked,
        "routers_checked": (
            inventory_checked
            and topology is not None
            and telemetry is not None
        ),
        "traffic_checked": telemetry is not None,
    }
