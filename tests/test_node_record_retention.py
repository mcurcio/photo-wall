"""Node records are kept a week and trimmed in whole batches, outside the coordinator's locks
(decision 0033, central/infra/node_record_retention.py).

The records are seeded beside a real Node (`rig`: its producers, sessions and Player), with
`received_at` on the clock that stamps them; the rig's own records are younger than the cut."""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_node_runtime_reconciliation import rig

from central.infra.node_record_retention import KEPT_SECONDS, NodeRecordRetention
from central.transaction_locks import acquire_runtime_locks
from contracts.time import ManualClock

pytestmark = pytest.mark.db

KEPT = 100.0
NOW = 1000.0
OLD = NOW - KEPT - 500  # every seeded old record is received before the cut (NOW - KEPT)
YOUNG = NOW - KEPT / 2


def _evidence(conn, producer, kind, received_at, *, completed=True, sequence=None):
    evidence_id = uuid4()
    conn.execute("INSERT INTO node_evidence(producer_id,evidence_id,kind,sequence,payload,"
                 "received_at,disposition) VALUES(%s,%s,%s,%s,'\\x00',%s,'applied')",
                 (producer, evidence_id, kind, sequence or 0, received_at))
    conn.execute("INSERT INTO node_reconciliation_work(producer_id,evidence_id,kind,received_at,"
                 "completed_at,result) VALUES(%s,%s,%s,%s,%s,%s)",
                 (producer, evidence_id, kind, received_at, received_at if completed else None,
                  "historical" if completed else None))
    return evidence_id


def _exchange(conn, producer, session, received_at):
    decision = uuid4()
    conn.execute("INSERT INTO node_display_exchanges(producer_id,request_id,session_id,output_id,"
                 "sampled_boottime_ms,request,response,decision_id,received_at) "
                 "VALUES(%s,%s,%s,'HDMI-A-9',%s,'\\x00','\\x00',%s,%s)",
                 (producer, uuid4(), session, int(received_at * 1000), decision, received_at))
    return decision


def _observation(conn, table, producer, sequence, received_at):
    conn.execute(f"INSERT INTO {table}(producer_id,sequence,payload,received_at) "  # noqa: S608
                 "VALUES(%s,%s,'\\x00',%s)", (producer, sequence, received_at))


def _count(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()["n"]


def test_each_table_is_trimmed_in_whole_batches_and_keeps_what_central_reads(registry):
    """Old records go in whole batches of `batch_rows`, each table's rule in its own
    transactions, while another transaction holds the Coordination and Runtime locks; a table
    one short of a batch is left as it is; unfinished work, work an Output loss names and every
    event's evidence stay at any age, and a handoff and a withdrawal go with their exchange.
    Mutation probes: drop the unfinished-work guard (the unfinished work goes in the first
    batch); drop the whole-batch count (the second pass deletes the three left over)."""
    _, player, _, _, _, grants, _, _, _ = rig(registry)
    display = grants["display_host"]
    with registry.db.transaction() as conn:
        producers = {row["owner"]: row["producer_id"] for row in conn.execute(
            "SELECT owner,producer_id FROM node_producers WHERE owner IN "
            "('host_core','display_host')")}
        host_producer, display_producer = producers["host_core"], producers["display_host"]
        rig_counts = {table: _count(conn, f"SELECT count(*) n FROM {table}")  # noqa: S608
                      for table in ("node_evidence", "node_reconciliation_work",
                                    "node_display_exchanges", "node_host_observations",
                                    "node_manager_observations")}
        assert all(row["received_at"] >= NOW for row in conn.execute(
            "SELECT received_at FROM node_reconciliation_work"))  # the rig's are all young
        # The oldest first: what a dropped guard would put in the first batch.
        unfinished = _evidence(conn, host_producer, "snapshot", OLD - 30, completed=False)
        named = _evidence(conn, host_producer, "snapshot", OLD - 20)
        conn.execute("INSERT INTO node_output_losses(player_id,authority_epoch,output_id,frame_id,"
                     "binding_generation,configuration_revision,cause_producer_id,"
                     "cause_evidence_id,cause_kind,detail,interrupted_at) "
                     "VALUES(%s,1,'HDMI-A-9','node-f0',1,1,%s,%s,'snapshot',%s,%s)",
                     (player["player_id"], host_producer, named, Jsonb({}), OLD))
        event = _evidence(conn, host_producer, "event", OLD - 10, sequence=900)
        finished = [_evidence(conn, host_producer, "snapshot", OLD + index) for index in range(12)]
        young = _evidence(conn, host_producer, "snapshot", YOUNG)
        handed, withdrawn = (_exchange(conn, display_producer, display.session_id, OLD - 2),
                             _exchange(conn, display_producer, display.session_id, OLD - 1))
        conn.execute("INSERT INTO node_display_handoffs VALUES(%s,%s,%s)", (handed, uuid4(), OLD))
        conn.execute("INSERT INTO node_display_withdrawals VALUES(%s,%s,%s)",
                     (withdrawn, uuid4(), OLD))
        for index in range(5):
            _exchange(conn, display_producer, display.session_id, OLD + index)
        _exchange(conn, display_producer, display.session_id, YOUNG)
        for index in range(6):
            _observation(conn, "node_host_observations", host_producer, 900 + index, OLD + index)
        for index in range(4):
            _observation(conn, "node_manager_observations", host_producer, 900 + index,
                         OLD + index)

    held, release = threading.Event(), threading.Event()

    def hold_the_coordinator_locks():
        with registry.db.transaction() as conn:
            acquire_runtime_locks(conn)
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold_the_coordinator_locks)
    holder.start()
    assert held.wait(10)
    retention = NodeRecordRetention(registry.db.transaction, ManualClock(NOW), kept_seconds=KEPT,
                                    batch_rows=5, batches=2)
    try:
        first = retention.prune()
        second = retention.prune()
    finally:
        release.set()
        holder.join(30)

    # Work: the event and 12 finished are due (13); two batches take 10, then 3 are short.
    assert first == {"node_reconciliation_work": 10, "node_display_exchanges": 5,
                     "node_host_observations": 5, "node_manager_observations": 0}
    assert second == dict.fromkeys(first, 0)
    with registry.db.transaction() as conn:
        work = {row["evidence_id"] for row in conn.execute(
            "SELECT evidence_id FROM node_reconciliation_work WHERE received_at < %s", (NOW,))}
        assert work == {unfinished, named, *finished[9:], young}
        evidence = {row["evidence_id"] for row in conn.execute(
            "SELECT evidence_id FROM node_evidence WHERE received_at < %s", (NOW,))}
        assert evidence == {unfinished, named, event, *finished[9:], young}
        assert _count(conn, "SELECT count(*) n FROM node_display_handoffs") == 0
        assert _count(conn, "SELECT count(*) n FROM node_display_withdrawals") == 0
        assert _count(conn, "SELECT count(*) n FROM node_display_exchanges WHERE received_at < %s",
                      (NOW,)) == 3  # two old exchanges short of a batch, and the young one
        assert _count(conn, "SELECT count(*) n FROM node_host_observations WHERE received_at < %s",
                      (NOW,)) == 1
        assert _count(conn, "SELECT count(*) n FROM node_manager_observations "
                            "WHERE received_at < %s", (NOW,)) == 4
        for table, before in rig_counts.items():  # the rig's young records all stay
            assert _count(conn, f"SELECT count(*) n FROM {table} "  # noqa: S608
                                "WHERE received_at >= %s", (NOW,)) == before


def test_a_week_is_kept_and_a_bad_bound_is_refused(registry):
    assert KEPT_SECONDS == 7 * 86400
    for bad in ({"kept_seconds": -1}, {"batch_rows": 0}, {"batches": 0}):
        with pytest.raises(ValueError):
            NodeRecordRetention(registry.db.transaction, ManualClock(NOW), **bad)
