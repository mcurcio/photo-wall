"""Host telemetry: the operator's host read (G12) and the device read's host part, both read from
the host's line (decision 0034, S4a)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from central.db import Database, DatabaseTransactionClock
from central.fleet.device_fence import standing_in
from central.fleet.host_thresholds import thresholds_document
from central.fleet.node_bus_presence import bus_links_in
from central.fleet.pi_reboot import reboot_records_in
from central.infra.node_link_latest import latest_values_in
from contracts.node_boot import parse_node_boot_offer
from contracts.node_host_facts import decode_host_facts_state
from contracts.node_observation import decode_host_metrics_state
from contracts.node_state import BIRTH_KEY, HOST_FACTS_KEY, HOST_LINE, HOST_METRICS_KEY
from contracts.wire_codec import plain
from nodeapi.buffers import state_prefix, state_stream

# G12 (console DDD §63), one statement per read: every active device in its current generation and its
# newest boot offer that carried an offer (Central's offer: its base tag). The host's metrics and
# facts are its line's (`fleet_hosts_in`).
_FLEET_HOSTS_SQL = (
    "SELECT d.device_id,d.serial,co.offer_payload FROM devices d "
    "LEFT JOIN LATERAL (SELECT offer_payload FROM node_boot_offers WHERE device_id=d.device_id "
    "AND device_generation=d.generation AND offer_payload IS NOT NULL "
    "ORDER BY created_at DESC,offer_id DESC LIMIT 1) co ON true "
    "WHERE d.retired_at IS NULL ORDER BY d.device_id")
_CLOCK: Final = DatabaseTransactionClock()


def _line_values_in(conn, key: str, decode: Callable[[bytes], Any], what: str) -> dict[str, tuple[float, Any]]:
    """device id -> (when Central recorded it, the value) of every enrolled device's host line `key`;
    a removed or undecodable value is absent."""
    values = {}
    for latest in latest_values_in(conn, HOST_LINE, key, of_players=False):
        value = latest.decoded(decode, what)
        if value is not None:
            values[latest.device_id] = (latest.recorded_at, value)
    return values


def host_facts_in(conn) -> dict[str, dict]:
    """device id -> the facts its host line states now (`HostFactsState` as plain values; absent:
    none stated), in the caller's transaction: the Releases page's running base, Central's state's
    addresses."""
    return {device_id: plain(facts) for device_id, (_, facts)
            in _line_values_in(conn, HOST_FACTS_KEY, decode_host_facts_state, "host facts").items()}


def fleet_hosts_in(conn) -> dict:
    """G12 inside the caller's transaction: every active box's serial (its claim at boot), the metrics
    and facts its host line states (decision 0034, S4a: `host` and `facts`, each with when Central
    recorded it), Central's offer's base (`boot`), its presence on Central's hub (`bus_link`: the console's silence) and the numbers-only
    thresholds; `read_at` is the database's clock, the clock every `received_at` here was stamped by
    (`bus_link.changed_at` is the worker's)."""
    read_at = _CLOCK.now_in(conn)
    rows = conn.execute(_FLEET_HOSTS_SQL).fetchall()
    bus_links = bus_links_in(conn, [row["device_id"] for row in rows])
    metrics = _line_values_in(conn, HOST_METRICS_KEY, decode_host_metrics_state, "host metrics")
    facts = _line_values_in(conn, HOST_FACTS_KEY, decode_host_facts_state, "host facts")
    devices = []
    for row in rows:
        host = None
        if row["device_id"] in metrics:
            received_at, sample = metrics[row["device_id"]]
            host = {"received_at": received_at, **plain(sample)}
        stated = None
        if row["device_id"] in facts:
            # `base_tag` is the node's own record of its base: a host report, served beside
            # `boot.base_tag` (Central's offer), never merged with it. `first_received_at`: when
            # Central recorded these values (the host puts them only when they change).
            received_at, values = facts[row["device_id"]]
            stated = {"first_received_at": received_at, **plain(values)}
        boot = None
        if row["offer_payload"] is not None:
            boot = {"base_tag": parse_node_boot_offer(bytes(row["offer_payload"])).base.tag}
        devices.append({"device_id": row["device_id"], "serial": row["serial"], "host": host,
                        "boot": boot, "facts": stated,
                        "bus_link": bus_links[row["device_id"]]})
    return {"read_at": read_at, "thresholds": thresholds_document(), "devices": devices}


def host_line_in(conn, device_id: str) -> dict | None:
    """The device read's host part, from the host line as Central keeps it (node_states): `epoch`,
    the line's stream creation its host's birth was written in (the Pi's bus keeps its streams in
    memory, so every bus start, every boot among them, makes a new one), `born_at` when Central
    recorded that birth, and `received_at` when it recorded the newest metrics or facts (None:
    neither stated). None when Central keeps no birth of the device's host."""
    birth = conn.execute("SELECT epoch,recorded_at FROM node_states WHERE device_id=%s AND stream=%s "
                         "AND subject=%s AND NOT removed",
                         (device_id, state_stream(HOST_LINE), state_prefix(HOST_LINE) + BIRTH_KEY)).fetchone()
    if birth is None:
        return None
    stated = [latest.recorded_at for key in (HOST_METRICS_KEY, HOST_FACTS_KEY)
              for latest in latest_values_in(conn, HOST_LINE, key, device_id=device_id, of_players=False)
              if not latest.removed]
    return {"epoch": str(birth["epoch"]), "born_at": float(birth["recorded_at"]),
            "received_at": max(stated, default=None)}


class NodeObservations:
    def __init__(self, db: Database) -> None:
        self.db = db

    def fleet_hosts(self) -> dict:
        """G12 (`fleet_hosts_in`) under one read-only snapshot. No fleet or device lock is taken, so
        a held lock never blocks this read."""
        with self.db.read_snapshot() as conn:
            return fleet_hosts_in(conn)

    def status(self, device_id: str) -> dict:
        """The device read: the host part from the host line only (`host_line_in`), the box's boot
        claims (its boot offers) and its reboot records; no session, producer, admission,
        observation sample, manager preparation or display exchange. `read_at` is
        the database's clock, the one the host line and the reboot records were stamped by (R10)."""
        with self.db.read_snapshot() as conn:
            generation = standing_in(conn, device_id).active_generation()
            now = _CLOCK.now_in(conn)
            claims = conn.execute("SELECT kernel_boot_id,offer_id,created_at,refusal FROM node_boot_offers "
                "WHERE device_id=%s AND device_generation=%s ORDER BY created_at DESC,offer_id LIMIT 64",
                (device_id, generation)).fetchall()
            boot_claims = [{"kernel_boot_id": str(item["kernel_boot_id"]), "offer_id": str(item["offer_id"]),
                "first_received_at": item["created_at"], "offer_refusal": item["refusal"],
                "physical_identity": "unverified"} for item in claims]
            return {"boot_claims": boot_claims,
                    "device_id": device_id, "device_generation": generation, "read_at": now,
                    "host": host_line_in(conn, device_id),
                    "reboot_commands": reboot_records_in(conn, device_id), "physical_output": "unknown"}
