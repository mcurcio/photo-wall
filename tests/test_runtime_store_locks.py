"""Runtime reads and scheduler time cuts serialize with current-state writers."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from threading import Event

import pytest
from fastapi.testclient import TestClient
from test_registry import ADMIN, enroll, frame

from central.app import create_app
from central.coordination import COORDINATION_LOCK, Coordinator
from central.equipment_drain import EquipmentDrain
from central.runtime import Contribution, Program, RuntimeConflict, Scene
from central.runtime_store import RUNTIME_LOCK, RuntimeStore
from central.transaction_locks import acquire_runtime_locks
from contracts.models import Calibration


@pytest.mark.parametrize("held_lock", [COORDINATION_LOCK, RUNTIME_LOCK])
def test_advance_samples_clock_after_both_serialization_locks(registry, monkeypatch, held_lock):
    coordinator = Coordinator(registry.db, registry.clock)
    entering = Event()
    if held_lock == COORDINATION_LOCK:
        original = coordinator._transaction

        @contextmanager
        def notified_transaction():
            entering.set()
            with original() as conn:
                yield conn

        monkeypatch.setattr(coordinator, "_transaction", notified_transaction)
    else:
        original = coordinator.runtime.edit

        @contextmanager
        def notified_edit(conn=None):
            entering.set()
            with original(conn) as runtime:
                yield runtime

        monkeypatch.setattr(coordinator.runtime, "edit", notified_edit)

    with ThreadPoolExecutor(max_workers=1) as workers:
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (held_lock,))
            pending = workers.submit(coordinator.advance)
            assert entering.wait(4)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.1)
            registry.clock.advance(5)
        projection = pending.result(timeout=4)

    assert projection.now == 1005
    assert coordinator.runtime.read().export_state()["now"] == 1005


def test_runtime_read_waits_for_writer_then_returns_committed_state(registry):
    store = RuntimeStore(registry.db, registry.clock)
    entering = Event()

    def read_after_signal():
        entering.set()
        return store.read()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with store.edit() as runtime:
            runtime.set_scene(Scene(scene_id="written-under-lock", loop=True))
            pending = workers.submit(read_after_signal)
            assert entering.wait(4)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.1)
        observed = pending.result(timeout=4)

    assert "written-under-lock" in observed.export_state()["scenes"]
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT revision FROM runtime_state WHERE singleton").fetchone()[
            "revision"
        ] == 1


def test_direct_runtime_command_waits_for_coordination_lock(registry):
    store = RuntimeStore(registry.db, registry.clock)
    entering = Event()

    def write_after_signal():
        entering.set()
        store.command("set_scene", Scene(scene_id="serialized-command", loop=True))

    with ThreadPoolExecutor(max_workers=1) as workers:
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
            pending = workers.submit(write_after_signal)
            assert entering.wait(4)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.1)
            assert store.read_in(blocker).export_state()["scenes"] == {}
        pending.result(timeout=4)

    assert "serialized-command" in store.read().export_state()["scenes"]


def test_operator_activation_uses_post_lock_time_after_drain_cut(registry, monkeypatch):
    player, _, request = enroll(registry)
    frame(registry, "drained")
    registry.bind("drained", player["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate("drained", "commit", 1, Calibration(), expected_generation=1)
    store = RuntimeStore(registry.db, registry.clock)
    store.command("set_scene", Scene(
        scene_id="manual", loop=True,
        contributions=(Contribution(target="frame:drained", kind="black"),),
    ))
    entering = Event()
    original = RuntimeStore.edit

    @contextmanager
    def notified_edit(self, conn=None):
        if conn is None:
            entering.set()
        with original(self, conn) as runtime:
            yield runtime

    monkeypatch.setattr(RuntimeStore, "edit", notified_edit)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as workers:
        with registry.db.transaction() as blocker:
            acquire_runtime_locks(blocker)
            pending = workers.submit(
                client.post, "/v1/operator/activations",
                json={"scene_id": "manual", "activation_id": "after-cut"},
                headers={"Authorization": "Bearer " + ADMIN},
            )
            assert entering.wait(4)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.1)
            registry.clock.advance(5)
            EquipmentDrain(Coordinator(registry.db, registry.clock))._prepare_idle_in(
                blocker, player["player_id"], "attempt-after-cut", request.boot_id,
                player["authority_epoch"], authorization_expires_at=1010,
                require_unbound=False,
            )
        response = pending.result(timeout=4)

    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["reason"]) == (
        "rejected", "equipment_draining"
    )
    state = store.read().export_state()
    assert state["now"] == 1005
    assert state["runs"] == {}


def test_program_replace_checks_window_after_lock_wait(registry, monkeypatch):
    store = RuntimeStore(registry.db, registry.clock)
    store.command("set_scene", Scene(scene_id="scheduled"))
    expected = Program(
        program_id="upcoming", scene_id="scheduled", starts_at=1003, ends_at=1020,
    )
    replacement = expected.model_copy(update={"starts_at": 1010})
    store.command("set_program", expected)
    entering = Event()
    original = store.edit

    @contextmanager
    def notified_edit(conn=None):
        entering.set()
        with original(conn) as runtime:
            yield runtime

    monkeypatch.setattr(store, "edit", notified_edit)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
            pending = workers.submit(
                store.command_current, "replace_program", expected, replacement,
            )
            assert entering.wait(4)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.1)
            registry.clock.advance(5)
        with pytest.raises(RuntimeConflict, match="program_started"):
            pending.result(timeout=4)

    assert store.read().export_state()["programs"][expected.program_id]["starts_at"] == 1003
