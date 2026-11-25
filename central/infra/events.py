"""Central's one event log over PostgreSQL (decision 0034, migration 119): the one writer of
`events`, and the home of how long it is kept. Every Pi event record (a `RECORD_*` stream, or any
stream that is not a KV bucket), every gap Central's link counted, every action Central's link took
(nodeapi's action log), every refused room-light report, every change of a Frame's status
(decision 0036) and every everyday action its own row does not tell (a Pause request's answer, a
Show again: `everyday_in`, migration 136) go here, append-only, and are trimmed
past a week (`EVENTS_KEPT_SECONDS`, central/infra/node_record_retention.py's `events` rule).

`EventKind` is the vocabulary's code home; the CHECK on `events.kind` (migration 119, widened by
132 and 136) is its other, and
tests/test_events.py keeps the two equal. Times are the database's clock (`DatabaseTransactionClock`
on the caller's connection, or the stamp a caller read from it), never a Pi's.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from psycopg.types.json import Jsonb

from central.db import DatabaseTransactionClock
from contracts.node_link import Pipe

EventKind = Literal["record", "gap", "link-action", "refusal", "frame-status", "everyday"]
EVENTS_KEPT_SECONDS: Final = 7 * 86400     # decision 0033's week, its one home for `events`

_CLOCK: Final = DatabaseTransactionClock()


@dataclass(frozen=True, slots=True)
class RecordEvent:
    """One record a Pi published on an event stream, keyed (device, stream, epoch, seq)."""
    device_id: str
    stream: str
    epoch: str
    seq: int
    subject: str
    message_id: str | None
    headers: Mapping[str, str]
    data: bytes
    recorded_at: float


@dataclass(frozen=True, slots=True)
class GapEvent:
    """Records Central's link counted lost after `after_seq`; `lost` None: unknown, the epoch ended."""
    device_id: str
    stream: str
    epoch: str
    after_seq: int
    lost: int | None
    recorded_at: float


@dataclass(frozen=True, slots=True)
class RefusalEvent:
    """A report Central refused: from which source and address, and why."""
    source: str
    address: str
    reason: str
    recorded_at: float


def record_events_in(conn: Any, rows: Sequence[RecordEvent]) -> frozenset[tuple[str, int]]:
    """Insert Pi records (kind 'record'), ON CONFLICT DO NOTHING on (device, stream, epoch, seq);
    answers the (epoch, seq) of the rows this call inserted (a repeat is a no-op)."""
    if not rows:
        return frozenset()
    inserted: set[tuple[str, int]] = set()
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO events (kind, device_id, stream, epoch, seq, subject, message_id, headers,"
            " data, recorded_at, recorded_by) VALUES ('record', %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " 'node') ON CONFLICT (device_id, stream, epoch, seq) WHERE kind = 'record' DO NOTHING"
            " RETURNING epoch, seq",
            [(row.device_id, row.stream, row.epoch, row.seq, row.subject, row.message_id,
              Jsonb(dict(row.headers)), row.data, row.recorded_at) for row in rows],
            returning=True)
        while True:
            inserted.update((found["epoch"], found["seq"]) for found in cursor.fetchall())
            if not cursor.nextset():
                break
    return frozenset(inserted)


def gap_events_in(conn: Any, rows: Sequence[GapEvent]) -> None:
    """Insert counted gaps (kind 'gap', seq = after_seq, body {"lost"}), idempotent on
    (device, stream, epoch, after_seq)."""
    if not rows:
        return
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO events (kind, device_id, stream, epoch, seq, body, recorded_at, recorded_by)"
            " VALUES ('gap', %s, %s, %s, %s, %s, %s, 'central')"
            " ON CONFLICT (device_id, stream, epoch, seq) WHERE kind = 'gap' DO NOTHING",
            [(row.device_id, row.stream, row.epoch, row.after_seq, Jsonb({"lost": row.lost}),
              row.recorded_at) for row in rows])


def link_action_in(conn: Any, device_id: str, pipe: Pipe, kind: str, body: Mapping[str, object]) -> None:
    """One action Central's link took on `device_id`'s `pipe` (kind 'link-action', body
    {"pipe", "kind", "body"})."""
    conn.execute(
        "INSERT INTO events (kind, device_id, body, recorded_at, recorded_by)"
        " VALUES ('link-action', %s, %s, %s, 'central')",
        (device_id, Jsonb({"pipe": Pipe(pipe).value, "kind": kind, "body": dict(body)}),
         _CLOCK.now_in(conn)))


def refusal_in(conn: Any, *, source: str, address: str, reason: str) -> None:
    """One refused report (kind 'refusal', no device), body {"source", "address", "reason"}."""
    conn.execute(
        "INSERT INTO events (kind, body, recorded_at, recorded_by) VALUES ('refusal', %s, %s, 'central')",
        (Jsonb({"source": source, "address": address, "reason": reason}), _CLOCK.now_in(conn)))


def refusals_in(conn: Any, source: str | None, limit: int) -> tuple[RefusalEvent, ...]:
    """The newest `limit` refusals, newest first, of `source` or of every source."""
    rows = conn.execute(
        "SELECT body, recorded_at FROM events WHERE kind = 'refusal'"
        " AND (%(source)s::text IS NULL OR body->>'source' = %(source)s)"
        " ORDER BY recorded_at DESC, id DESC LIMIT %(limit)s",
        {"source": source, "limit": limit}).fetchall()
    return tuple(RefusalEvent(source=row["body"]["source"], address=row["body"]["address"],
                              reason=row["body"]["reason"], recorded_at=float(row["recorded_at"]))
                 for row in rows)


@dataclass(frozen=True, slots=True)
class FrameStatusEvent:
    """One change of a Frame's status: the body its writer gave, and when it was recorded."""
    body: Mapping[str, Any]
    recorded_at: float


def frame_status_in(conn: Any, frame_id: str, body: Mapping[str, object]) -> None:
    """One change of `frame_id`'s status (kind 'frame-status', no device), body {"frame_id", **body}."""
    conn.execute(
        "INSERT INTO events (kind, body, recorded_at, recorded_by) VALUES ('frame-status', %s, %s, 'central')",
        (Jsonb({**body, "frame_id": frame_id}), _CLOCK.now_in(conn)))


def frame_statuses_in(conn: Any, frame_id: str, limit: int) -> tuple[FrameStatusEvent, ...]:
    """`frame_id`'s newest `limit` status changes still kept, newest first."""
    rows = conn.execute(
        "SELECT body, recorded_at FROM events WHERE kind = 'frame-status' AND body->>'frame_id' = %s"
        " ORDER BY recorded_at DESC, id DESC LIMIT %s", (frame_id, limit)).fetchall()
    return tuple(FrameStatusEvent(body=row["body"], recorded_at=float(row["recorded_at"])) for row in rows)


__all__ = ["EVENTS_KEPT_SECONDS", "EventKind", "FrameStatusEvent", "GapEvent", "RecordEvent", "RefusalEvent",
           "frame_status_in", "frame_statuses_in", "gap_events_in", "link_action_in", "record_events_in", "refusal_in",
           "refusals_in"]


def everyday_in(conn: Any, action: str, body: Mapping[str, object], *, by: str,
                request_id: str | None = None) -> bool:
    """One everyday action (kind 'everyday', no device), body {"action", "request_id"?, **body},
    `recorded_by` the person who asked (decision 0036). With `request_id`, one per (action,
    request): False, and nothing written, when that request is logged already."""
    found = conn.execute(
        "INSERT INTO events (kind, body, recorded_at, recorded_by) VALUES ('everyday', %s, %s, %s)"
        " ON CONFLICT ((body->>'action'), (body->>'request_id'))"
        " WHERE kind = 'everyday' AND body ? 'request_id' DO NOTHING RETURNING id",
        (Jsonb({**body, "action": action, **({} if request_id is None else {"request_id": request_id})}),
         _CLOCK.now_in(conn), by)).fetchone()
    return found is not None


def everyday_of_in(conn: Any, action: str, request_id: str) -> Mapping[str, Any] | None:
    """The body `everyday_in` logged for `action`'s request `request_id`, None when none is."""
    row = conn.execute(
        "SELECT body FROM events WHERE kind = 'everyday' AND body ? 'request_id'"
        " AND body->>'action' = %s AND body->>'request_id' = %s", (action, request_id)).fetchone()
    return None if row is None else row["body"]
