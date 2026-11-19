"""Node records are kept a week and trimmed in whole batches, outside the coordinator's locks
(decision 0033, central/infra/node_record_retention.py).

The records are seeded beside a real Node (`rig`: its producers, sessions and Player), with
`received_at` on the clock that stamps them; the rig's own records are younger than the cut."""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest
from test_fleet_attempts import DEVICE_ID, SERIAL
from test_node_boot import boot_service, claim_for
from test_node_runtime_reconciliation import rig

from central.fleet.node_display import RECENT_EXCHANGES, display_outputs_in
from central.infra.node_record_retention import KEPT_SECONDS, PREPARATION_KEYS, NodeRecordRetention
from central.transaction_locks import acquire_runtime_locks
from contracts.node_boot import NodeBootRequestV2
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


def _exchange(conn, producer, session, received_at, *, sample_ms=None):
    decision = uuid4()
    conn.execute("INSERT INTO node_display_exchanges(producer_id,request_id,session_id,output_id,"
                 "sampled_boottime_ms,request,response,decision_id,received_at) "
                 "VALUES(%s,%s,%s,'HDMI-A-9',%s,'\\x00','\\x00',%s,%s)",
                 (producer, uuid4(), session,
                  int(received_at * 1000) if sample_ms is None else sample_ms, decision,
                  received_at))
    return decision


def _observation(conn, table, producer, sequence, received_at):
    conn.execute(f"INSERT INTO {table}(producer_id,sequence,payload,received_at) "  # noqa: S608
                 "VALUES(%s,%s,'\\x00',%s)", (producer, sequence, received_at))


def _count(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()["n"]


def _reboot(sessions):
    """The rig's Pi boots again, a second later: a new admission supersedes its current one
    (the later supersession is the previous boot)."""
    sessions.clock.advance(1)
    offer = boot_service(sessions).offer(NodeBootRequestV2(SERIAL, uuid4(), uuid4().hex * 2))
    sessions.enroll(claim_for(offer))


def test_each_table_is_trimmed_in_whole_batches_and_keeps_what_central_reads(registry):
    """Old records go in whole batches of `batch_rows`, each table's rule in its own
    transactions, while another transaction holds the Coordination and Runtime locks; a table
    one short of a batch is left as it is; unfinished work and every
    event's evidence stay at any age, and a handoff and a withdrawal go with their exchange.
    Mutation probes: drop the unfinished-work guard (the unfinished work goes in the first
    batch); drop the whole-batch count (the second pass deletes the three left over). The
    records are of a boot that is neither the Pi's current nor its previous one, which keeps
    no newest record past the week (the next test)."""
    _, player, _, sessions, _, grants, _, _, _ = rig(registry)
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
    # The rig's boot becomes neither its Pi's current nor its previous one, so no newest
    # record of it is kept past the week.
    _reboot(sessions)
    _reboot(sessions)

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
                     "node_host_observations": 5, "node_manager_observations": 0, "events": 0}
    assert second == dict.fromkeys(first, 0)
    with registry.db.transaction() as conn:
        work = {row["evidence_id"] for row in conn.execute(
            "SELECT evidence_id FROM node_reconciliation_work WHERE received_at < %s", (NOW,))}
        assert work == {unfinished, *finished[9:], young}
        evidence = {row["evidence_id"] for row in conn.execute(
            "SELECT evidence_id FROM node_evidence WHERE received_at < %s", (NOW,))}
        assert evidence == {unfinished, event, *finished[9:], young}
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


def test_a_pi_silent_for_over_a_week_keeps_its_newest_sample_and_its_outputs(registry):
    """A Pi whose current boot has been silent past the week keeps, at any age, what Central's
    readers show of it: each producer's newest host sample, its newest 8 App Manager samples
    and each Output's newest 128 exchanges, so the device page still reads "last heard" and
    its Outputs. After the next boot its previous boot keeps its samples and loses its
    exchanges; after one more it keeps nothing past the week. Mutation probes: drop the kept
    boot guard (the silent Pi's newest sample goes); drop the previous boot from it (the
    previous boot's last sample goes)."""
    _, _, _, sessions, _, grants, _, _, _ = rig(registry)
    display = grants["display_host"]
    silent = NOW - KEPT - 8 * 86400   # the whole boot's records: received past the cut
    with registry.db.transaction() as conn:
        producers = {row["owner"]: row["producer_id"] for row in conn.execute(
            "SELECT owner,producer_id FROM node_producers WHERE owner IN "
            "('host_core','display_host')")}
        host, screen = producers["host_core"], producers["display_host"]
        for table in ("node_host_observations", "node_manager_observations",
                      "node_display_exchanges"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608  the rig's young records
        for index in range(10):
            _observation(conn, "node_host_observations", host, 900 + index, silent + index)
            _observation(conn, "node_manager_observations", host, 900 + index, silent + index)
        for index in range(RECENT_EXCHANGES + 2):
            _exchange(conn, screen, display.session_id, silent + index, sample_ms=index)
    retention = NodeRecordRetention(registry.db.transaction, ManualClock(NOW), kept_seconds=KEPT,
                                    batch_rows=1, batches=RECENT_EXCHANGES + 10)

    def kept():
        with registry.db.transaction() as conn:
            return {table: sorted(row["n"] for row in conn.execute(
                f"SELECT {order} AS n FROM {table}").fetchall())  # noqa: S608
                for table, order in (("node_host_observations", "sequence"),
                                     ("node_manager_observations", "sequence"),
                                     ("node_display_exchanges", "sampled_boottime_ms"))}

    assert retention.prune() == {"node_reconciliation_work": 0, "node_display_exchanges": 2,
                                 "node_host_observations": 9, "node_manager_observations": 2,
                                 "events": 0}
    assert kept() == {"node_host_observations": [909],
                      "node_manager_observations": list(range(910 - PREPARATION_KEYS, 910)),
                      "node_display_exchanges": list(range(2, RECENT_EXCHANGES + 2))}
    with registry.db.transaction() as conn:
        generation = conn.execute("SELECT device_generation AS n FROM node_boot_admissions "
                                  "WHERE device_id=%s LIMIT 1", (DEVICE_ID,)).fetchone()["n"]
        outputs = display_outputs_in(conn, DEVICE_ID, generation)
    assert [output["output_id"] for output in outputs] == ["HDMI-A-9"]
    assert retention.prune() == dict.fromkeys(retention.prune(), 0)

    _reboot(sessions)   # the silent boot is now the previous one: its samples stay
    retention.prune()
    assert kept() == {"node_host_observations": [909],
                      "node_manager_observations": list(range(910 - PREPARATION_KEYS, 910)),
                      "node_display_exchanges": []}
    _reboot(sessions)   # neither current nor previous: nothing of it past the week
    retention.prune()
    assert kept() == {"node_host_observations": [], "node_manager_observations": [],
                      "node_display_exchanges": []}
