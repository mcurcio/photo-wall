"""Central's one event log over PostgreSQL (decision 0034, migration 119): the one writer of
`events`, and the home of how long it is kept. Every Pi event record (a `RECORD_*` stream, or any
stream that is not a KV bucket), every gap Central's link counted, every action Central's link took
(nodeapi's action log) and every refused room-light report go here, append-only, and are trimmed
past a week (`EVENTS_KEPT_SECONDS`, central/infra/node_record_retention.py's `events` rule).

`EventKind` is the vocabulary's code home; migration 119's CHECK is its other, and
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

EventKind = Literal["record", "gap", "link-action", "refusal"]
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


__all__ = ["EVENTS_KEPT_SECONDS", "EventKind", "GapEvent", "RecordEvent", "RefusalEvent",
           "gap_events_in", "link_action_in", "record_events_in", "refusal_in", "refusals_in"]
