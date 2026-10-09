"""Crash and convergence acceptance for the online base effect owner."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from node.test_node_linux_adapters import store
from test_node_boot import environment

from appliance.apps.broker import RunningApp
from appliance.apps.online_broker import OnlineEffectBroker
from appliance.apps.stop_operation import StopCompleted, StopView
from contracts.node_lifecycle import (
    StageCommandV2,
    encode_stage_command,
    parse_app_effect_event,
    stage_digest,
)
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2


class Driver:
    def __init__(self, old):
        self.running = old
        self.old_epoch = old.app_epoch
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
                                  self.old_epoch + len(self.starts), operation_id)
        return self.running


def stage(producer, old, target, fallback, *, offer_id):
    command = StageCommandV2(uuid4(), uuid4(), "0" * 64, producer, uuid4(), offer_id,
                             old.process, old.app_epoch, old.environment, target, fallback)
    return replace(command, command_sha256=stage_digest(command))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import appliance.apps.online_broker as module
    monkeypatch.setattr(module, "boottime_ms", lambda: 1000)
    monkeypatch.setattr(module, "memory_values", lambda: (8 * 1024**3, 7 * 1024**3))
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    old = RunningApp(environment("a"), NodeProcessIdentity(100, 200, uuid4()), 1, uuid4())
    command = stage(producer, old, environment("b"), old.environment, offer_id=uuid4())
    journal = store(tmp_path, producer.kernel_boot_id)
    # A renewed session keeps a command current: binding is to the producer, not one session.
    session = SimpleNamespace(grant=SimpleNamespace(producer=producer, session_id=uuid4(),
        offer_id=command.offer_id), claim=None, request=lambda *args, **kwargs: (200, b'{}'))
    driver = Driver(old)
    broker = OnlineEffectBroker(journal, driver, session, SimpleNamespace(arm=lambda x: x.receipt, advance=lambda *a: None))
    broker.accept(command)
    yield broker, driver, command, module
    journal.close()


def phases(broker):
    return [parse_app_effect_event(raw.encode()).phase for raw in broker.record["pending"]]


def test_preparation_has_no_effect_then_verified_roots_switch_once(setup):
    broker, driver, command, _ = setup
    assert driver.stop_calls == 0 and driver.starts == [] and broker.record["phase"] == "preparing"
    broker.execute()
    assert driver.stop_calls == 1 and driver.starts == [command.target]
    events = [parse_app_effect_event(raw.encode()) for raw in broker.record["pending"]]
    assert [e.phase for e in events] == ["intent_stop", "stopped", "starting_new", "running"]
    assert [e.sequence for e in events] == [1, 2, 3, 4]
    with pytest.raises(ValueError, match="not_preparing"):
        broker.execute()
    assert driver.stop_calls == 1


def test_unverified_root_or_changed_old_process_never_journals_intent(setup):
    broker, driver, command, _ = setup
    driver.verify = lambda reference: reference != command.fallback
    with pytest.raises(ValueError, match="root_unverified"):
        broker.execute()
    driver.verify = lambda reference: True
    driver.running = replace(driver.running, app_epoch=7)
    with pytest.raises(ValueError, match="old_process_changed"):
        broker.execute()
    assert broker.record["phase"] == "preparing" and not broker.record["pending"]
    assert driver.stop_calls == 0 and not driver.starts


def test_reporting_never_gates_the_switch_and_flush_reports_in_order(setup):
    broker, driver, command, _ = setup
    def outage(*args, **kwargs):
        raise OSError("central unreachable")
    broker.session.request = outage
    broker.session.grant = None
    broker.execute()
    assert driver.starts == [command.target]
    with pytest.raises(OSError):
        broker.flush()
    assert phases(broker) == ["intent_stop", "stopped", "starting_new", "running"]
    sent = []
    broker.session.request = lambda method, path, body=None: (sent.append((path, body)) or (200, b"{}"))
    broker.flush()
    assert [path for path, _ in sent] == ["/v2/node/app-responses"] + ["/v2/node/app-effects"] * 4
    assert [parse_app_effect_event(body).sequence for _, body in sent[1:]] == [1, 2, 3, 4]
    assert not broker.record["pending"] and not broker.record["response_pending"]


def test_a_refused_effect_report_is_dropped_and_never_blocks_later_ones(setup):
    broker, _, _, _ = setup
    broker.execute()
    sent, statuses = [], iter([200, 409, 200, 200])
    broker.session.request = lambda method, path, body=None: (sent.append((path, body)) or (
        (200, b"{}") if path.endswith("app-responses") else (next(statuses), b"{}")))
    broker.flush()
    assert [parse_app_effect_event(body).sequence for path, body in sent[1:]] == [1, 2, 3, 4]
    assert not broker.record["pending"] and broker.record["refused_reports"] == 1
    assert broker.record["last_refused_status"] == 409


@pytest.mark.parametrize("status", [500, 503, 401, 429])
def test_an_unavailable_central_retries_the_same_head_report(setup, status):
    broker, _, _, _ = setup
    broker.execute()
    sent = []
    broker.session.request = lambda method, path, body=None: (sent.append((path, body)) or (
        (200, b"{}") if path.endswith("app-responses") else (status, b"{}")))
    broker.flush()
    broker.flush()
    assert [parse_app_effect_event(body).sequence for path, body in sent if path.endswith("app-effects")] == [1, 1]
    assert phases(broker) == ["intent_stop", "stopped", "starting_new", "running"]
    assert "refused_reports" not in broker.record


def test_latest_stage_replaces_unstarted_switch_but_never_one_in_flight(setup):
    broker, driver, command, _ = setup
    old = driver.running
    newer = stage(command.producer, old, environment("c"), old.environment, offer_id=command.offer_id)
    broker.accept(newer)
    assert broker.record["command"] == encode_stage_command(newer).decode()
    assert broker.record["phase"] == "preparing"
    driver.unknown_stop = True
    broker.execute()
    assert broker.record["phase"] == "intent_stop"
    latest = stage(command.producer, old, environment("d"), old.environment, offer_id=command.offer_id)
    with pytest.raises(ValueError, match="prior_operation_unresolved"):
        broker.accept(latest)
    assert driver.stop_calls == 1


def test_completed_switch_accepts_the_next_stage_and_keeps_unreported_events(setup):
    broker, driver, command, _ = setup
    broker.execute()
    following = stage(command.producer, driver.running, environment("c"), command.target,
                      offer_id=command.offer_id)
    broker.accept(following)
    assert broker.record["phase"] == "preparing" and broker.record["sequence"] == 0
    assert phases(broker) == ["intent_stop", "stopped", "starting_new", "running"]


def test_target_failure_selects_only_frozen_accepted_fallback(setup):
    broker, driver, command, _ = setup
    driver.target_fails = True
    broker.execute()
    assert driver.starts == [command.target, command.fallback]
    assert broker.record["phase"] == "fallback_running"
    assert driver.stop_calls == 1


def test_unknown_stop_and_restart_never_repeat_stop_or_start(setup):
    broker, driver, _, _ = setup
    driver.unknown_stop = True
    broker.execute()
    restarted = OnlineEffectBroker(broker.store, driver, broker.session, broker.recovery)
    restarted.reconcile()
    assert restarted.record["phase"] == "intent_stop"
    assert driver.stop_calls == 1 and driver.starts == []


def test_stop_complete_journal_before_start_recovers_once(setup, monkeypatch):
    broker, driver, command, _ = setup
    def crash(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(broker, "_start", crash)
    with pytest.raises(KeyboardInterrupt):
        broker.execute()
    assert broker.record["phase"] == "stopped"
    restarted = OnlineEffectBroker(broker.store, driver, broker.session, broker.recovery)
    restarted.reconcile()
    restarted.reconcile()
    assert driver.stop_calls == 1 and driver.starts == [command.target]


def test_worker_spawn_intent_survives_lost_systemd_reply_without_duplicate(setup, monkeypatch):
    from appliance.apps import import_worker
    broker, _, command, _ = setup
    worker = import_worker.RootImportWorker(broker.store, base_abi="b", graphics_abi="g", plugin_abi="p")
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


def test_completed_worker_never_respawns(setup, monkeypatch):
    from appliance.apps import import_worker
    broker, driver, command, _ = setup
    worker = import_worker.RootImportWorker(broker.store, base_abi="b", graphics_abi="g", plugin_abi="p")
    monkeypatch.setattr(worker, "ready", lambda command: True)
    def forbidden(*args, **kwargs):
        raise AssertionError("completed worker must not spawn again")
    monkeypatch.setattr(import_worker.subprocess, "run", forbidden)
    worker.advance(command)
    assert driver.stop_calls == 0 and not driver.starts


def test_worker_same_unit_new_invocation_never_passes_recovery(setup, monkeypatch):
    from appliance.apps import import_worker
    broker, _, command, _ = setup
    worker = import_worker.RootImportWorker(broker.store, base_abi="b", graphics_abi="g", plugin_abi="p")
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


def test_pending_stop_recovers_locally_after_session_loss_without_redispatch(setup):
    broker, driver, command, _ = setup
    driver.unknown_stop = True
    broker.execute()
    broker.session.grant = None
    broker.session.request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
    driver.unknown_stop = False
    recovered = OnlineEffectBroker(broker.store, driver, broker.session, broker.recovery)
    recovered.service()
    recovered.service()
    assert driver.stop_calls == 1 and driver.starts == [command.target]
    assert phases(broker) == ["intent_stop", "stopped", "starting_new", "running"]


def test_host_ack_loss_prevents_stop_and_replay_does_not_extend_obligation(setup):
    broker, driver, _, _ = setup
    registrations = []
    def lost(obligation):
        registrations.append(obligation)
        raise TimeoutError()
    broker.recovery.arm = lost
    with pytest.raises(TimeoutError):
        broker.execute()
    with pytest.raises(TimeoutError):
        broker.reconcile()
    assert registrations[0] == registrations[1]
    assert driver.stop_calls == 0 and driver.starts == []


def test_reboot_intent_refuses_replacement_even_after_stop_completion(setup, monkeypatch):
    broker, driver, _, _ = setup
    original = broker._start
    monkeypatch.setattr(broker, "_start", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        broker.execute()
    monkeypatch.setattr(broker, "_start", original)
    broker.recovery.arm = lambda *a: (_ for _ in ()).throw(ValueError("reboot_intended"))
    with pytest.raises(ValueError, match="reboot_intended"):
        broker.reconcile()
    assert not driver.starts and driver.stop_calls == 1


def test_stop_intent_and_frozen_recovery_are_one_crash_atomic_transition(setup, monkeypatch):
    broker, driver, command, _ = setup
    original = broker.store.write
    intents = []
    def save(name, value):
        original(name, value)
        if name == "online" and value["phase"] == "intent_stop":
            assert value["stop_request"]["operation_id"] == str(command.operation_id)
            assert value["recovery"]["operation_id"] == str(command.operation_id)
            intents.append(value)
            raise KeyboardInterrupt()
    monkeypatch.setattr(broker.store, "write", save)
    with pytest.raises(KeyboardInterrupt):
        broker.execute()
    assert driver.stop_calls == 0
    assert len(intents) == 1
    assert broker.record["intent_stop_written"]
    with pytest.raises(ValueError, match="not_preparing"):
        broker.execute()
    assert broker.record["recovery"] == intents[0]["recovery"]


def test_wrong_host_receipt_cannot_dispatch(setup):
    broker, driver, _, _ = setup
    broker.recovery.arm = lambda obligation: "wrong"
    with pytest.raises(ValueError, match="recovery_receipt"):
        broker.execute()
    assert driver.stop_calls == 0 and not driver.starts
