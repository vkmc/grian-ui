#
# SPDX-License-Identifier: Apache-2.0

"""
Sample project network data for the fake datasource.

A small project shaped like the DevStack demo project: one router in
front of two networks, instances whose CirrOS traffic is too low to judge,
one busier instance with a few dropped packets, ports of another project's
instances on ``private``, OVN metadata ports, a stopped instance and two
floating IPs. Interface telemetry, Networking API inventory and Compute
API instances match each other (see :mod:`grian_ui.project_network` for
the formats).
"""

import math

from grian_ui import timeseries


ROUTER_ID = "d8496252-edc1-4b22-943d-6158a0b3b3f2"
PRIVATE_ID = "c52f970c-6036-4987-8413-f4a383538667"
HEAT_NET_ID = "b7ee7e0b-696c-4e25-b4a6-7cc39b7198ce"

#: (instance id, name, Nova status, port id, IP, in pps, usual pps,
#: lost pps, in bytes/s, out bytes/s); ports of instances that are not
#: running have no telemetry.
_INSTANCES = (
    (
        "c726b640-2a18-41e3-9fac-af2730c12410",
        "grian-ih-disk",
        "ACTIVE",
        "01132844-f877-402d-8400-830891917614",
        "10.0.0.54",
        0.036,
        0.042,
        0.0,
        5.1,
        1.5,
    ),
    (
        "d414fbd9-66a6-485c-bec7-8191c2fa4221",
        "grian-ih-cpu-2",
        "ACTIVE",
        "b4493520-e425-465d-83cb-73a6e680d510",
        "10.0.0.24",
        0.036,
        0.042,
        0.0,
        5.1,
        1.5,
    ),
    (
        "5a1e2f9c-3b4d-4e8f-9a0b-1c2d3e4f5a6b",
        "web-01",
        "ACTIVE",
        "6f0e9a2b-11c4-4d7e-8f90-a1b2c3d4e5f6",
        "10.0.0.31",
        11.5,
        12.0,
        0.4,
        14500.0,
        52000.0,
    ),
    (
        "7c8d9e0f-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
        "batch-01",
        "SHUTOFF",
        "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d",
        "10.0.0.40",
        None,
        None,
        None,
        None,
        None,
    ),
)
#: An instance in ERROR: no port, no telemetry.
_ERROR_INSTANCE = ("133a85dc-be0f-44e3-8f4f-475b79e704c6", "grian-ih-error")


def _instance_rows():
    for row in _INSTANCES:
        yield dict(
            zip(
                (
                    "id",
                    "name",
                    "status",
                    "port_id",
                    "ip",
                    "in_pps",
                    "usual_pps",
                    "lost_pps",
                    "in_bps",
                    "out_bps",
                ),
                row,
            )
        )


def fake_instances():
    servers = {
        i["id"]: {"name": i["name"], "status": i["status"]}
        for i in _instance_rows()
    }
    servers[_ERROR_INSTANCE[0]] = {
        "name": _ERROR_INSTANCE[1],
        "status": "ERROR",
    }
    return {"servers": servers}


def fake_inventory():
    ports = [
        {
            "id": i["port_id"],
            "key": i["port_id"][:11],
            "network_id": PRIVATE_ID,
            "device_id": i["id"],
            "status": "ACTIVE" if i["status"] == "ACTIVE" else "DOWN",
            "admin_up": True,
            "ip": i["ip"],
        }
        for i in _instance_rows()
    ]
    return {
        "routers": [
            {
                "id": ROUTER_ID,
                "name": "router1",
                "status": "ACTIVE",
                "admin_up": True,
                "gateway": "172.24.4.67",
            }
        ],
        "networks": [
            {
                "id": PRIVATE_ID,
                "name": "private",
                "status": "ACTIVE",
                "cidr": "10.0.0.0/26",
                "router_ids": [ROUTER_ID],
                "other_project_ports": 3,
                "metadata_ports_down": 1,
            },
            {
                "id": HEAT_NET_ID,
                "name": "heat-net",
                "status": "ACTIVE",
                "cidr": "10.0.5.0/24",
                "router_ids": [ROUTER_ID],
                "other_project_ports": 0,
                "metadata_ports_down": 1,
            },
        ],
        "ports": ports,
        "floating_ips": [
            {
                "ip": "172.24.4.120",
                "status": "ACTIVE",
                "port_id": "6f0e9a2b-11c4-4d7e-8f90-a1b2c3d4e5f6",
                "fixed_ip": "10.0.0.31",
            },
            {
                "ip": "172.24.4.188",
                "status": "DOWN",
                "port_id": None,
                "fixed_ip": "",
            },
        ],
    }


def fake_quota():
    return {
        "quota": {
            "ports": {"used": 9, "limit": 50},
            "routers": {"used": 1, "limit": 10},
            "floating_ips": {"used": 2, "limit": 10},
            "security_groups": {"used": 1, "limit": 10},
        },
    }


def fake_interfaces():
    return {
        "interfaces": [
            {
                "key": i["port_id"][:11],
                "name": i["name"],
                "in_pps": i["in_pps"],
                "usual_pps": i["usual_pps"],
                "usual_basis": "1d",
                "in_bps": i["in_bps"],
                "out_bps": i["out_bps"],
                "lost_pps": i["lost_pps"],
            }
            for i in _instance_rows()
            if i["status"] == "ACTIVE"
        ]
    }


def _wave(i):
    return 1 + 0.08 * math.sin(i / 6.0)


def fake_traffic_trend(range_key, at=None):
    running = [i for i in _instance_rows() if i["status"] == "ACTIVE"]
    total_in = sum(i["in_bps"] for i in running)
    total_out = sum(i["out_bps"] for i in running)
    trend = {"in": [], "out": []}
    for i, timestamp in enumerate(timeseries.range_grid(range_key, at)):
        trend["in"].append([float(timestamp), round(total_in * _wave(i), 1)])
        trend["out"].append([float(timestamp), round(total_out * _wave(i), 1)])
    return trend


def fake_interface_trend(range_key, at=None):
    grid = timeseries.range_grid(range_key, at)
    return {
        i["port_id"][:11]: [
            [float(t), round(i["usual_pps"] * _wave(n + j), 3)]
            for n, t in enumerate(grid)
        ]
        for j, i in enumerate(_instance_rows())
        if i["status"] == "ACTIVE"
    }
