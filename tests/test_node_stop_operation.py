"""Stop ownership/crash/absence tests, with no physical reboot or PID1 claims."""
from concurrent.futures import InvalidStateError
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_node_boot import environment
from test_node_linux_adapters import store

from appliance.node.broker import RunningApp
from appliance.node.process_linux import SystemdAppProcessDriver
from appliance.node.recovery import (
    RESTORE_BUDGET_MS,
    STOP_BUDGET_MS,
    RecoveryObligation,
    RecoverySupervisor,
)
from appliance.node.stop_linux import GROUP, PROPERTIES, StopObserver, proc_birth
from appliance.node.stop_operation import StopGuaranteeUnavailable, StopRequest
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
    request = StopRequest(uuid4(), boot, "a" * 64, uuid4(), "b" * 64, old, 2000)
    calls = []
    monkeypatch.setattr(driver, "current", lambda: old)
    monkeypatch.setattr("appliance.node.stop_linux.boottime_ms", lambda: 1000)
    monkeypatch.setattr("appliance.node.stop_linux.subprocess.run", lambda *a, **k: SimpleNamespace(
        stdout="Restart=no\nDelegate=no\nKillMode=control-group\nTimeoutStopUSec=30s\n"))
    def spawn(argv, **kwargs):
        calls.append(argv)
        if "show" in argv:
            kwargs["stdout"].write("".join(k + "=" + v + "\n" for k, v in rows.items()).encode())
        return SimpleNamespace(poll=lambda: 0, wait=lambda **k: 0)
    monkeypatch.setattr("appliance.node.stop_linux.subprocess.Popen", spawn)
    rows = dict.fromkeys(PROPERTIES.split(","), "")
    rows.update(LoadState="not-found", ActiveState="inactive", MainPID="0", Job="0")
    yield driver, request, group, rows, calls
    driver.stops.close()
    journal.close()


def service(driver, now=3000):
    for _ in range(3):
        driver.service(now_ms=now, budget_ms=100)


def test_pending_survives_waiter_and_restart_without_second_dispatch(stopped):
    driver, request, group, rows, calls = stopped
    operation = driver.stop(request)
    with pytest.raises(InvalidStateError):
        operation.result()
    (group / "cgroup.events").write_text("populated 1\n")
    service(driver, now=50000)  # Well beyond both permit and former 15-second wait.
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
    operation = driver.stop(request)
    if fault == "malformed":
        rows.pop("Job")
    elif fault == "missing_events":
        (group / "cgroup.events").unlink()
    else:
        monkeypatch.setattr("appliance.node.stop_linux.proc_birth", lambda *a: (_ for _ in ()).throw(PermissionError()))
    service(driver)
    assert not operation.done()
    rows["Job"] = "0"
    (group / "cgroup.events").write_text("populated 0\n")
    monkeypatch.setattr("appliance.node.stop_linux.proc_birth", lambda *a: None)
    service(driver)
    assert operation.done()


def test_cgroup_replacement_is_terminal_not_empty_proof(stopped):
    driver, request, group, _, _ = stopped
    operation = driver.stop(request)
    group.rename(group.with_name("old"))
    group.mkdir()
    (group / "cgroup.events").write_text("populated 0\n")
    service(driver)
    with pytest.raises(StopGuaranteeUnavailable, match="cgroup_replaced"):
        operation.result()


@pytest.mark.parametrize("field,value", [("Job", "42"), ("MainPID", "321"), ("ActiveState", "deactivating")])
def test_pending_unit_or_job_cannot_complete(stopped, field, value):
    driver, request, _, rows, _ = stopped
    rows.update(LoadState="loaded", Restart="no", Delegate="no", KillMode="control-group")
    rows[field] = value
    operation = driver.stop(request)
    service(driver)
    assert not operation.done()


def test_disappeared_group_with_exact_absence_completes(stopped):
    driver, request, group, _, _ = stopped
    operation = driver.stop(request)
    (group / "cgroup.events").unlink()
    group.rmdir()
    service(driver)
    assert operation.result().request == request


def test_conflicting_request_and_new_boot_refuse(stopped):
    driver, request, _, _, _ = stopped
    driver.stop(request)
    with pytest.raises(StopGuaranteeUnavailable):
        driver.stop(replace(request, permit_sha256="c" * 64), reattach_only=True)
    with pytest.raises(StopGuaranteeUnavailable, match="boot"):
        driver.stop(replace(request, boot_id=uuid4()), reattach_only=True)


def test_reattachment_without_capture_never_dispatches(stopped):
    driver, request, _, _, calls = stopped
    with pytest.raises(StopGuaranteeUnavailable, match="capture_unavailable"):
        driver.stop(request, reattach_only=True)
    assert calls == []


def test_dispatch_deadline_is_checked_after_durable_intent(stopped, monkeypatch):
    driver, request, _, _, calls = stopped
    monkeypatch.setattr("appliance.node.stop_linux.boottime_ms", lambda: 2000)
    operation = driver.stop(request)
    with pytest.raises(StopGuaranteeUnavailable, match="expired"):
        operation.result()
    assert not calls


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
