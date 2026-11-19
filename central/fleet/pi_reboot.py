"""A Pi's reboot, asked of its host over the Pi's fleet-pipe link (decision 0034, S4a).

Only the worker holds the Pis' links (central/node_bus_wiring.py), and Install is written by the API
process too, so a reboot is a job on Central's queue: the write that wants it publishes `RebootPi` in
its own transaction (an Install that rolls back queues none; one pending copy per Pi), and the
worker's `RebootPiHandler` asks the Pi's host (contracts/node_state.py HOST_REBOOT_METHOD) through
`PiReboot`, then records the ask as one `events` row (`link_action_in`, kind 'reboot', body
{"request_id", "reason", "by", "delivered"}), kept `EVENTS_KEPT_SECONDS` like every event. A Pi with
no link now, or that does not answer, is not asked again: its next boot takes its version anyway.

The operator's reboot (`OperatorReboots`, S4c) is the same job: the route fences the device, defers
`RebootPi` with the console's command id as its `request_id`, and writes an ask row (kind
'reboot-asked', body {"request_id", "reason", "by"}) in that transaction, so a queued ask is listed
before the worker runs it. `reboot_records_in` folds the two by `request_id` for the device read.
Both are `events` rows, kept a week: the old path's reboot commands table kept every reboot forever.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Final, Protocol
from uuid import UUID

from central.actor import Actor
from central.db import Database
from central.fleet.device_fence import fence_in
from central.fleet.models import NodeControlError
from central.infra.events import link_action_in
from central.infra.job_queue import pending_in
from central.infra.node_links import NodeLinks
from central.infra.transactions import PgTransaction, PgTransactions, pg_connection
from central.kernel.job_types import RebootPi
from central.kernel.publishing import Publisher
from contracts.node_link import Pipe
from contracts.node_state import (
    HOST_LINE,
    HOST_REBOOT_METHOD,
    HostReboot,
    decode_host_reboot_answer,
    encode_host_reboot,
)

log = logging.getLogger(__name__)

REBOOT_CALL_SECONDS: Final = 5.0   # how long the host has to answer `reboot`
REBOOT_ACTION: Final = "reboot"    # the `events` link action's kind
REBOOT_ASKED_ACTION: Final = "reboot-asked"   # the operator's ask, written with its job
REBOOT_HISTORY: Final = 64         # the device read's reboot records, newest first
# A reboot's reason: one word (the console's `console/<date>[/<note>]`, Install's `install:<tag>`).
_REASON: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}")


class PiReboot(Protocol):
    async def reboot(self, device_id: str, reason: str) -> bool:
        """Ask the Pi's host to reboot over its fleet-pipe link; False when it has no link now or
        does not answer within the call's timeout."""
        ...


class BusPiReboot:
    """PiReboot over the worker's fleet-pipe NodeLinks (built by NodeBus.pi_reboot())."""

    def __init__(self, links: NodeLinks) -> None:
        self._links = links

    async def reboot(self, device_id: str, reason: str) -> bool:
        reply = await self._links.call(device_id, HOST_LINE, HOST_REBOOT_METHOD,
                                       encode_host_reboot(HostReboot(reason=reason)), timeout=REBOOT_CALL_SECONDS)
        if reply is None:
            return False
        try:
            return decode_host_reboot_answer(reply).accepted
        except ValueError:
            log.warning("reboot on %s: the host's answer does not decode", device_id)
            return False


class NoHubReboot:
    """PiReboot with no hub deployed: no Pi is asked (its next boot takes its version)."""

    async def reboot(self, device_id: str, reason: str) -> bool:
        return False


class RebootPiHandler:
    """Handler[RebootPi]: ask the Pi through `PiReboot`, then record the ask in `events`."""

    def __init__(self, reboot: PiReboot, transactions: PgTransactions) -> None:
        self._reboot = reboot
        self._transactions = transactions

    async def handle(self, job: RebootPi) -> None:
        delivered = await self._reboot.reboot(job.device_id, job.reason)
        log.info("reboot %s on %s (%s, by %s): %s", job.request_id, job.device_id, job.reason, job.by,
                 "asked" if delivered else "not delivered")
        await asyncio.to_thread(self._record, job, delivered)

    def _record(self, job: RebootPi, delivered: bool) -> None:
        with self._transactions.begin() as tx:
            link_action_in(pg_connection(tx), job.device_id, Pipe.FLEET, REBOOT_ACTION,
                           {"request_id": job.request_id, "reason": job.reason, "by": job.by,
                            "delivered": delivered})


@dataclass(frozen=True, slots=True)
class OperatorReboot:
    """The operator's reboot body: {"command_id": UUID, "reason": one word}."""

    command_id: UUID
    reason: str

    def __post_init__(self) -> None:
        if type(self.command_id) is not UUID:
            raise ValueError("invalid_operator_reboot")
        if not isinstance(self.reason, str) or _REASON.fullmatch(self.reason) is None:
            raise ValueError("invalid_operator_reboot")


def parse_operator_reboot(value: object) -> OperatorReboot:
    """The body as strict JSON gave it, exactly its two fields; ValueError for anything else."""
    if not isinstance(value, dict) or set(value) != {"command_id", "reason"} \
            or not isinstance(value["command_id"], str):
        raise ValueError("invalid_operator_reboot")
    try:
        command_id = UUID(value["command_id"])
    except ValueError as exc:
        raise ValueError("invalid_operator_reboot") from exc
    return OperatorReboot(command_id, value["reason"])


class OperatorReboots:
    """The operator's reboot of one Pi: S4a's `RebootPi` job, deferred with its ask row."""

    def __init__(self, db: Database, publisher: Publisher | None) -> None:
        self._db = db
        self._publisher = publisher

    def request_operator_reboot(self, device_id: str, request: OperatorReboot, *, by: Actor) -> dict:
        """Queue the reboot: {"duplicate": True} for a command id already asked of this device;
        403 `node_device_unavailable` unless the device is active; 409 `node_reboot_outstanding`
        while another reboot of the Pi is queued (an operator's or Install's); 503
        `node_reboot_unavailable` without a job queue."""
        if self._publisher is None:
            raise NodeControlError("node_reboot_unavailable", 503)
        request_id = str(request.command_id)
        with self._db.transaction() as conn:
            fence_in(conn, device_id, "act").generation()
            if conn.execute(_ASKED_SQL, (device_id, REBOOT_ASKED_ACTION, request_id)).fetchone() is not None:
                return {"duplicate": True}
            job = RebootPi(request_id=request_id, device_id=device_id, reason=request.reason, by=by)
            self._publisher.publish(job, within=PgTransaction(conn))
            # A defer merged into another pending copy (the queueing lock: one per Pi) leaves that
            # copy, not this one, pending; it waited for that copy's commit, so this read sees it.
            if not any(status == "todo" and queued == job for queued, status in pending_in(conn, job)):
                raise NodeControlError("node_reboot_outstanding", 409)
            link_action_in(conn, device_id, Pipe.FLEET, REBOOT_ASKED_ACTION,
                           {"request_id": request_id, "reason": request.reason, "by": by})
        return {"duplicate": False}


# One device's ask of `request_id` (one `%s` each for the device, the ask's kind and the id).
_ASKED_SQL: Final = ("SELECT 1 FROM events WHERE kind = 'link-action' AND device_id = %s "
                     "AND body->>'kind' = %s AND body->'body'->>'request_id' = %s LIMIT 1")
# The device's asks and outcomes, newest first (one `%s` each for the device, the two kinds, the limit).
_RECORDS_SQL: Final = ("SELECT body->>'kind' AS kind, body->'body' AS body, recorded_at FROM events "
                       "WHERE kind = 'link-action' AND device_id = %s AND body->>'kind' IN (%s, %s) "
                       "ORDER BY recorded_at DESC, id DESC LIMIT %s")


def reboot_records_in(conn: Any, device_id: str) -> list[dict]:
    """The device's reboots, newest first: each ask folded with its outcome by `request_id`
    (`command_id`, `reason`, `by`, `requested_at`, `delivered`: true asked and accepted, false not
    delivered, None not run yet; `answered_at` the outcome's time; `outstanding`: not run and its
    job still queued or running). An Install's reboot has no ask: its outcome alone is listed."""
    rows = conn.execute(_RECORDS_SQL, (device_id, REBOOT_ASKED_ACTION, REBOOT_ACTION,
                                       2 * REBOOT_HISTORY)).fetchall()
    probe = RebootPi(request_id="read", device_id=device_id, reason="read", by="central")
    running = {queued.request_id for queued, _ in pending_in(conn, probe)}
    records: dict[str, dict] = {}
    for row in rows:   # newest first: an outcome is seen before its ask
        body = row["body"]
        request_id = str(body.get("request_id"))
        record = records.setdefault(request_id, {
            "command_id": request_id, "reason": body.get("reason"), "by": body.get("by"),
            "requested_at": float(row["recorded_at"]), "delivered": None, "answered_at": None})
        if row["kind"] == REBOOT_ACTION and record["answered_at"] is None:
            record["delivered"] = body.get("delivered") is True
            record["answered_at"] = float(row["recorded_at"])
        elif row["kind"] == REBOOT_ASKED_ACTION:
            record["requested_at"] = float(row["recorded_at"])
    listed = sorted(records.values(), key=lambda item: item["requested_at"], reverse=True)[:REBOOT_HISTORY]
    for record in listed:
        record["outstanding"] = record["answered_at"] is None and record["command_id"] in running
    return listed


__all__ = ["REBOOT_ACTION", "REBOOT_ASKED_ACTION", "BusPiReboot", "NoHubReboot", "OperatorReboot",
           "OperatorReboots", "PiReboot", "RebootPiHandler", "parse_operator_reboot", "reboot_records_in"]
