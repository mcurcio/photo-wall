"""Node records are kept a week and trimmed in whole batches, outside the coordinator's locks
(decisions 0033 and 0034, central/infra/node_record_retention.py). Every Node record is an event
(`events`), stamped by the database's clock, so the week is that clock's."""

from __future__ import annotations

import threading

import pytest
from support.devices import seed_device

from central.content_catalog.catalog import device_id_for_serial
from central.infra.events import EVENTS_KEPT_SECONDS, RecordEvent, record_events_in
from central.infra.node_record_retention import NodeRecordRetention
from central.transaction_locks import acquire_runtime_locks

pytestmark = pytest.mark.db

SERIAL = "10000000e0e0e002"
DEVICE = device_id_for_serial(SERIAL)


def _record(seq: int, at: float) -> RecordEvent:
    return RecordEvent(DEVICE, "RECORD_host", "e1", seq, "record.reboot", f"m-{seq}", {}, b"%d" % seq, at)


def test_events_past_a_week_go_in_whole_batches_while_the_coordinator_holds_its_locks(registry):
    """Old events go oldest first in whole batches of `batch_rows`, while another transaction
    holds the Coordination and Runtime locks; one short of a batch stays; an event of the week
    stays. `events` is the only rule. Mutation probes: remove the `events` rule (nothing is
    trimmed, and the result has no `events` key); drop the whole-batch count (the second pass
    deletes the one left over)."""
    with registry.db.transaction() as conn:
        seed_device(conn, DEVICE, SERIAL)
        now = float(conn.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS t").fetchone()["t"])
        old = now - EVENTS_KEPT_SECONDS - 600
        record_events_in(conn, [_record(seq, old + seq) for seq in range(1, 6)])
        record_events_in(conn, [_record(9, now - EVENTS_KEPT_SECONDS + 600)])

    held, release = threading.Event(), threading.Event()

    def hold_the_coordinator_locks():
        with registry.db.transaction() as conn:
            acquire_runtime_locks(conn)
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold_the_coordinator_locks)
    holder.start()
    assert held.wait(10)
    retention = NodeRecordRetention(registry.db.transaction, batch_rows=2, batches=4)
    try:
        first = retention.prune()
        second = retention.prune()
    finally:
        release.set()
        holder.join(30)

    assert first == {"events": 4}               # two whole batches; the fifth old one is short of one
    assert second == {"events": 0}
    with registry.db.transaction() as conn:
        left = [row["seq"] for row in conn.execute(
            "SELECT seq FROM events WHERE device_id=%s ORDER BY seq", (DEVICE,)).fetchall()]
    assert left == [5, 9]                       # the oldest went first; the week's one stays


def test_a_bad_bound_is_refused(registry):
    assert EVENTS_KEPT_SECONDS == 7 * 86400
    for bad in ({"batch_rows": 0}, {"batches": 0}):
        with pytest.raises(ValueError):
            NodeRecordRetention(registry.db.transaction, **bad)
