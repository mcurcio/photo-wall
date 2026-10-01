"""Crash and authority-boundary acceptance for the online base effect owner."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_node_boot import environment
from test_node_linux_adapters import store

from appliance.node.broker import RunningApp
from appliance.node.online_broker import OnlineEffectBroker
from appliance.node.stop_operation import StopCompleted, StopView
from contracts.node_lifecycle import (
    StageCommandV2,
    StopPermitV2,
    parse_app_effect_event,
    ready_digest,
    stage_digest,
)
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2


class Driver:
    def __init__(self, old):
        self.running = old
        self.stop_calls = 0
        self.starts = []
        self.target_fails = False
        self.unknown_stop = False

    def current(self):
        return self.running

    def verify(self, reference):
        return True

    def select(self, reference):
        self.selected = reference

    def stop(self, request, *, reattach_only):
        if not reattach_only:
            assert request.old == self.running
            self.stop_calls += 1
        if self.unknown_stop:
            return StopView(request, lambda: {"completed_ms": None, "fault": None})
        self.running = None
        return SimpleNamespace(done=lambda: True, result=lambda: StopCompleted(request, 1001))

    def service(self, **kwargs):
        pass

    def quiescent(self, expected):
        return self.running == expected

    def absent_and_quiescent(self):
        return self.running is None

    def start(self, reference, operation_id):
        self.starts.append(reference)
        if self.target_fails and len(self.starts) == 1:
            raise OSError("target exited")
        self.running = RunningApp(reference, NodeProcessIdentity(500 + len(self.starts), 999, uuid4()),
                                  1 + len(self.starts), operation_id)
        return self.running


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import appliance.node.online_broker as module
    monkeypatch.setattr(module, "boottime_ms", lambda: 1000)
    monkeypatch.setattr(module, "memory_values", lambda: (8 * 1024**3, 7 * 1024**3))
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    old = RunningApp(environment("a"), NodeProcessIdentity(100, 200, uuid4()), 1, uuid4())
    command = StageCommandV2(uuid4(), uuid4(), "0" * 64, producer, uuid4(), uuid4(), old.process,
        old.app_epoch, old.environment, environment("b"), old.environment, 50000)
    command = replace(command, command_sha256=stage_digest(command))
    journal = store(tmp_path, producer.kernel_boot_id)
    transport = SimpleNamespace(request=lambda *args, **kwargs: (200, b'{}'))
    session = SimpleNamespace(grant=SimpleNamespace(producer=producer, session_id=command.command_session_id,
        offer_id=command.offer_id, expires_boottime_ms=60000), claim=None, transport=transport)
    driver = Driver(old)
    broker = OnlineEffectBroker(journal, driver, session, SimpleNamespace(arm=lambda x: x.receipt, advance=lambda *a: None))
    broker.accept(command)
    ready = broker.ready()
    permit = StopPermitV2(producer, command.operation_id, command.command_id, command.command_sha256,
        command.command_session_id, uuid4(), uuid4(), ready_digest(ready), old.process, old.app_epoch, 1000, 2000)
    yield broker, driver, command, permit, module
    journal.close()


def test_stage_and_readiness_have_no_effect_then_exact_permit_switches(setup):
    broker, driver, command, permit, _ = setup
    assert driver.stop_calls == 0 and driver.starts == []
    with pytest.raises(ValueError, match="permit_binding"):
        broker.execute(replace(permit, command_session_id=uuid4()))
    assert driver.stop_calls == 0
    broker.execute(permit)
    assert driver.stop_calls == 1 and driver.starts == [command.target]
    events = [parse_app_effect_event(raw.encode()) for raw in broker.record["pending"]]
    assert [e.phase for e in events] == ["intent_stop", "stopped", "starting_new", "running"]
    assert [e.sequence for e in events] == [1, 2, 3, 4]
    with pytest.raises(ValueError, match="not_ready"):
        broker.execute(permit)
    assert driver.stop_calls == 1


def test_target_failure_selects_only_frozen_accepted_fallback(setup):
    broker, driver, command, permit, _ = setup
    driver.target_fails = True
    broker.execute(permit)
    assert driver.starts == [command.target, command.fallback]
    assert broker.record["phase"] == "fallback_running"
    assert driver.stop_calls == 1


def test_unknown_stop_and_restart_never_repeat_stop_or_start(setup):
    broker, driver, _, permit, _ = setup
    driver.unknown_stop = True
    broker.execute(permit)
    restarted = OnlineEffectBroker(broker.store, driver, broker.session, broker.recovery)
    restarted.reconcile()
    assert restarted.record["phase"] == "intent_stop"
    assert driver.stop_calls == 1 and driver.starts == []


def test_stop_complete_journal_before_start_recovers_once(setup, monkeypatch):
    broker, driver, command, permit, _ = setup
    def crash(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(broker, "_start", crash)
    with pytest.raises(KeyboardInterrupt):
        broker.execute(permit)
    assert broker.record["phase"] == "stopped"
    restarted = OnlineEffectBroker(broker.store, driver, broker.session, broker.recovery)
    restarted.reconcile()
    restarted.reconcile()
    assert driver.stop_calls == 1 and driver.starts == [command.target]


def test_expired_permit_seal_is_atomic_final_and_restart_safe(setup, monkeypatch):
    broker, driver, command, permit, module = setup
    monkeypatch.setattr(module, "boottime_ms", lambda: 5000)
    broker.execute(permit)
    assert driver.stop_calls == 0
    event = broker._event("no_stop_quiescent", running=driver.current())
    assert event.executor_sealed and event.journal_watermark == event.sequence == 1
    assert broker.record["sealed_operations"][str(command.operation_id)] == command.command_sha256
    assert broker.record["quiescent_event_id"] == str(event.event_id)
    assert broker.record["quiescent_at"] == event.occurred_boottime_ms
    assert broker.record["revalidation_id"]
    assert broker.record["seal_event"] == broker.record["pending"][-1]
    restarted = OnlineEffectBroker(broker.store, driver, broker.session, broker.recovery)
    restarted.reconcile()
    with pytest.raises(ValueError, match="not_ready"):
        restarted.execute(permit)
    with pytest.raises(ValueError, match="sealed"):
        restarted._start(command, command.target, fallback=False)
    with pytest.raises(ValueError, match="sealed"):
        restarted._event("intent_stop", running=driver.current())
    assert driver.stop_calls == 0 and driver.starts == []


def test_quiescence_cannot_skip_pending_intent_upload(setup):
    broker, driver, _, permit, _ = setup
    driver.unknown_stop = True
    broker.execute(permit)
    with pytest.raises(ValueError, match="stop_intent_not_no_effect"):
        broker._event("no_stop_quiescent", running=driver.current())
    assert not broker.record.get("executor_sealed")


def test_live_recovery_receipt_never_becomes_execution_authority(setup):
    from contracts.node_lifecycle import StopPermitReceiptV2
    broker, driver, _, permit, _ = setup
    broker.retain_permit_receipt(StopPermitReceiptV2(permit))
    assert broker.record["phase"] == "awaiting_recovery"
    restarted = OnlineEffectBroker(broker.store, driver, broker.session, broker.recovery)
    restarted.reconcile()
    with pytest.raises(ValueError, match="not_ready"):
        restarted.execute(permit)
    assert driver.stop_calls == 0 and not driver.starts


def test_uploaded_stop_intent_still_forbids_no_effect_seal(setup):
    broker, driver, _, permit, _ = setup
    driver.unknown_stop = True
    broker.execute(permit)
    broker.flush()
    assert not broker.record["pending"] and broker.record["intent_stop_written"]
    with pytest.raises(ValueError, match="stop_intent_not_no_effect"):
        broker._event("no_stop_quiescent", running=driver.current())
    assert not broker.record.get("executor_sealed")


def test_worker_spawn_intent_survives_lost_systemd_reply_without_duplicate(setup, monkeypatch):
    from appliance.node import import_worker
    broker, _, command, _, _ = setup
    worker = import_worker.RootImportWorker(broker.store)
    monkeypatch.setattr(worker, "ready", lambda command: False)
    calls = []
    def lost_reply(*args, **kwargs):
        calls.append(args)
        intent = broker.store.read("import-worker")
        assert intent["phase"] == "intent" and intent["command_sha256"] == command.command_sha256
        raise OSError("reply lost")
    monkeypatch.setattr(import_worker.subprocess, "run", lost_reply)
    with pytest.raises(OSError):
        worker.advance(command)
    monkeypatch.setattr(import_worker, "systemctl_show", lambda unit: {"ActiveState": "inactive", "MainPID": "0"})
    with pytest.raises(ValueError, match="outcome_unknown"):
        worker.advance(command)
    assert len(calls) == 1


def test_completed_worker_before_readiness_never_respawns(setup, monkeypatch):
    from appliance.node import import_worker
    broker, driver, command, _, _ = setup
    worker = import_worker.RootImportWorker(broker.store)
    monkeypatch.setattr(worker, "ready", lambda command: True)
    def forbidden(*args, **kwargs):
        raise AssertionError("completed worker must not spawn again")
    monkeypatch.setattr(import_worker.subprocess, "run", forbidden)
    worker.advance(command)
    recovered = OnlineEffectBroker(broker.store, driver, broker.session, broker.recovery)
    recovered.ready()
    assert driver.stop_calls == 0 and not driver.starts


def test_worker_same_unit_new_invocation_never_passes_recovery(setup, monkeypatch):
    from appliance.node import import_worker
    broker, _, command, _, _ = setup
    worker = import_worker.RootImportWorker(broker.store)
    incarnation = uuid4()
    broker.store.write("import-worker", {"command_sha256": command.command_sha256, "unit": "same.service",
        "phase": "observed", "identity": {"pid": 101, "ticks": 303, "invocation": str(incarnation)}})
    monkeypatch.setattr(worker, "ready", lambda command: False)
    rows = {"ActiveState": "active", "MainPID": "101", "InvocationID": str(incarnation)}
    monkeypatch.setattr(import_worker, "systemctl_show", lambda unit: rows)
    monkeypatch.setattr(import_worker, "read_proc_start_ticks", lambda *a: 303)
    worker.advance(command)
    rows["InvocationID"] = str(uuid4())
    with pytest.raises(ValueError, match="process_changed"):
        worker.advance(command)


def test_expired_preparation_closes_only_on_authoritative_cancel_receipt(setup, monkeypatch):
    broker, driver, command, _, module = setup
    broker._save({**broker.record, "phase": "preparing", "ready": None})
    monkeypatch.setattr(module, "boottime_ms", lambda: 51000)
    assert not broker.cancel_expired(worker_quiescent=False)
    assert broker.cancel_expired(worker_quiescent=True)
    event = parse_app_effect_event(broker.record["pending"][0].encode())
    assert event.phase == "cancelled_before_stop" and event.executor_sealed and event.journal_watermark == 1
    assert event.process == command.old_process and event.permit_id is None
    broker.session.transport.request = lambda *a, **kw: (200, b'{"stage_closed":true}')
    broker.flush()
    assert broker.record["stage_closed"]
    assert not broker.cancel_expired(worker_quiescent=True)
    assert driver.stop_calls == 0 and not driver.starts


def test_cancellation_receipt_absence_never_unlocks_new_operation(setup, monkeypatch):
    broker, driver, command, _, module = setup
    monkeypatch.setattr(module, "boottime_ms", lambda: 51000)
    assert broker.cancel_expired(worker_quiescent=True)
    with pytest.raises(ValueError, match="cancellation_receipt"):
        broker.flush()
    assert broker.record["pending"] and not broker.record.get("stage_closed")
    next_command = replace(command, operation_id=uuid4(), command_id=uuid4(), expires_boottime_ms=59000)
    next_command = replace(next_command, command_sha256=stage_digest(next_command))
    with pytest.raises(ValueError, match="cancellation_not_closed"):
        broker.accept(next_command)
    assert driver.stop_calls == 0


def test_cancel_loses_permit_race_retains_seal_and_later_quiescence(setup, monkeypatch):
    from contracts.node_lifecycle import StopPermitReceiptV2
    broker, driver, command, permit, module = setup
    monkeypatch.setattr(module, "boottime_ms", lambda: 51000)
    assert broker.cancel_expired(worker_quiescent=True)
    broker.session.transport.request = lambda *a, **kw: (200, b'{"stage_closed":false}')
    broker.flush()
    broker.retain_permit_receipt(StopPermitReceiptV2(permit))
    restarted = OnlineEffectBroker(broker.store, driver, broker.session, broker.recovery)
    assert restarted.record["executor_sealed"] and restarted.record["permit_recovery_only"]
    with pytest.raises(ValueError, match="not_ready"):
        restarted.execute(permit)
    event = restarted._event("no_stop_quiescent", running=driver.current())
    assert event.sequence == event.journal_watermark == 2 and event.executor_sealed
    assert event.permit_id == permit.permit_id
    with pytest.raises(ValueError, match="sealed"):
        restarted._event("no_stop_quiescent", running=driver.current())
    assert driver.stop_calls == 0 and not driver.starts
