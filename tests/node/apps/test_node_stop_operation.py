"""Stop ownership/crash/absence tests, with no physical reboot or PID1 claims."""
from concurrent.futures import InvalidStateError
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from node.test_node_linux_adapters import store
from test_node_boot import environment

from appliance.apps.broker import RunningApp
from appliance.apps.process_linux import SystemdAppProcessDriver
from appliance.apps.stop_linux import GROUP, PROPERTIES, StopObserver, proc_birth
from appliance.apps.stop_operation import StopGuaranteeUnavailable, StopRequest
from appliance.node.recovery import (
    RESTORE_BUDGET_MS,
    STOP_BUDGET_MS,
    RecoveryObligation,
    RecoverySupervisor,
)
from contracts.node_protocol import NodeProcessIdentity


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    boot = uuid4()
    journal = store(tmp_path / "journal", boot)
    old = RunningApp(environment("a"), NodeProcessIdentity(321, 1234, uuid4()), 1, uuid4())
    driver = SystemdAppProcessDriver(tmp_path / "roots", journal, base_abi="base", graphics_abi="g", plugin_abi="p",
                                    proc=tmp_path / "proc", cgroups=tmp_path / "cgroups")
    root = driver.roots / old.environment.environment_sha256 / "rootfs"
    root.mkdir(parents=True)
    group = driver.cgroups / GROUP.lstrip("/")
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 0\n")
    request = StopRequest(uuid4(), boot, "a" * 64, old, 2000)
    calls = []
    process = driver.proc / str(old.process.pid)
    process.mkdir(parents=True)
    (process / "stat").write_text("321 (player) " + " ".join(["S"] + ["0"] * 18 + ["1234"]))
    (process / "root").symlink_to(root)
    (process / "cgroup").write_text("0::" + GROUP + "\n")
    monkeypatch.setattr(driver, "current", lambda: old)
    monkeypatch.setattr("appliance.apps.stop_linux.boottime_ms", lambda: 1000)
    monkeypatch.setattr("appliance.apps.stop_linux.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("blocking transport")))
    def spawn(argv, **kwargs):
        calls.append(argv)
        if "show" in argv:
            kwargs["stdout"].write("".join(k + "=" + v + "\n" for k, v in rows.items()).encode())
        return SimpleNamespace(poll=lambda: 0, wait=lambda **k: 0)
    monkeypatch.setattr("appliance.apps.stop_linux.subprocess.Popen", spawn)
    rows = dict.fromkeys(PROPERTIES.split(","), "")
    rows.update(LoadState="loaded", ActiveState="active", MainPID="321", Job="0",
                InvocationID=str(old.process.invocation_id), ControlGroup=GROUP, RootDirectory=str(root),
                Restart="no", Delegate="no", KillMode="control-group", TimeoutStopUSec="30s")
    driver.test_rows = rows
    yield driver, request, group, rows, calls
    driver.stops.close()
    journal.close()


def begin(driver, request):
    operation = driver.stop(request)
    service(driver, now=1000)  # Capture, persist, submit and collect without blocking.
    driver.test_rows.update(LoadState="not-found", ActiveState="inactive", MainPID="0", Job="0")
    (driver.proc / str(request.old.process.pid) / "stat").unlink(missing_ok=True)
    return operation


def service(driver, now=3000):
    for _ in range(3):
        driver.service(now_ms=now, budget_ms=100)


def test_pending_survives_waiter_and_restart_without_second_dispatch(stopped):
    driver, request, group, rows, calls = stopped
    operation = begin(driver, request)
    with pytest.raises(InvalidStateError):
        operation.result()
    (group / "cgroup.events").write_text("populated 1\n")
    service(driver, now=50000)  # Well beyond the dispatch bound and former 15-second wait.
    assert not operation.done()
    driver.stops.close()
    driver.stops = StopObserver(driver)
    attached = driver.stop(request, reattach_only=True)
    (group / "cgroup.events").write_text("populated 0\n")
    service(driver, now=60000)
    assert attached.result().request == request
    assert operation.result().request == request
    assert sum("stop" in call for call in calls) == 1


@pytest.mark.parametrize("fault", ["malformed", "missing_events", "permission"])
def test_observation_failure_remains_pending_then_recovers(stopped, monkeypatch, fault):
    driver, request, group, rows, _ = stopped
    operation = begin(driver, request)
    if fault == "malformed":
        rows.pop("Job")
    elif fault == "missing_events":
        (group / "cgroup.events").unlink()
    else:
        monkeypatch.setattr("appliance.apps.stop_linux.proc_birth", lambda *a: (_ for _ in ()).throw(PermissionError()))
    service(driver)
    assert not operation.done()
    rows["Job"] = "0"
    (group / "cgroup.events").write_text("populated 0\n")
    monkeypatch.setattr("appliance.apps.stop_linux.proc_birth", lambda *a: None)
    service(driver)
    assert operation.done()


def test_cgroup_replacement_is_terminal_not_empty_proof(stopped):
    driver, request, group, _, _ = stopped
    operation = begin(driver, request)
    group.rename(group.with_name("old"))
    group.mkdir()
    (group / "cgroup.events").write_text("populated 0\n")
    service(driver)
    with pytest.raises(StopGuaranteeUnavailable, match="cgroup_replaced"):
        operation.result()


@pytest.mark.parametrize("field,value", [("Job", "42"), ("MainPID", "321"), ("ActiveState", "deactivating")])
def test_pending_unit_or_job_cannot_complete(stopped, field, value):
    driver, request, _, rows, _ = stopped
    operation = begin(driver, request)
    rows.update(LoadState="loaded", Restart="no", Delegate="no", KillMode="control-group")
    rows[field] = value
    service(driver)
    assert not operation.done()


def test_disappeared_group_with_exact_absence_completes(stopped):
    driver, request, group, _, _ = stopped
    operation = begin(driver, request)
    (group / "cgroup.events").unlink()
    group.rmdir()
    service(driver)
    assert operation.result().request == request


def test_conflicting_request_and_new_boot_refuse(stopped):
    driver, request, _, _, _ = stopped
    driver.stop(request)
    with pytest.raises(StopGuaranteeUnavailable):
        driver.stop(replace(request, command_sha256="c" * 64), reattach_only=True)
    with pytest.raises(StopGuaranteeUnavailable, match="boot"):
        driver.stop(replace(request, boot_id=uuid4()), reattach_only=True)


def test_reattachment_without_capture_never_dispatches(stopped):
    driver, request, _, _, calls = stopped
    with pytest.raises(StopGuaranteeUnavailable, match="capture_unavailable"):
        driver.stop(request, reattach_only=True)
    assert calls == []


def test_dispatch_deadline_is_checked_after_durable_intent(stopped, monkeypatch):
    driver, request, _, _, calls = stopped
    monkeypatch.setattr("appliance.apps.stop_linux.boottime_ms", lambda: 2000)
    operation = begin(driver, request)
    with pytest.raises(StopGuaranteeUnavailable, match="expired"):
        operation.result()
    assert not any("stop" in call for call in calls)


def test_proc_absence_is_distinct_from_malformed_data(tmp_path):
    assert proc_birth(tmp_path, 123) is None
    (tmp_path / "123").mkdir()
    (tmp_path / "123/stat").write_text("broken")
    with pytest.raises(ValueError, match="malformed"):
        proc_birth(tmp_path, 123)


@pytest.fixture
def recovery(tmp_path):
    boot = uuid4()
    journal = store(tmp_path, boot)
    old = NodeProcessIdentity(123, 456, uuid4())
    obligation = RecoveryObligation(boot, uuid4(), "a" * 64, old, 1, "a" * 64, "b" * 64, "a" * 64,
                                   1000, 1000 + STOP_BUDGET_MS, 1000 + STOP_BUDGET_MS + RESTORE_BUDGET_MS)
    calls = []
    state = {"stopped": False, "controlled": False}
    observer = SimpleNamespace(stopped=lambda o: state["stopped"], controlled=lambda *a, **k: state["controlled"])
    driver = SimpleNamespace(initiate=lambda: calls.append(journal.read("local-recovery")) or True)
    supervisor = RecoverySupervisor(journal, driver, observer)
    yield supervisor, obligation, state, calls
    journal.close()


def test_host_restarts_keep_deadlines_and_reboot_intent_once(recovery):
    supervisor, obligation, _, calls = recovery
    receipt = supervisor.arm(obligation, now_ms=1000)
    for _ in range(3):
        supervisor = RecoverySupervisor(supervisor.store, supervisor.driver, supervisor.observer)
        assert supervisor.arm(obligation, now_ms=2000) == receipt
        supervisor.service(now_ms=obligation.stop_deadline_ms - 1)
    assert not calls
    supervisor.service(now_ms=obligation.stop_deadline_ms)
    assert list(calls[0]["records"].values())[0]["phase"] == "reboot_intent"
    supervisor.service(now_ms=obligation.restore_deadline_ms + 1)
    assert len(calls) == 1
    with pytest.raises(ValueError, match="already_intended"):
        supervisor.arm(obligation, now_ms=2000)


def test_late_stop_advances_to_original_restore_deadline(recovery):
    supervisor, obligation, state, calls = recovery
    supervisor.arm(obligation, now_ms=1000)
    state["stopped"] = True
    supervisor.service(now_ms=obligation.stop_deadline_ms)
    assert not calls
    supervisor.service(now_ms=obligation.restore_deadline_ms)
    assert len(calls) == 1


def test_local_control_disarms_without_central(recovery):
    supervisor, obligation, state, calls = recovery
    receipt = supervisor.arm(obligation, now_ms=1000)
    state["controlled"] = True
    supervisor.advance(obligation.operation_id, receipt, {"kind": "controlled"}, now_ms=1100)
    supervisor.service(now_ms=obligation.restore_deadline_ms + 1)
    assert calls == []


def test_registration_conflicts_and_foreign_boot_refuse(recovery):
    supervisor, obligation, _, _ = recovery
    supervisor.arm(obligation, now_ms=1000)
    with pytest.raises(ValueError, match="conflict"):
        supervisor.arm(replace(obligation, target="c" * 64), now_ms=1000)
    with pytest.raises(ValueError, match="boot"):
        supervisor.arm(replace(obligation, boot_id=uuid4()), now_ms=1000)
    with pytest.raises(ValueError, match="deadline"):
        replace(obligation, restore_deadline_ms=obligation.restore_deadline_ms + 1)


def test_unarmed_network_failure_cannot_reboot(recovery):
    supervisor, obligation, _, calls = recovery
    supervisor.service(now_ms=obligation.restore_deadline_ms + 1)
    assert calls == []


def test_crash_after_adapter_intent_never_resubmits(stopped, monkeypatch):
    driver, request, _, _, calls = stopped
    original = driver.store.write
    def crash(name, value):
        original(name, value)
        if name.startswith("stop-") and value["dispatch"] == "not_dispatched":
            raise KeyboardInterrupt()
    monkeypatch.setattr(driver.store, "write", crash)
    with pytest.raises(KeyboardInterrupt):
        driver.stop(request)
    monkeypatch.setattr(driver.store, "write", original)
    driver.stops.close()
    driver.stops = StopObserver(driver)
    operation = driver.stop(request, reattach_only=True)
    with pytest.raises(StopGuaranteeUnavailable, match="capture_unavailable"):
        operation.result()
    assert not any("stop" in call for call in calls)


def test_completed_record_survives_restart_and_cannot_change_request(stopped):
    driver, request, _, _, calls = stopped
    operation = begin(driver, request)
    service(driver)
    driver.stops = StopObserver(driver)
    assert driver.stop(request, reattach_only=True).result() == operation.result()
    with pytest.raises(StopGuaranteeUnavailable, match="journal"):
        driver.stop(replace(request, dispatch_not_after_boottime_ms=2001), reattach_only=True)
    assert sum("stop" in call for call in calls) == 1


def test_owner_restart_after_reboot_intent_cannot_repeat_dispatch(recovery, monkeypatch):
    supervisor, obligation, _, calls = recovery
    supervisor.arm(obligation, now_ms=1000)
    def crash():
        raise KeyboardInterrupt()
    monkeypatch.setattr(supervisor.driver, "initiate", crash)
    with pytest.raises(KeyboardInterrupt):
        supervisor.service(now_ms=obligation.stop_deadline_ms)
    restarted = RecoverySupervisor(supervisor.store, supervisor.driver, supervisor.observer)
    restarted.service(now_ms=obligation.restore_deadline_ms + 1)
    assert calls == []
    assert next(iter(supervisor._load()["records"].values()))["phase"] == "reboot_intent"


def test_journal_failure_prevents_reboot_effect(recovery, monkeypatch):
    supervisor, obligation, _, calls = recovery
    supervisor.arm(obligation, now_ms=1000)
    def failed(*a):
        supervisor.store.failed = True
        raise OSError("disk")
    monkeypatch.setattr(supervisor.store, "write", failed)
    with pytest.raises(OSError):
        supervisor.service(now_ms=obligation.stop_deadline_ms)
    assert calls == []


@pytest.mark.parametrize("field,value", [("schema", True), ("completed_ms", True), ("identity", {}), ("fault", 5)])
def test_invalid_adapter_journal_cannot_complete(stopped, field, value):
    driver, request, _, _, _ = stopped
    driver.stop(request)
    name = "stop-" + str(request.operation_id)
    row = driver.store.read(name)
    driver.store.write(name, {**row, field: value})
    with pytest.raises(StopGuaranteeUnavailable, match="journal"):
        driver.stop(request, reattach_only=True)


def test_empty_recovery_journal_is_corruption_not_fresh_authority(recovery):
    supervisor, obligation, _, _ = recovery
    supervisor.store.write("local-recovery", {})
    with pytest.raises(ValueError, match="journal"):
        supervisor.arm(obligation, now_ms=1000)


def test_recovery_cause_is_reportable_without_remote_authority(recovery):
    supervisor, obligation, _, _ = recovery
    supervisor.arm(obligation, now_ms=1000)
    supervisor.service(now_ms=obligation.stop_deadline_ms)
    metrics, fault = supervisor.telemetry()
    assert fault == "local_recovery_stop_deadline"
    assert metrics[1][1] == 1


def test_stop_admission_is_nonblocking_and_does_not_dispatch(stopped):
    driver, request, _, _, calls = stopped
    operation = driver.stop(request)
    assert calls == [] and not operation.done()
    assert driver.store.read("stop-" + str(request.operation_id))["identity"] is None


def test_crash_after_capture_allows_natural_exit_without_dispatch(stopped, monkeypatch):
    driver, request, _, rows, calls = stopped
    original = driver.store.write
    def crash(name, value):
        original(name, value)
        if name.startswith("stop-") and value["identity"] is not None:
            raise KeyboardInterrupt()
    driver.stop(request)
    monkeypatch.setattr(driver.store, "write", crash)
    with pytest.raises(KeyboardInterrupt):
        service(driver, now=1000)
    monkeypatch.setattr(driver.store, "write", original)
    driver.stops.close()
    driver.stops = StopObserver(driver)
    operation = driver.stop(request, reattach_only=True)
    rows.update(LoadState="not-found", ActiveState="inactive", MainPID="0", Job="0")
    (driver.proc / "321/stat").unlink()
    service(driver)
    assert operation.result().request == request
    assert not any("stop" in call for call in calls)


def test_live_process_teardown_does_not_need_root_metadata(stopped):
    driver, request, _, rows, _ = stopped
    driver.stop(request)
    service(driver, now=1000)
    (driver.proc / "321/root").unlink()
    rows["ActiveState"] = "deactivating"
    service(driver)
    assert not driver.stops.view.done()


def test_transport_timeout_kills_only_transport_and_remains_pending(stopped, monkeypatch):
    driver, request, _, _, _ = stopped
    operation = begin(driver, request)
    killed = []
    child = SimpleNamespace(poll=lambda: None, kill=lambda: killed.append(True))
    monkeypatch.setattr("appliance.apps.stop_linux.subprocess.Popen", lambda *a, **k: child)
    driver.service(now_ms=3000, budget_ms=100)
    driver.service(now_ms=4000, budget_ms=100)
    assert killed == [True] and not operation.done()
    assert driver.stops.retiring == [child]
    child.poll = lambda: 0


def test_local_control_proof_survives_expired_session_and_central_timeout(stopped, monkeypatch):
    from appliance.node.app_link import BrokerLinkService
    from contracts.node_app_link import (
        NodeAppLinkV2,
        encode_node_app_link,
        encode_node_app_link_begin,
        encode_node_app_link_result,
        parse_node_app_link_challenge,
    )
    from contracts.node_commands import NodeSessionGrant, encode_session_grant
    from contracts.node_protocol import NodeProducerV2

    driver, request, _, _, _ = stopped
    producer = NodeProducerV2("site", "device-" + "a" * 64, 1, request.boot_id, "app_effect_broker", uuid4())
    grant = NodeSessionGrant(producer, uuid4(), uuid4(), 500, "app_effect")
    driver.store.write("local-proof-grant", {"grant": encode_session_grant(grant).decode()})
    service = BrokerLinkService.__new__(BrokerLinkService)
    service.driver = driver
    service.session = SimpleNamespace(grant=None, store=driver.store, claim=None,
        ensure=lambda: (_ for _ in ()).throw(AssertionError("network session gate")),
        request=lambda *a: (_ for _ in ()).throw(TimeoutError()))
    sent = []
    def send(raw):
        sent.append(raw)
        return len(raw)
    def receive(*a, **k):
        peer = (request.old.process.pid, 10004, 10004)
        if not sent:
            return peer, encode_node_app_link_begin(player_id="p-" + "a" * 32,
                authority_epoch=1, control_receipt='{"authority_epoch":1}')
        challenge = parse_node_app_link_challenge(sent[0])
        return peer, encode_node_app_link(NodeAppLinkV2(challenge, "c" * 64, "d" * 128))
    monkeypatch.setattr("appliance.node.app_link.receive_credential_packet", receive)
    monkeypatch.setattr("appliance.node.app_link.boottime_ms", lambda: 1000)
    service.feed = None
    # Accepted locally: the proof never waits on Central (whose request would time out here).
    service.handle(SimpleNamespace(send=send))
    assert sent[-1] == encode_node_app_link_result("accepted")
    proof = driver.store.read("local-app-control")
    assert proof["operation_id"] == str(request.old.operation_id)
    assert proof["progress"]["process"]["pid"] == request.old.process.pid
