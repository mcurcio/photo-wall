"""Runtime reads and scheduler time cuts serialize with current-state writers."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from threading import Event

import pytest
from fastapi.testclient import TestClient
from support.house_time import UTC_HOUSE, daily, set_house, window_program
from test_registry import ADMIN

from central.app import create_app
from central.coordination import COORDINATION_LOCK, Coordinator
from central.runtime import Contribution, ProgramDraft, Scene
from central.runtime_store import RUNTIME_LOCK, RuntimeStore
from central.transaction_locks import acquire_runtime_locks


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
        def notified_edit(conn=None, *, by):
            entering.set()
            with original(conn, by=by) as runtime:
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
        advance = pending.result(timeout=4)

    assert advance.failures == ()
    assert advance.projection.now == 1005
    assert coordinator.runtime.read().export_state()["now"] == 1005


def test_runtime_read_waits_for_writer_then_returns_committed_state(registry):
    store = RuntimeStore(registry.db, registry.clock)
    entering = Event()

    def read_after_signal():
        entering.set()
        return store.read()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with store.edit(by="console") as runtime:
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
        store.command("set_scene", Scene(scene_id="serialized-command", loop=True), by="console")

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


def test_operator_activation_uses_post_lock_time(registry, monkeypatch):
    store = RuntimeStore(registry.db, registry.clock)
    store.command("set_scene", Scene(
        scene_id="manual", loop=True,
        contributions=(Contribution(target="frame:left", kind="black"),),
    ), by="console")
    entering = Event()
    original = RuntimeStore.edit

    @contextmanager
    def notified_edit(self, conn=None, *, by):
        entering.set()
        with original(self, conn, by=by) as runtime:
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
        response = pending.result(timeout=4)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "admitted"
    state = store.read().export_state()
    assert state["now"] == 1005
    assert [run["started_at"] for run in state["runs"].values()] == [1005]


def test_program_save_applies_at_the_clock_after_lock_wait(registry, monkeypatch):
    """A save waits for the Runtime lock and applies at the clock read after it: the window that
    started while it waited keeps its end (R10), so the save applies from the next window."""
    set_house(registry.db, UTC_HOUSE)
    store = RuntimeStore(registry.db, registry.clock)
    store.command("set_scene", Scene(scene_id="scheduled", loop=True), by="console")
    registry.clock.advance(17)                                     # 1017: the window starts at 1020
    expected = store.command_current("create_program", window_program("upcoming", "scheduled", 1020, 1080), by="console")
    draft = ProgramDraft(name="upcoming", scene_id="scheduled", when=daily("00:17", "00:19"))
    entering = Event()
    original = store.edit

    @contextmanager
    def notified_edit(conn=None, *, by):
        entering.set()
        with original(conn, by=by) as runtime:
            yield runtime

    monkeypatch.setattr(store, "edit", notified_edit)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
            pending = workers.submit(store.command_current, "save_program", expected, draft,
                                     by="console")
            assert entering.wait(4)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.1)
            registry.clock.advance(5)
        assert pending.result(timeout=4).revision == 2

    state = store.read().export_state()
    assert state["now"] == 1022
    (run,) = state["runs"].values()
    assert (run["started_at"], run["program_ends_at"]) == (1020, 1080)    # the old rule's window
    assert state["program_from"]["upcoming"] == 1080
