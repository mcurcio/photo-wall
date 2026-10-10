"""Presence: whether Central's hub holds each Node's leaf, as FleetHub's last look saw it (E3d S4;
0017 Open item "Presence"; leaf state only, $SRV.PING deferred: E-E3D-CUT-8). FleetHub writes it
(`central.fleet.node_bus_hub`) and the operator reads serve it, both through this module, the one
owner of its two tables. Both times are Central's clock. It never gates anything, and it is free of
nodeapi, so Central's web process reads it without the bus client."""
from __future__ import annotations

from collections.abc import Collection, Mapping

_NO_ROW = {"linked": None, "changed_at": None}


def bus_links_in(conn, device_ids: Collection[str]) -> dict[str, dict]:
    """`{device_id: {"linked": bool | None, "changed_at": float | None, "looked_at": float | None}}`
    for each of `device_ids`: its presence row (None where it has none) and the hub's last look (the
    same for every device; None while Central has never reached a hub)."""
    look = conn.execute("SELECT looked_at FROM node_bus_hub_looks WHERE singleton").fetchone()
    looked_at = None if look is None else look["looked_at"]
    rows = {row["device_id"]: {"linked": row["linked"], "changed_at": row["changed_at"]}
            for row in conn.execute("SELECT device_id,linked,changed_at FROM node_bus_presence "
                                    "WHERE device_id = ANY(%s)", (list(device_ids),)).fetchall()}
    return {device_id: {**rows.get(device_id, _NO_ROW), "looked_at": looked_at} for device_id in device_ids}


def record_look_in(conn, linked: Mapping[str, bool], now: float) -> None:
    """One look that reached the hub, at `now`: each device of `linked` ({device_id: Central's hub
    holds its leaf}) whose state differs from its row, or that has none, is written with `now` as its
    changed_at; the look's time becomes `now`."""
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO node_bus_presence(device_id,linked,changed_at) VALUES (%s,%s,%s) "
            "ON CONFLICT(device_id) DO UPDATE SET linked=EXCLUDED.linked,changed_at=EXCLUDED.changed_at "
            "WHERE node_bus_presence.linked<>EXCLUDED.linked",
            [(device_id, state, now) for device_id, state in linked.items()])
    conn.execute("INSERT INTO node_bus_hub_looks(singleton,looked_at) VALUES (TRUE,%s) "
                 "ON CONFLICT(singleton) DO UPDATE SET looked_at=EXCLUDED.looked_at", (now,))
