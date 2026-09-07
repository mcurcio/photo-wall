from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from scripts import vm_health_probe as probe

BOOT = "11111111-2222-3333-4444-555555555555"


def report(**changes):
    return dict(boot_id=BOOT, sampled_monotonic=100.0, player_id="secret-player-id",
                authority_epoch=4, persistence="volatile", healthy=True, health_reason="healthy",
                release_accepted=False, clock=dict(
                    status="healthy", rtt=.02, central_offset=.003, apply_age=.004, drift=.001,
                    transport_drift=.001, apply_drift=.0005, mapping_age=.5, step=.0002,
                    uncertainty=.013, samples=2, accepted=2, rejected=0)) | changes


def runner(argv, *, timeout):
    assert timeout == 5
    assert argv[:3] == ["systemctl", "show", argv[2]]
    assert argv[2] in probe.SERVICES
    return SimpleNamespace(returncode=0, stdout=(
        "ActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\n"
    ))


def sample(**kwargs):
    kwargs.setdefault("systemctl_runner", runner)
    return probe.sample_once(1, BOOT, 100.0, socket_state=lambda: "present", **kwargs)


def test_valid_sample_is_a_bounded_public_projection():
    value = sample(report_reader=lambda: report())
    assert value == {
        "event": "photo-wall-health-diagnostic", "boot_id": BOOT, "sample_index": 1,
        "report_status": "present", "healthy": True, "persistence": "volatile",
        "current_boot": True, "identity_valid": True, "sample_age": "fresh",
        "release_accepted": False,
        "clock": {"status": "healthy", "rtt": .02, "offset": .003, "delay": .004,
                  "drift": .001, "transport_drift": .001, "apply_drift": .0005,
                  "mapping_age": .5, "step": .0002, "uncertainty": .013,
                  "samples": 2, "accepted": 2, "rejected": 0},
        "health_reason": "healthy",
        "wayland_socket": "present",
        "services": {
            service: {"active_state": "active", "sub_state": "running", "result": "success",
                      "exec_main_status": 0}
            for service in probe.SERVICES
        },
    }
    assert "secret-player-id" not in json.dumps(value)
    assert probe.validate_event(value, BOOT) == value


@pytest.mark.parametrize("reason", sorted(probe.HEALTH_REASONS))
def test_optional_health_reason_survives_only_as_a_consistent_closed_enum(reason):
    healthy = reason == "healthy"
    value = sample(report_reader=lambda: report(healthy=healthy, health_reason=reason))
    assert value["report_status"] == "present"
    assert value["health_reason"] == reason
    assert value["healthy"] is healthy
    assert probe.validate_event(value, BOOT) == value
    assert "secret-player-id" not in json.dumps(value)


@pytest.mark.parametrize("reason,healthy", [
    ("clock", True), ("healthy", False), ("private-secret", False),
    (None, False), ([], False), ({"secret": "private-secret"}, False), (1, False),
])
def test_invalid_or_contradictory_reason_is_never_exported(reason, healthy):
    value = sample(report_reader=lambda: report(healthy=healthy, health_reason=reason))
    assert value["report_status"] == "invalid"
    assert value["healthy"] is None
    assert "health_reason" not in value
    assert "private-secret" not in json.dumps(value)
    event = sample(report_reader=lambda: report()) | {"health_reason": reason, "healthy": healthy}
    assert probe.validate_event(event, BOOT) is None


def test_missing_or_invalid_health_cannot_claim_a_component_reason():
    missing = sample(report_reader=lambda: (_ for _ in ()).throw(FileNotFoundError()))
    invalid = sample(report_reader=lambda: report(healthy=1))
    for value in (missing, invalid):
        assert probe.validate_event(value | {"health_reason": "clock"}, BOOT) is None


@pytest.mark.parametrize("changes", [
    {"secret": "do-not-export"},
    {"healthy": 1},
    {"authority_epoch": True},
    {"player_id": None},
    {"sampled_monotonic": float("nan")},
    {"persistence": "durable"},
    {"persistence": "other"},
    {"persistence": []},
    {"persistence": {"secret": "do-not-export"}},
    {"boot_id": "old"},
    {"release_accepted": None},
    {"clock": {"status": "healthy"}},
])
def test_malformed_or_unknown_health_is_invalid_without_report_values(changes):
    value = sample(report_reader=lambda: report(**changes))
    assert value["report_status"] == "invalid"
    assert value["healthy"] is None
    assert value["persistence"] == "invalid"
    assert value["identity_valid"] is False
    assert value["sample_age"] == "invalid"
    assert "do-not-export" not in json.dumps(value)


def test_missing_health_is_distinguished_from_invalid_health():
    value = sample(report_reader=lambda: (_ for _ in ()).throw(FileNotFoundError()))
    assert value["report_status"] == "missing"
    assert value["sample_age"] == "missing"
    assert value["healthy"] is None


@pytest.mark.parametrize("sampled, age", [(98.1, "fresh"), (97.9, "stale"), (101.0, "future")])
def test_health_age_is_classified_without_reusing_it_as_acceptance(sampled, age):
    value = sample(report_reader=lambda: report(sampled_monotonic=sampled))
    assert value["sample_age"] == age
    assert value["healthy"] is True


def test_volatile_report_and_other_boot_remain_visible_but_unqualified():
    value = sample(report_reader=lambda: report(persistence="volatile", boot_id=""
                                                 "22222222-3333-4444-5555-666666666666"))
    assert value["report_status"] == "present"
    assert value["persistence"] == "volatile"
    assert value["identity_valid"] is True
    assert value["current_boot"] is False


def test_regular_reader_rejects_symlink_and_oversized_reports(tmp_path):
    target = tmp_path / "report.json"
    target.write_text(json.dumps(report()))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    value = sample(report_reader=lambda: probe.read_report(link))
    assert value["report_status"] == "invalid"
    target.write_bytes(b"{" + b"x" * probe.REPORT_BYTES + b"}")
    value = sample(report_reader=lambda: probe.read_report(target))
    assert value["report_status"] == "invalid"


def test_systemctl_unknown_values_and_failures_are_projected_to_fixed_enums():
    def unknown(_argv, *, timeout):
        return SimpleNamespace(returncode=0, stdout=(
            "ActiveState=secret-state\nSubState=secret-sub\nResult=secret-result\n"
            "ExecMainStatus=999999999999999999999\nUNTRUSTED=secret\n"))

    value = sample(report_reader=lambda: report(), systemctl_runner=unknown)
    assert all(status == {"active_state": "other", "sub_state": "other", "result": "other",
                          "exec_main_status": None}
               for status in value["services"].values())
    assert "secret" not in json.dumps(value)

    def failed(_argv, *, timeout):
        raise TimeoutError

    value = sample(report_reader=lambda: report(), systemctl_runner=failed)
    assert all(status["result"] == "other" for status in value["services"].values())

    def oversized(_argv, *, timeout):
        return SimpleNamespace(returncode=0, stdout="x" * (probe.REPORT_BYTES + 1))

    value = sample(report_reader=lambda: report(), systemctl_runner=oversized)
    assert all(status == {"active_state": "other", "sub_state": "other", "result": "other",
                          "exec_main_status": None}
               for status in value["services"].values())


def test_wayland_socket_check_never_follows_a_link(tmp_path):
    target = tmp_path / "regular"
    target.write_bytes(b"not a socket")
    link = tmp_path / "wayland-0"
    link.symlink_to(target)
    assert probe.wayland_socket(link) == "invalid"
    assert probe.wayland_socket(tmp_path / "missing") == "missing"


def test_validate_event_rejects_extra_fields_bad_boot_and_unbounded_samples():
    value = sample(report_reader=lambda: report())
    assert probe.validate_event(value | {"player_id": "secret"}, BOOT) is None
    assert probe.validate_event(value, "bad") is None
    assert probe.validate_event(value | {"sample_index": 0}, BOOT) is None
    assert probe.validate_event(value | {"sample_index": 61}, BOOT) is None


@pytest.mark.parametrize("changes", [
    {"report_status": "missing", "healthy": True, "persistence": "invalid",
     "current_boot": False, "identity_valid": False, "sample_age": "missing"},
    {"report_status": "invalid", "healthy": None, "persistence": "volatile",
     "current_boot": False, "identity_valid": False, "sample_age": "invalid"},
    {"report_status": "present", "healthy": None, "persistence": "volatile",
     "current_boot": True, "identity_valid": True, "sample_age": "fresh"},
    {"report_status": "present", "healthy": True, "persistence": "volatile",
     "current_boot": True, "identity_valid": False, "sample_age": "fresh"},
    {"report_status": "present", "healthy": True, "persistence": "volatile",
     "current_boot": True, "identity_valid": True, "sample_age": "invalid"},
    {"report_status": "present", "healthy": True, "persistence": "volatile",
     "current_boot": True, "identity_valid": True, "sample_age": "fresh", "clock": None},
])
def test_validate_event_rejects_cross_field_contradictions(changes):
    value = sample(report_reader=lambda: report()) | changes
    assert probe.validate_event(value, BOOT) is None


def test_observe_is_bounded_to_sixty_samples_and_six_hundred_seconds():
    now = [0.0]
    sleeps: list[float] = []

    def clock():
        return now[0]

    def sleep(duration):
        sleeps.append(duration)
        now[0] += duration

    events = list(probe.observe(report_reader=lambda: report(sampled_monotonic=now[0]),
                                boot_id_reader=lambda: BOOT, systemctl_runner=runner,
                                socket_state=lambda: "missing", clock=clock, sleep=sleep))
    assert len(events) == probe.SAMPLE_LIMIT
    assert [event["sample_index"] for event in events] == list(range(1, 61))
    assert now[0] <= probe.MAX_RUNTIME
    assert len(sleeps) == probe.SAMPLE_LIMIT - 1
    assert max(sleeps) <= probe.SAMPLE_INTERVAL


def test_observe_rejects_test_limits_that_could_escape_production_bounds():
    with pytest.raises(probe.ProbeError, match="sampling_limits"):
        list(probe.observe(max_samples=61, boot_id_reader=lambda: BOOT))
    with pytest.raises(probe.ProbeError, match="sampling_limits"):
        list(probe.observe(max_runtime=probe.MAX_RUNTIME + 1, boot_id_reader=lambda: BOOT))


def test_read_report_does_not_modify_input(tmp_path):
    path = tmp_path / "report.json"
    path.write_bytes(json.dumps(report()).encode())
    before = path.read_bytes()
    probe.read_report(path)
    assert path.read_bytes() == before
    assert stat.S_ISREG(os.lstat(path).st_mode)
