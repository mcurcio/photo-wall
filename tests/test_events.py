"""Central's event log and the bus store (decision 0034, slice S2c; central/infra/events.py,
central/infra/node_link_store.py): every Pi event, gap and link action is logged once (kept a week:
tests/test_node_record_retention.py); only the newest value per state key is kept; a pipe's
cursors stay its own; the WALL mark never lowers.

Mutation probes (S2c): remove ON CONFLICT DO NOTHING from `record_events_in` -> the repeat raises
(#2); drop the `WHERE` guard from the node_states upsert -> an older seq replayed overwrites the
newer (#1); return every pipe's cursors from `cursors()` -> the fleet store answers the show
stream (#4); write any mark unconditionally -> a lower mark lands (#5).
"""
from __future__ import annotations

import asyncio
import re
import typing

import pytest
from support.devices import seed_device

from central.content_catalog.catalog import device_id_for_serial
from central.infra.events import (
    EventKind,
    GapEvent,
    RecordEvent,
    gap_events_in,
    link_action_in,
    record_events_in,
    refusal_in,
    refusals_in,
)
from central.infra.node_link_store import WALL_MARK, PgLinkStores, PgWallMarks
from contracts.node_link import Pipe
from nodeapi.buffers import KV_OPERATION_HEADER
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Gap, Read

pytestmark = pytest.mark.db

SERIAL = "10000000e0e0e001"
DEVICE = device_id_for_serial(SERIAL)
STATE = "KV_state_display"
RECORDS = "RECORD_display"
SHOW_STATE = "KV_state_content"


def _device(registry) -> None:
    with registry.db.transaction() as conn:
        seed_device(conn, DEVICE, SERIAL)


def _now(conn) -> float:
    return float(conn.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS t").fetchone()["t"])


def _record(seq: int, at: float, epoch: str = "e1") -> RecordEvent:
    return RecordEvent(DEVICE, RECORDS, epoch, seq, "record.shown", f"m-{seq}", {"Nats-Msg-Id": f"m-{seq}"},
                       b"%d" % seq, at)


def test_event_kind_is_the_check_s_list(registry):
    with registry.db.transaction() as conn:
        definition = conn.execute(
            "SELECT pg_get_constraintdef(c.oid) AS d FROM pg_constraint c "
            "WHERE c.conrelid = 'events'::regclass AND c.contype = 'c'").fetchone()["d"]
    assert set(re.findall(r"'([a-z-]+)'::text", definition)) == set(typing.get_args(EventKind))


def test_a_repeated_record_or_gap_is_logged_once_and_not_answered_as_inserted(registry):
    """S2c #2."""
    _device(registry)
    with registry.db.transaction() as conn:
        now = _now(conn)
        assert record_events_in(conn, [_record(1, now), _record(2, now)]) == {("e1", 1), ("e1", 2)}
        assert record_events_in(conn, [_record(2, now), _record(3, now)]) == {("e1", 3)}
        assert record_events_in(conn, []) == frozenset()
        gap_events_in(conn, [GapEvent(DEVICE, RECORDS, "e1", 3, None, now)])
        gap_events_in(conn, [GapEvent(DEVICE, RECORDS, "e1", 3, None, now)])
        link_action_in(conn, DEVICE, Pipe.FLEET, "document-adopted", {"key": "show"})
        rows = conn.execute("SELECT kind, seq, body, recorded_by FROM events WHERE device_id=%s ORDER BY id",
                            (DEVICE,)).fetchall()
    assert [(row["kind"], row["seq"], row["recorded_by"]) for row in rows] == [
        ("record", 1, "node"), ("record", 2, "node"), ("record", 3, "node"), ("gap", 3, "central"),
        ("link-action", None, "central")]
    assert rows[3]["body"] == {"lost": None}
    assert rows[4]["body"] == {"pipe": "fleet", "kind": "document-adopted", "body": {"key": "show"}}


def test_refusals_read_newest_first_by_source(registry):
    with registry.db.transaction() as conn:
        refusal_in(conn, source="pi", address="p1", reason="unreadable")
        refusal_in(conn, source="home-assistant", address="light.a", reason="out_of_range")
        refusal_in(conn, source="pi", address="p2", reason="unreadable")
        assert [(r.source, r.address) for r in refusals_in(conn, "pi", 10)] == [("pi", "p2"), ("pi", "p1")]
        assert [r.address for r in refusals_in(conn, None, 2)] == ["p2", "light.a"]


def _states(registry) -> list[tuple]:
    with registry.db.transaction() as conn:
        return [(row["subject"], row["epoch"], row["seq"], row["removed"], bytes(row["data"]))
                for row in conn.execute("SELECT subject, epoch, seq, removed, data FROM node_states "
                                        "WHERE device_id=%s ORDER BY subject", (DEVICE,)).fetchall()]


def test_the_store_keeps_only_the_newest_value_per_state_key(registry):
    """S2c #1: an older value replayed, or the same one again, changes nothing; a new epoch replaces."""
    _device(registry)
    store = PgLinkStores(registry.db).store(SERIAL, Pipe.FLEET)

    def commit(*reads: Read) -> None:
        asyncio.run(store.commit(STATE, Batch(reads, reads[-1].token, False)))

    a, b = "$KV.state_display.a", "$KV.state_display.b"
    commit(Read(Token("e1", 1), a, {}, b"a1"), Read(Token("e1", 2), b, {}, b"b2"),
           Read(Token("e1", 3), a, {}, b"a3"))
    commit(Read(Token("e1", 3), a, {}, b"a3"))   # the same value again
    commit(Read(Token("e1", 1), a, {}, b"a1"))   # an older value replayed
    assert _states(registry) == [(a, "e1", 3, False, b"a3"), (b, "e1", 2, False, b"b2")]
    commit(Read(Token("e2", 1), b, {KV_OPERATION_HEADER: "DEL"}, b""))
    assert _states(registry) == [(a, "e1", 3, False, b"a3"), (b, "e2", 1, True, b"")]
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM events WHERE kind='record'").fetchone()["n"] == 0


def test_event_reads_and_gaps_of_a_commit_go_to_events(registry):
    _device(registry)
    store = PgLinkStores(registry.db).store(SERIAL, Pipe.FLEET)
    batch = Batch((Gap("e1", 0, 4), Read(Token("e1", 5), "record.shown", {}, b"x")), Token("e1", 5), False)
    asyncio.run(store.commit(RECORDS, batch))
    asyncio.run(store.commit(RECORDS, batch))
    asyncio.run(store.action("call", {"method": "x"}))
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT kind, stream, epoch, seq, body FROM events ORDER BY id").fetchall()
    assert [(r["kind"], r["stream"], r["seq"]) for r in rows] == [
        ("gap", RECORDS, 0), ("record", RECORDS, 5), ("link-action", None, None)]
    assert rows[0]["body"] == {"lost": 4}


def test_each_pipe_answers_only_its_own_cursors(registry):
    """S2c #4: NodeLink.reconcile ends every cursor whose stream it does not see."""
    _device(registry)
    stores = PgLinkStores(registry.db)
    fleet, show = stores.store(SERIAL, Pipe.FLEET), stores.store(SERIAL, Pipe.SHOW)
    asyncio.run(fleet.commit(RECORDS, Batch((), Token("e1", 7), False)))
    asyncio.run(show.commit(SHOW_STATE, Batch((), Token("e2", 3), False)))
    asyncio.run(fleet.commit(STATE, Batch((), Token("e3", 1), False)))
    asyncio.run(fleet.commit(STATE, Batch((), Token("e3", 2), False)))
    assert asyncio.run(fleet.cursors()) == {RECORDS: Token("e1", 7), STATE: Token("e3", 2)}
    assert asyncio.run(show.cursors()) == {SHOW_STATE: Token("e2", 3)}
    asyncio.run(fleet.commit(RECORDS, Batch((), None, False)))     # the stream ended
    assert asyncio.run(fleet.cursors()) == {STATE: Token("e3", 2)}
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT created_by, changed_by, linked FROM node_links WHERE device_id=%s",
                           (DEVICE,)).fetchone()
    assert (row["created_by"], row["changed_by"], row["linked"]) == ("central", "central", None)


def test_own_tokens_are_kept_per_stream_and_key(registry):
    _device(registry)
    store = PgLinkStores(registry.db).store(SERIAL, Pipe.FLEET)
    asyncio.run(store.wrote("KV_desired_display", "show", "d1", Token("e1", 1)))
    asyncio.run(store.wrote("KV_desired_display", "other", "d2", Token("e1", 2)))
    asyncio.run(store.wrote("KV_desired_display", "show", "d3", Token("e1", 3)))
    assert asyncio.run(store.own_tokens("KV_desired_display")) == {
        "show": ("d3", Token("e1", 3)), "other": ("d2", Token("e1", 2))}
    assert asyncio.run(store.own_tokens("KV_desired_content")) == {}


def test_the_wall_mark_never_lowers(registry):
    """S2c #5."""
    marks = PgWallMarks(registry.db, lambda: None)
    assert asyncio.run(marks.mark()) == 0
    asyncio.run(marks.record_mark(5))
    asyncio.run(marks.record_mark(3))
    assert asyncio.run(marks.mark()) == 5
    asyncio.run(marks.record_mark(5))
    asyncio.run(marks.record_mark(9))
    with registry.db.transaction() as conn:
        stored = WALL_MARK.read_in(conn)
    assert stored is not None and (stored.value.mark, stored.revision, stored.changed_by) == (9, 2, "central")
