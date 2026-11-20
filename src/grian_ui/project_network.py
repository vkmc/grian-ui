#
# SPDX-License-Identifier: Apache-2.0

"""
Project network health evaluation.

Pure functions used by the Project > Telemetry > Network views. They join
three inputs, all scoped to the current project, into a verdict, summary
cards and bounded tables:

* the **Networking API inventory** (see
  :mod:`grian_ui.project_network_inventory`), read with the user's token::

    {
        "routers": [{"id", "name", "status", "admin_up", "gateway"}],
        "networks": [
            {
                "id",
                "name",
                "status",
                "cidr",
                "router_ids",
                "other_project_ports",
                "metadata_ports_down",
            }
        ],
        "ports": [
            {
                "id",
                "key",
                "network_id",
                "device_id",
                "status",
                "admin_up",
                "ip",
            }
        ],
        "floating_ips": [{"ip", "status", "port_id", "fixed_ip"}],
    }

  The network quota is read separately (a failure there must not hide
  the rest)::

    {
        "quota": {
            "ports": {"used", "limit"},
            "routers": ...,
            "floating_ips": ...,
            "security_groups": ...,
        }
    }

  ``ports`` holds only the project's own instance ports (device owner
  ``compute:*``). Neutron also lists the ports of other projects' instances
  on networks the project owns: their traffic belongs to those projects
  (Aetos hides it), so they are only counted, per network
  (``other_project_ports``), or they would look quiet. OVN metadata ports
  are DOWN by design and are only counted (``metadata_ports_down``);

* the **Compute API instances**: ``{"servers": {id: {"name", "status"}}}``.
  Instances that are not running report no traffic: their ports are not
  judged;

* the **interface telemetry**
  (``DatasourceClient.get_project_network_interfaces``)::

    {
        "interfaces": [
            {
                "key",
                "name",
                "in_pps",
                "usual_pps",
                "usual_basis",
                "in_bps",
                "out_bps",
                "lost_pps",
            }
        ]
    }

  ``key`` is the first 11 characters of the Neutron port ID, the part
  libvirt puts in the interface name (``tap<key>``); ``name`` is the
  instance name from the metric. ``usual_pps`` is the inbound rate at the
  same time yesterday (``usual_basis`` "1d"), or the average of the last
  6 hours when there is less history ("6h"). Missing values are None.

Any input may be None when it is not available; the checks that need it
are then reported as not done.
"""

from django.utils.translation import gettext_lazy


#: Maximum rows of the interfaces and floating IP tables.
INTERFACE_LIMIT = 25
FLOATING_IP_LIMIT = 25
#: Maximum names listed in the verdict.
NAME_LIMIT = 5
#: Window of the quiet check ("now" and "usual") and of the drop, error and
#: traffic rates. The drop and traffic window holds at least two samples
#: with the default Ceilometer polling interval (300 s).
QUIET_WINDOW = "15m"
RATE_WINDOW = "10m"
#: "Usual" is the same window this long before, else the average over the
#: fallback range.
USUAL_OFFSET = "1d"
USUAL_FALLBACK = "6h"

#: Neutron device owners.
ROUTER_INTERFACE_OWNERS = (
    "network:router_interface",
    "network:router_interface_distributed",
    "network:ha_router_replicated_interface",
)
METADATA_OWNER = "network:distributed"
COMPUTE_OWNER_PREFIX = "compute:"

VERDICT_OK = "ok"
VERDICT_ROUTER_QUIET = "router_quiet"
VERDICT_INSTANCES_QUIET = "instances_quiet"
VERDICT_NOT_ACTIVE = "not_active"
VERDICT_PACKET_LOSS = "packet_loss"

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"

STATE_PORT_DOWN = "port_down"
STATE_QUIET = "quiet"
STATE_DROPS = "drops"
STATE_NO_DATA = "no_data"
STATE_NOT_CHECKED = "not_checked"
STATE_OK = "ok"
STATE_LABELS = {
    STATE_PORT_DOWN: gettext_lazy("Port down"),
    STATE_QUIET: gettext_lazy("Quiet"),
    STATE_DROPS: gettext_lazy("Drops"),
    STATE_NO_DATA: gettext_lazy("No data"),
    STATE_NOT_CHECKED: gettext_lazy("Not checked"),
    STATE_OK: gettext_lazy("Normal"),
}

#: Quota rows, in display order: inventory key -> label.
QUOTA_LABELS = (
    ("ports", gettext_lazy("Ports")),
    ("routers", gettext_lazy("Routers")),
    ("floating_ips", gettext_lazy("Floating IPs")),
    ("security_groups", gettext_lazy("Security groups")),
)

_TELEMETRY_FIELDS = (
    "in_pps",
    "usual_pps",
    "usual_basis",
    "in_bps",
    "out_bps",
    "lost_pps",
)


def port_key(port_id):
    """The part of a port ID that libvirt uses in interface names."""
    return (port_id or "")[:11]


def is_judged(interface, thresholds):
    """Reporting now, with a usual rate high enough to call it quiet."""
    return (
        interface.get("in_pps") is not None
        and interface.get("usual_pps") is not None
        and interface["usual_pps"] >= thresholds["quiet_min_pps"]
    )


def is_quiet(interface, thresholds):
    """Inbound traffic below ``quiet_percent`` of the usual rate."""
    if not is_judged(interface, thresholds):
        return False
    limit = interface["usual_pps"] * thresholds["quiet_percent"] / 100.0
    return interface["in_pps"] < limit


def percent_of_usual(interface, thresholds):
    """Inbound traffic now in percent of usual, or None if not judged."""
    if not is_judged(interface, thresholds):
        return None
    return round(100.0 * interface["in_pps"] / interface["usual_pps"])


def loss_level(lost_pps, thresholds):
    """None, LEVEL_WARNING or LEVEL_CRITICAL for a dropped + failed rate."""
    if lost_pps is None or lost_pps <= thresholds["packet_loss_warning"]:
        return None
    if lost_pps >= thresholds["packet_loss_critical"]:
        return LEVEL_CRITICAL
    return LEVEL_WARNING


def _evaluate_interface(row, thresholds, telemetry_checked):
    row["judged"] = is_judged(row, thresholds)
    row["quiet"] = is_quiet(row, thresholds)
    row["percent"] = percent_of_usual(row, thresholds)
    row["loss"] = loss_level(row.get("lost_pps"), thresholds)
    if row.get("port_status") not in (None, "ACTIVE"):
        state, severity = STATE_PORT_DOWN, 3
    elif row["quiet"]:
        state, severity = STATE_QUIET, 3
    elif row["loss"]:
        state = STATE_DROPS
        severity = 3 if row["loss"] == LEVEL_CRITICAL else 2
    elif not telemetry_checked:
        state, severity = STATE_NOT_CHECKED, 0
    elif not row["has_data"]:
        state, severity = STATE_NO_DATA, 0
    else:
        state, severity = STATE_OK, 0
    row["state"] = state
    row["severity"] = severity
    return row


def _telemetry_rows(by_key, thresholds):
    """Rows from the telemetry alone (no Networking API data)."""
    rows = []
    for interface in by_key.values():
        row = dict(
            interface,
            port_id=None,
            network_id=None,
            network_name="",
            ip="",
            device_id=None,
            instance_name=interface.get("name") or interface["key"],
            port_status=None,
            has_data=True,
        )
        rows.append(_evaluate_interface(row, thresholds, True))
    return rows


def interface_rows(inventory, instances, telemetry, thresholds):
    """
    One row per network interface of the project's running instances.

    Rows come from the Networking API ports, joined to the telemetry by
    port key. Ports of instances that are not running (Compute API) are
    left out and counted. Without the Networking API, rows come from the
    telemetry alone (no network, router or port state).

    Returns:
        tuple: (rows in no particular order, ports of instances not
        running)
    """
    by_key = {i["key"]: i for i in (telemetry or {}).get("interfaces", [])}
    if inventory is None:
        return _telemetry_rows(by_key, thresholds), 0
    servers = instances.get("servers") if instances is not None else None
    networks = {n["id"]: n for n in inventory.get("networks", [])}
    rows, not_running = [], 0
    for port in inventory.get("ports", []):
        server = (servers or {}).get(port.get("device_id"))
        if servers is not None and (
            server is None or server.get("status") != "ACTIVE"
        ):
            not_running += 1
            continue
        key = port.get("key") or port_key(port.get("id"))
        interface = by_key.get(key) or {}
        network = networks.get(port.get("network_id")) or {}
        row = {
            "key": key,
            "port_id": port.get("id"),
            "network_id": port.get("network_id"),
            "network_name": network.get("name") or "",
            "ip": port.get("ip") or "",
            "device_id": port.get("device_id"),
            "instance_name": (server or {}).get("name")
            or interface.get("name")
            or port.get("device_id")
            or "",
            "port_status": port.get("status"),
            "port_admin_up": port.get("admin_up", True),
            "has_data": bool(interface),
        }
        for field in _TELEMETRY_FIELDS:
            row[field] = interface.get(field)
        rows.append(
            _evaluate_interface(row, thresholds, telemetry is not None)
        )
    return rows, not_running


def select_interfaces(rows, limit=INTERFACE_LIMIT):
    """Problems first, then the most inbound traffic; at most ``limit``."""
    return sorted(
        rows,
        key=lambda r: (
            -r["severity"],
            -(r.get("in_bps") or 0),
            (r.get("instance_name") or "").lower(),
            r.get("port_id") or r.get("key") or "",
        ),
    )[:limit]


def _group(rows):
    """Counts and traffic now vs usual of a group of interface rows."""
    quiet = sum(1 for r in rows if r["quiet"])
    judged = [r for r in rows if r["judged"]]
    usual = sum(r["usual_pps"] for r in judged)
    lost = [r["lost_pps"] for r in rows if r.get("lost_pps") is not None]
    return {
        "total": len(rows),
        "judged": len(judged),
        "quiet": quiet,
        # as for each interface: only when every one has enough traffic
        "percent": (
            round(100.0 * sum(r["in_pps"] for r in judged) / usual)
            if rows and len(judged) == len(rows) and usual
            else None
        ),
        "lost_pps": sum(lost) if lost else None,
        # over the interfaces with enough traffic to judge: one idle
        # instance must not hide an outage
        "all_quiet": len(judged) >= 2 and quiet == len(judged),
    }


def router_active(router):
    """ACTIVE in Neutron and not disabled by its owner."""
    return router.get("status") == "ACTIVE" and bool(
        router.get("admin_up", True)
    )


def topology(inventory, rows):
    """
    The "By router and network" table: each router, then its networks.

    Networks attached to no router of the project come last, under a
    group without router. Interfaces are counted once per router even when
    two of its networks hold them.

    Returns:
        list: rows with ``type`` "router" (or "no_router") or "network";
        router rows carry ``keys``, the port keys behind them, and
        ``judged_keys``, those with enough traffic to judge
    """
    if inventory is None:
        return []
    by_network = {}
    for row in rows:
        by_network.setdefault(row.get("network_id"), []).append(row)
    networks = sorted(
        inventory.get("networks", []),
        key=lambda n: ((n.get("name") or "").lower(), n["id"]),
    )
    table = []
    attached = set()
    groups = [
        (router, [n for n in networks if router["id"] in n["router_ids"]])
        for router in sorted(
            inventory.get("routers", []),
            key=lambda r: ((r.get("name") or "").lower(), r["id"]),
        )
    ]
    for _router, selected in groups:
        attached.update(n["id"] for n in selected)
    rest = [n for n in networks if n["id"] not in attached]
    if rest:
        groups.append(({"type": "no_router", "id": None, "name": ""}, rest))
    for router, selected in groups:
        behind = {}
        for network in selected:
            for row in by_network.get(network["id"], []):
                behind[row["port_id"]] = row
        head = dict(router, **_group(list(behind.values())))
        head["keys"] = sorted(r["key"] for r in behind.values())
        head["judged_keys"] = sorted(
            r["key"] for r in behind.values() if r["judged"]
        )
        if head.get("type") != "no_router":
            head.update(type="router", active=router_active(router))
        table.append(head)
        table.extend(
            dict(
                network,
                type="network",
                **_group(by_network.get(network["id"], [])),
            )
            for network in selected
        )
    return table


def quiet_since(series, keys, interfaces, thresholds):
    """
    When the interfaces ``keys`` went quiet together, or None.

    ``series`` is ``{key: [[timestamp, pps or None], ...]}`` on one time
    grid; ``interfaces`` maps keys to their telemetry (``usual_pps``). The
    group is quiet at a point when its summed inbound rate is below
    ``quiet_percent`` of its summed usual rate. A point where some
    interface has no value counts only when the others already reach that
    rate (not quiet): a missing series would look quiet.

    Returns:
        float: timestamp of the first point of the current quiet stretch,
        or None when it is not found in the series (quiet over the whole
        range, or no data)
    """
    usual = sum((interfaces.get(k) or {}).get("usual_pps") or 0 for k in keys)
    if not usual:
        return None
    limit = usual * thresholds["quiet_percent"] / 100.0
    keys = set(keys)
    totals, counts = {}, {}
    for key in keys:
        for timestamp, value in series.get(key) or ():
            if value is not None:
                totals[timestamp] = totals.get(timestamp, 0.0) + value
                counts[timestamp] = counts.get(timestamp, 0) + 1
    totals = {
        t: v for t, v in totals.items() if counts[t] == len(keys) or v >= limit
    }
    since = None
    for timestamp in sorted(totals, reverse=True):
        if totals[timestamp] >= limit:
            return since
        since = timestamp
    return None


def floating_ip_rows(inventory, rows, limit=FLOATING_IP_LIMIT):
    """
    Floating IPs with their target instance (or fixed IP).

    An unassociated floating IP is DOWN by design; an associated one that
    is not ACTIVE is a problem (listed first).

    Returns:
        tuple: (at most ``limit`` rows, total)
    """
    if inventory is None:
        return [], 0
    names = {r["port_id"]: r["instance_name"] for r in rows if r["port_id"]}
    result = []
    for fip in inventory.get("floating_ips", []):
        associated = bool(fip.get("port_id"))
        result.append(
            dict(
                fip,
                associated=associated,
                target=names.get(fip.get("port_id"))
                or fip.get("fixed_ip")
                or "",
                problem=associated and fip.get("status") != "ACTIVE",
            )
        )
    result.sort(key=lambda f: (not f["problem"], f.get("ip") or ""))
    return result[:limit], len(result)


def quota_rows(quota, thresholds):
    """Ports, routers, floating IPs and security groups against quota."""
    quota = (quota or {}).get("quota") or {}
    result = []
    for key, label in QUOTA_LABELS:
        item = quota.get(key)
        if not item:
            continue
        used, limit = item.get("used") or 0, item.get("limit")
        percent = None
        if limit is not None and limit >= 0:
            percent = round(100.0 * used / limit) if limit else 100
        level = LEVEL_OK
        if percent is not None and percent >= thresholds["quota_critical"]:
            level = LEVEL_CRITICAL
        elif percent is not None and percent >= thresholds["quota_warning"]:
            level = LEVEL_WARNING
        result.append(
            {
                "key": key,
                "label": label,
                "used": used,
                "limit": limit if percent is not None else None,
                "percent": percent,
                "bar": min(percent, 100) if percent is not None else 0,
                "level": level,
            }
        )
    return result


def not_active(inventory, rows):
    """
    Resources that are not active, as ``[{"kind", "name"}]``.

    Routers not ACTIVE or disabled, associated floating IPs not ACTIVE,
    and ports of running instances not ACTIVE. Metadata ports and
    unassociated floating IPs are DOWN by design and not listed.
    """
    if inventory is None:
        return []
    found = [
        {"kind": "router", "name": r.get("name") or r["id"]}
        for r in inventory.get("routers", [])
        if not router_active(r)
    ]
    found += [
        {"kind": "floating_ip", "name": f.get("ip") or ""}
        for f in inventory.get("floating_ips", [])
        if f.get("port_id") and f.get("status") != "ACTIVE"
    ]
    found += [
        {"kind": "port", "name": r["instance_name"]}
        for r in rows
        if r["state"] == STATE_PORT_DOWN
    ]
    return found


def _names(rows):
    names = []
    for row in rows:
        if row["instance_name"] not in names:
            names.append(row["instance_name"])
    return names


def _verdict(rows, table, inactive, lossy):
    groups = [t for t in table if t["type"] != "network" and t["total"]]
    outage = max(
        (t for t in groups if t["type"] == "router" and t["all_quiet"]),
        key=lambda t: t["judged"],
        default=None,
    )
    quiet = [r for r in rows if r["quiet"]]
    if outage is not None:
        return {
            "key": VERDICT_ROUTER_QUIET,
            "level": LEVEL_CRITICAL if outage["active"] else LEVEL_WARNING,
            "router": outage.get("name") or outage["id"],
            "router_active": outage["active"],
            # the quiet interfaces; the others have too little traffic
            "total": outage["judged"],
            "keys": outage["judged_keys"],
            "not_judged": outage["total"] - outage["judged"],
            # other routers with an instance judged and not quiet
            "others_fine": any(
                t is not outage and t["quiet"] < t["judged"] for t in groups
            ),
        }
    if quiet:
        first = quiet[0]
        neighbours = [
            r
            for r in rows
            if first["network_id"]
            and r["network_id"] == first["network_id"]
            and r["device_id"] != first["device_id"]
            and r["judged"]
            and not r["quiet"]
        ]
        names = _names(quiet)
        return {
            "key": VERDICT_INSTANCES_QUIET,
            "level": LEVEL_WARNING,
            "instances": names[:NAME_LIMIT],
            "instances_total": len(names),
            "network": first["network_name"],
            "has_neighbours": bool(neighbours),
        }
    if inactive:
        return {
            "key": VERDICT_NOT_ACTIVE,
            "level": LEVEL_WARNING,
            "resources": inactive[:NAME_LIMIT],
            "resources_total": len(inactive),
        }
    if lossy:
        return {
            "key": VERDICT_PACKET_LOSS,
            "level": LEVEL_WARNING,
            "interfaces_total": len(lossy),
        }
    return {"key": VERDICT_OK, "level": LEVEL_OK}


def summarize(inventory, instances, telemetry, thresholds):
    """
    Build the verdict, the summary cards and the joined rows.

    Verdicts, most severe first: every instance behind one router quiet
    (probably the cloud, critical; a warning when the router itself is not
    active or disabled), some instances quiet (check those instances),
    resources not active, drops or errors, else OK.

    Returns:
        dict: ``verdict`` (``key``, ``level`` and what its text needs),
        card values, ``rows`` (every interface row), ``topology`` and
        ``inventory_checked`` / ``traffic_checked``
    """
    rows, not_running = interface_rows(
        inventory, instances, telemetry, thresholds
    )
    table = topology(inventory, rows)
    lossy = [r for r in rows if r["loss"]]
    inactive = not_active(inventory, rows)
    interfaces = (telemetry or {}).get("interfaces", [])
    judged = sum(1 for r in rows if r["judged"])
    reporting = sum(1 for r in rows if r["has_data"])
    in_values = [
        i["in_bps"] for i in interfaces if i.get("in_bps") is not None
    ]
    out_values = [
        i["out_bps"] for i in interfaces if i.get("out_bps") is not None
    ]
    inventory_checked = inventory is not None
    inventory = inventory or {}
    return {
        "verdict": _verdict(rows, table, inactive, lossy),
        "rows": rows,
        "topology": table,
        "not_running": not_running,
        "interfaces_total": len(rows),
        "judged": judged,
        # interfaces reporting traffic below the floor
        "low_traffic": reporting - judged,
        "all_low": bool(reporting) and not judged,
        "quiet": sum(1 for r in rows if r["quiet"]),
        "lossy": len(lossy),
        "lossy_critical": sum(1 for r in lossy if r["loss"] == LEVEL_CRITICAL),
        "lost_pps": sum(r["lost_pps"] for r in lossy),
        "not_active": len(inactive),
        "routers_total": len(inventory.get("routers", [])),
        "floating_ips_total": len(inventory.get("floating_ips", [])),
        "in_bps": sum(in_values) if in_values else None,
        "out_bps": sum(out_values) if out_values else None,
        "metadata_ports_down": sum(
            n.get("metadata_ports_down") or 0
            for n in inventory.get("networks", [])
        ),
        "inventory_checked": inventory_checked,
        "traffic_checked": telemetry is not None,
    }
