"""Presence: whether Central's hub holds each Node's leaf, as FleetHub's last look that reached the
hub saw it (E3d S4; 0017 Open item "Presence"; leaf state only, $SRV.PING deferred: E-E3D-CUT-8),
and whether the hub's last look reached it at all. FleetHub writes both (`central.fleet.node_bus_hub`)
and the operator reads serve them, through this module, the one owner of `node_bus_presence` and
of the presence columns of `node_links` (linked, linked_changed_at; migration 119) and of
`singletons` 'hub_reachability'. Each is written only when it changes (decision 0034, class A),
so how fresh the hub's state is comes from the worker's queue heartbeat (`worker_running_in`):
Central's bus task runs in that process. It never gates anything, and it is free of nodeapi, so
Central's web process reads it without the bus client."""
from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Final, Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from central.db import DatabaseTransactionClock, TransactionClock
from central.infra.runtime import worker_running_in
from central.infra.singletons import Singleton


class HubReachability(BaseModel):
    """`singletons` 'hub_reachability': whether FleetHub's last look reached the hub. Written
    only when it changes."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    reachable: bool


HUB_REACHABILITY: Final[Singleton[HubReachability]] = Singleton("hub_reachability", HubReachability)
_CLOCK: Final = DatabaseTransactionClock()

HubState = Literal["no_hub", "worker_stopped", "unreachable", "reachable"]


class BusLink(TypedDict):
    linked: bool | None          # node_links.linked; None never looked at
    changed_at: float | None     # node_links.linked_changed_at (the worker's clock); None never looked at
    hub: HubState


def _hub_state_in(conn) -> HubState:
    stored = HUB_REACHABILITY.read_in(conn)
    if stored is None:
        return "no_hub"
    if not worker_running_in(conn):
        return "worker_stopped"
    return "reachable" if stored.value.reachable else "unreachable"


def bus_links_in(conn, device_ids: Collection[str]) -> dict[str, BusLink]:
    """Each device's presence row and the hub's state, judged in this order: no
    'hub_reachability' row → "no_hub"; `not worker_running_in(conn)` → "worker_stopped";
    reachable False → "unreachable"; else "reachable". Read-only."""
    hub = _hub_state_in(conn)
    rows = {row["device_id"]: (row["linked"], row["linked_changed_at"])
            for row in conn.execute("SELECT device_id,linked,linked_changed_at FROM node_links "
                                    "WHERE device_id = ANY(%s)", (list(device_ids),)).fetchall()}
    return {device_id: BusLink(linked=rows.get(device_id, (None, None))[0],
                               changed_at=rows.get(device_id, (None, None))[1], hub=hub)
            for device_id in device_ids}


def record_presence_in(conn, linked: Mapping[str, bool], now: float) -> None:
    """Each device of `linked` ({device_id: Central's hub holds its leaf}) whose state differs from
    its `node_links` row's, or that has never been looked at, written with `now` (the worker's
    clock) as its linked_changed_at; a device with no `node_links` row gets one (audit by
    "central", on the database's clock). Nothing else: the rows are read first, so a look that
    changed nothing writes nothing and takes no transaction id (an upsert's conflict would lock the
    row even when its WHERE refuses the update)."""
    stored = {row["device_id"]: row["linked"]
              for row in conn.execute("SELECT device_id,linked FROM node_links "
                                      "WHERE device_id = ANY(%s)", (list(linked),)).fetchall()}
    changed = [(device_id, state, now) for device_id, state in linked.items()
               if stored.get(device_id) is not state]
    if not changed:
        return
    at = _CLOCK.now_in(conn)
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO node_links(device_id,linked,linked_changed_at,created_at,created_by,changed_at,"
            "changed_by) VALUES (%s,%s,%s,%s,'central',%s,'central') "
            "ON CONFLICT(device_id) DO UPDATE SET linked=EXCLUDED.linked,"
            "linked_changed_at=EXCLUDED.linked_changed_at,changed_at=EXCLUDED.changed_at,"
            "changed_by=EXCLUDED.changed_by "
            "WHERE node_links.linked IS DISTINCT FROM EXCLUDED.linked",
            [(device_id, state, when, at, at) for device_id, state, when in changed])


def record_hub_reachable_in(conn, reachable: bool, *, times: TransactionClock) -> None:
    """`HUB_REACHABILITY.write_in(conn, HubReachability(reachable=reachable), by="central",
    times=times)`: a write only when it changes."""
    HUB_REACHABILITY.write_in(conn, HubReachability(reachable=reachable), by="central", times=times)


__all__ = ["HUB_REACHABILITY", "BusLink", "HubReachability", "HubState", "bus_links_in",
           "record_hub_reachable_in", "record_presence_in"]
