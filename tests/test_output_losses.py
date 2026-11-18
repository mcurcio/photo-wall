"""An Output's loss, judged from the Node's `display` line (decision 0034, S4b part 2).

Each test drives Central's real `OutputReportJudge` with the worker's `OutputLossLedger`
(`support.output_report.report_modes`, the reads a hub's link store commits) and reads what the
Runtime serves (`Coordinator.output_interruptions_in`). Lock order: a report that changes no bound
Output's connection takes no fleet-wide lock (issue #127); one that does takes Coordination and
Runtime before the Node's `players` row.
"""
from __future__ import annotations

import threading
import time

import psycopg
import pytest
from psycopg.rows import dict_row
from support.database import waiting_backends
from support.output_report import report_modes
from test_registry import enroll, frame

from central.coordination import Coordinator, OutputLossLedger
from central.transaction_locks import COORDINATION_LOCK, acquire_runtime_locks

DEVICE = "node-losses"
OUTPUT = "HDMI-A-1"
MODE = (1920, 1080)


def _bound(registry, frame_id="lost-frame", output_id=OUTPUT):
    """An enrolled Node whose Outputs report connected, with `frame_id` bound to `output_id`."""
    player, _, _ = enroll(registry, device_id=DEVICE, size=MODE)
    frame(registry, frame_id)
    registry.bind(frame_id, player["player_id"], output_id, expected_generation=0, by="console")
    return player["player_id"]


def _disconnect(registry, output_id=OUTPUT):
    report_modes(registry, DEVICE, (output_id,), None)


def _connect(registry, output_id=OUTPUT):
    report_modes(registry, DEVICE, (output_id,), MODE)


def _losses(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT frame_id,cause_layer,cause_kind,resolved_at,changed_at FROM output_losses "
                            "ORDER BY frame_id,interrupted_at").fetchall()


def _served(registry):
    with registry.db.transaction() as conn:
        return [(row.frame_id, row.output_id, row.cause_layer) for row in Coordinator.output_interruptions_in(conn)]


def test_a_bound_output_reported_disconnected_is_lost_once_and_restored_when_connected(registry):
    _bound(registry)
    _disconnect(registry)
    [loss] = _losses(registry)
    assert (loss["frame_id"], loss["cause_layer"], loss["cause_kind"], loss["resolved_at"]) == (
        "lost-frame", "display_host", "output_disconnected", None)
    assert _served(registry) == [("lost-frame", OUTPUT, "display_host")]
    _disconnect(registry)   # the same report again: no new sighting, no second loss
    assert _losses(registry) == [loss]
    _connect(registry)
    [restored] = _losses(registry)
    assert restored["resolved_at"] is not None
    assert _served(registry) == []


def test_the_ledger_judges_a_loss_once(registry):
    """The ledger's own rule, beside the judge's: an unresolved loss of the bound Frame is never
    written again (its `changed_at` stands), and resolving needs one."""
    player_id = _bound(registry)
    ledger = OutputLossLedger()
    args = dict(player_id=player_id, output_id=OUTPUT, cause_layer="display_host",
                cause_kind="output_disconnected", detail={})
    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        assert ledger.lost_in(conn, **args, at=100.0)
    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        assert not ledger.lost_in(conn, **args, at=200.0)
        assert ledger.restored_in(conn, player_id=player_id, output_id=OUTPUT, at=300.0)
        assert not ledger.restored_in(conn, player_id=player_id, output_id=OUTPUT, at=400.0)
    [loss] = _losses(registry)
    assert (loss["changed_at"], loss["resolved_at"]) == (300.0, 300.0)


def test_the_ledger_needs_the_runtime_locks(registry):
    player_id = _bound(registry)
    with registry.db.transaction() as conn, pytest.raises(ValueError, match="output_loss_runtime_locks_required"):
        OutputLossLedger().restored_in(conn, player_id=player_id, output_id=OUTPUT, at=1.0)


def test_an_unbound_outputs_disconnect_writes_no_loss(registry):
    _bound(registry)
    _disconnect(registry, "HDMI-A-2")   # connected, but no Frame bound there
    assert _losses(registry) == []


def test_a_rebind_while_disconnected_leaves_no_loss_behind(registry):
    """A loss names the binding it fenced; a Frame bound to the Output meanwhile is not fenced by
    it, and the Output's reconnection resolves it whatever Frame it names."""
    player_id = _bound(registry)
    _disconnect(registry)
    registry.unbind("lost-frame", expected_generation=1, by="console")
    frame(registry, "replacement")
    registry.bind("replacement", player_id, OUTPUT, expected_generation=0, by="console")
    assert [row["frame_id"] for row in _losses(registry)] == ["lost-frame"]
    assert _served(registry) == []   # the replacement inherits nothing
    _connect(registry)
    assert [row["resolved_at"] is not None for row in _losses(registry)] == [True]


def _holding_coordination(registry):
    holder = psycopg.connect(registry.db.dsn, row_factory=dict_row)
    holder.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
    return holder


def _waiting_on_coordination(registry, deadline=5.0):
    """Whether a backend of this test's database waits on Coordination, within `deadline` seconds."""
    end = time.monotonic() + deadline
    with psycopg.connect(registry.db.dsn, autocommit=True) as probe:
        while time.monotonic() < end:
            if waiting_backends(probe, advisory_lock=COORDINATION_LOCK):
                return True
            time.sleep(0.05)
    return False


def _in_thread(body):
    errors: list[BaseException] = []

    def run():
        try:
            body()
        except BaseException as error:   # handed to the test thread
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def test_a_report_changing_no_connection_completes_while_coordination_is_held(registry):
    _bound(registry)
    holder = _holding_coordination(registry)
    try:
        started = time.monotonic()
        report_modes(registry, DEVICE, (OUTPUT, "HDMI-A-2"), (1280, 720))   # a new mode, still connected
        assert time.monotonic() - started < 2
    finally:
        holder.close()
    assert _losses(registry) == []


def test_a_disconnect_waits_for_coordination_then_writes_the_loss(registry):
    _bound(registry)
    holder = _holding_coordination(registry)
    try:
        thread, errors = _in_thread(lambda: _disconnect(registry))
        assert _waiting_on_coordination(registry)
        assert _losses(registry) == []
    finally:
        holder.close()
    thread.join(10)
    assert not thread.is_alive() and errors == []
    assert [row["frame_id"] for row in _losses(registry)] == ["lost-frame"]


def test_the_advisory_locks_precede_the_players_row(registry):
    """A Coordinator-ordered transaction (Coordination, then the Node's `players` row) beside the
    judge on a disconnect: the judge waits on Coordination holding no row, so both finish."""
    _bound(registry)
    holder = _holding_coordination(registry)
    try:
        thread, errors = _in_thread(lambda: _disconnect(registry))
        assert _waiting_on_coordination(registry)
        holder.execute("SET LOCAL lock_timeout='3s'")
        holder.execute("SELECT id FROM players WHERE device_id=%s FOR SHARE", (DEVICE,))
        holder.commit()
    finally:
        holder.close()
    thread.join(10)
    assert not thread.is_alive() and errors == []
    assert [row["frame_id"] for row in _losses(registry)] == ["lost-frame"]
