#
# SPDX-License-Identifier: Apache-2.0

"""
Sample network data for the fake datasource.

The Networking API inventory (see grian_ui.network_inventory) and the
interface telemetry describe the same sample cloud: five projects, three
gateway nodes, one router of one project gone quiet and two interfaces of
another project dropping packets.
"""

import math
import time

from grian_ui import timeseries


FAKE_PROJECTS = {
    "0a1b2c3d4e5f40718293a4b5c6d7e8f9": "web-shop",
    "1b2c3d4e5f6a41829304b5c6d7e8f90a": "data-lake",
    "2c3d4e5f6a7b4293a415c6d7e8f90a1b": "ci-runners",
    "3d4e5f6a7b8c43a4b526d7e8f90a1b2c": "ml-training",
    "4e5f6a7b8c9d44b5c637e8f90a1b2c3d": "erp",
}
_PROJECT = {name: pid for pid, name in FAKE_PROJECTS.items()}

#: (router, project, gateway host, interfaces, usual pps, now vs usual)
_ROUTERS = (
    ("web-router", "web-shop", "net-01", 6, 40.0, 1.02),
    ("lake-router", "data-lake", "net-02", 5, 120.0, 0.96),
    ("ci-router", "ci-runners", "net-03", 4, 25.0, 0.02),
    ("ml-router", "ml-training", "net-01", 3, 0.1, 1.0),
    ("erp-router", "erp", "net-02", 0, 0.0, 1.0),
)
#: Interfaces on a provider network (behind no router).
_PROVIDER_INTERFACES = 3
#: Interfaces dropping packets: (router, index) -> packets lost/s.
_LOSSY = {("lake-router", 0): 3.4, ("lake-router", 3): 0.8}


def _router_id(index):
    return f"f{index:07x}-0000-4000-8000-{index:012x}"


def _port_key(router_index, index):
    return f"{router_index:04x}{index:04x}-0{router_index % 10}"


def fake_inventory():
    agents = []
    for host in ("net-01", "net-02", "net-03"):
        agents.append(_agent("OVN Controller Gateway agent", host))
    for i in range(1, 7):
        host = f"compute-{i:02d}"
        agents.append(_agent("OVN Controller agent", host))
        agents.append(_agent("OVN Metadata agent", host))
    routers = [
        {
            "id": _router_id(i),
            "name": name,
            "project_id": _PROJECT[project],
            "status": "ACTIVE",
            "admin_up": True,
            "gateway_host": gateway,
        }
        for i, (name, project, gateway, _n, _u, _r) in enumerate(_ROUTERS)
    ]
    floating_ips = (
        [{"status": "ACTIVE", "associated": True}] * 9
        + [{"status": "DOWN", "associated": False}] * 2
        + [{"status": "DOWN", "associated": True}]
    )
    return {
        "agents": agents,
        "routers": routers,
        "floating_ips": [dict(f) for f in floating_ips],
    }


def _agent(agent_type, host):
    return {
        "id": f"{agent_type}@{host}",
        "type": agent_type,
        "binary": (
            "neutron-ovn-metadata-agent"
            if "Metadata" in agent_type
            else "ovn-controller"
        ),
        "host": host,
        "alive": True,
        "admin_up": True,
        "heartbeat": time.time() - 20,
    }


def fake_topology():
    return {
        "behind": {
            _router_id(i): sorted(_port_key(i, j) for j in range(count))
            for i, (_name, _p, _g, count, _u, _r) in enumerate(_ROUTERS)
            if count
        }
    }


def fake_interfaces():
    interfaces = []
    for i, (name, project, _g, count, usual, ratio) in enumerate(_ROUTERS):
        for j in range(count):
            interfaces.append(
                {
                    "key": _port_key(i, j),
                    "project": _PROJECT[project],
                    "in_pps": round(usual * ratio * (1 + 0.05 * j), 3),
                    "usual_pps": round(usual * (1 + 0.05 * j), 3),
                    "lost_pps": _LOSSY.get((name, j), 0.0),
                }
            )
    for j in range(_PROVIDER_INTERFACES):
        interfaces.append(
            {
                "key": _port_key(len(_ROUTERS), j),
                "project": _PROJECT["erp"],
                "in_pps": 5.0,
                "usual_pps": 5.0,
                "lost_pps": 0.0,
            }
        )
    return {
        "interfaces": interfaces,
        "traffic": {"in_bps": 1.6e8, "out_bps": 1.1e8},
    }


def fake_traffic_trend(range_key, at=None):
    trend = {"in": [], "out": []}
    for i, timestamp in enumerate(timeseries.range_grid(range_key, at)):
        wave = 1 + 0.08 * math.sin(i / 6.0)
        trend["in"].append([float(timestamp), round(1.6e8 * wave, 1)])
        trend["out"].append([float(timestamp), round(1.1e8 * wave, 1)])
    return trend
