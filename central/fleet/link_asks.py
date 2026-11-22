"""An ask of one Pi over its fleet-pipe link, queued by the API process and run by the worker: what a
Pi's reboot (central/fleet/pi_reboot.py) and Restart app (central/fleet/app_restart.py) share.

Only the worker holds the Pis' links (central/node_bus_wiring.py), so each ask is a job on Central's
queue, one pending copy per Pi (the job type's `queued_by`). Two `events` rows tell its story, both
link actions (`link_action_in`), kept `EVENTS_KEPT_SECONDS` like every event: the ask, written with
the job in the route's transaction (so a queued ask is listed before the worker runs it), and the
outcome, written by the worker's handler once it asked the Pi. Each body names its `request_id`;
`asks_in` folds the two by it.

This module owns the shared mechanics only: which job, which kinds, what a body holds and how an
outcome reads are each owner's.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from central.fleet.device_fence import fence_in
from central.fleet.models import NodeControlError
from central.infra.events import link_action_in
from central.infra.job_queue import pending_in
from central.infra.transactions import PgTransaction
from central.kernel.jobs import Job
from central.kernel.publishing import Publisher
from contracts.node_link import Pipe


@dataclass(frozen=True, slots=True)
class AskKinds:
    """An ask's two link-action kinds: `asked` written with its job, `outcome` by the worker."""
    asked: str
    outcome: str


@dataclass(frozen=True, slots=True)
class Ask:
    """One ask as `events` tells it. `body` is its newest row's (the outcome's once the worker ran
    it, else the ask's): both name who asked. `requested_at` is the ask's time (an ask with no ask
    row, an Install's reboot: its outcome's). `outcome` and `answered_at` are None until the worker
    ran it; `outstanding`: not run and its job still queued or running."""
    request_id: str
    body: Mapping[str, Any]
    requested_at: float
    outcome: Mapping[str, Any] | None
    answered_at: float | None
    outstanding: bool


# When one device's ask of `request_id` was recorded (one `%s` each for the device, the ask's kind and the id).
_ASKED_SQL: Final = ("SELECT recorded_at FROM events WHERE kind = 'link-action' AND device_id = %s "
                     "AND body->>'kind' = %s AND body->'body'->>'request_id' = %s ORDER BY recorded_at LIMIT 1")
# The device's asks and outcomes, newest first (one `%s` each for the device, the two kinds, the limit).
_RECORDS_SQL: Final = ("SELECT body->>'kind' AS kind, body->'body' AS body, recorded_at FROM events "
                       "WHERE kind = 'link-action' AND device_id = %s AND body->>'kind' IN (%s, %s) "
                       "ORDER BY recorded_at DESC, id DESC LIMIT %s")


def asked_at_in(conn: Any, device_id: str, kinds: AskKinds, request_id: str) -> float | None:
    """When `request_id` was asked of `device_id` (its ask row's time, the database's clock); None
    for an ask with no ask row (never asked here, or an Install's reboot)."""
    row = conn.execute(_ASKED_SQL, (device_id, kinds.asked, request_id)).fetchone()
    return None if row is None else float(row["recorded_at"])


def queue_ask_in(conn: Any, publisher: Publisher, job: Job[Any], kinds: AskKinds, *, device_id: str,
                 request_id: str, body: Mapping[str, object], outstanding: str) -> bool:
    """Queue `job` for `device_id` with its ask row (`kinds.asked`, `body`, which names
    `request_id`) in `conn`'s transaction. False, and nothing queued, for a `request_id` already
    asked of this device. 403 `node_device_unavailable` unless the device is active; 409
    `outstanding` (the owner's code) while another copy of the job is queued for the Pi."""
    fence_in(conn, device_id, "act").generation()
    if asked_at_in(conn, device_id, kinds, request_id) is not None:
        return False
    publisher.publish(job, within=PgTransaction(conn))
    # A defer merged into another pending copy (the queueing lock: one per Pi) leaves that
    # copy, not this one, pending; it waited for that copy's commit, so this read sees it.
    if not any(status == "todo" and queued == job for queued, status in pending_in(conn, job)):
        raise NodeControlError(outstanding, 409)
    link_action_in(conn, device_id, Pipe.FLEET, kinds.asked, body)
    return True


def asks_in(conn: Any, device_id: str, kinds: AskKinds, probe: Job[Any], limit: int) -> list[Ask]:
    """The device's newest `limit` asks of `kinds`, newest first, each ask folded with its outcome
    by `request_id`. `probe` is any job of the asks' type for this device: its queueing lock finds
    the copies still queued or running, which must carry a `request_id`."""
    rows = conn.execute(_RECORDS_SQL, (device_id, kinds.asked, kinds.outcome, 2 * limit)).fetchall()
    running = {queued.request_id for queued, _ in pending_in(conn, probe)}   # type: ignore[attr-defined]
    found: dict[str, dict] = {}
    for row in rows:   # newest first: an outcome is seen before its ask
        body = row["body"]
        request_id, at = str(body.get("request_id")), float(row["recorded_at"])
        ask = found.setdefault(request_id, {"body": body, "requested_at": at, "outcome": None, "answered_at": None})
        if row["kind"] == kinds.outcome and ask["answered_at"] is None:
            ask["outcome"], ask["answered_at"] = body, at
        elif row["kind"] == kinds.asked:
            ask["requested_at"] = at
    listed = sorted(found.items(), key=lambda item: item[1]["requested_at"], reverse=True)[:limit]
    return [Ask(request_id, ask["body"], ask["requested_at"], ask["outcome"], ask["answered_at"],
                outstanding=ask["answered_at"] is None and request_id in running)
            for request_id, ask in listed]


__all__ = ["Ask", "AskKinds", "asked_at_in", "asks_in", "queue_ask_in"]
