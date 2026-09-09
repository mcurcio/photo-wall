"""The exact-image Player controller exposes only bounded cache test actions."""

import json
from types import SimpleNamespace

import pytest

from scripts import vm_player_control as control

BOOT = "01234567-89ab-cdef-0123-456789abcdef"
DIGEST = "a" * 64
PLAYER = "p-" + "b" * 32


def item(action="restart", digest=None):
    return dict(schema=1, revision=1, boot_id=BOOT, player_id=PLAYER, prior_epoch=3,
                action=action, sha256=digest)


@pytest.mark.parametrize("change", ["action", "path", "revision", "extra"])
def test_control_rejects_arbitrary_or_unbounded_input(tmp_path, change):
    value = item()
    if change == "action":
        value["action"] = "shell"
    elif change == "path":
        value.update(action="delete", sha256="../cache")
    elif change == "revision":
        value["revision"] = 9
    else:
        value["command"] = "anything"
    path = tmp_path / "control.json"
    path.write_text(json.dumps(value))
    with pytest.raises(control.ControlError, match="invalid_player_control"):
        control.control(path)


def test_named_cache_file_can_be_deleted_or_corrupted(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    blob = cache / f"{DIGEST}.blob"
    blob.write_bytes(b"valid bytes")
    control.mutate("corrupt", DIGEST, cache)
    assert blob.read_bytes() == b"!alid bytes"
    control.mutate("delete", DIGEST, cache)
    assert not blob.exists()


def test_cache_path_types_fail_closed(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    cache = tmp_path / "cache"
    cache.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        control.mutate("delete", DIGEST, cache)
    cache.unlink()
    cache.mkdir()
    (cache / f"{DIGEST}.blob").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        control.mutate("corrupt", DIGEST, cache)


def test_controller_restarts_only_the_player_service(tmp_path, capsys):
    control_path, boot_path, health_path = (tmp_path / name for name in
                                            ("control.json", "boot.json", "health.json"))
    control_path.write_text(json.dumps(item()))
    boot_path.write_text(json.dumps({"boot_id": BOOT}))
    health_path.write_text(json.dumps({"player_id": PLAYER, "authority_epoch": 3}))
    moments = iter((0, 0, 2))
    calls = []

    def command(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    original = control.time.monotonic
    control.time.monotonic = lambda: next(moments)
    try:
        control.run(control_path=control_path, boot_path=boot_path, health_path=health_path,
                    directory=tmp_path,
                    command_runner=command, sleep=lambda _: None, timeout=1)
    finally:
        control.time.monotonic = original
    assert calls == [["/usr/bin/systemctl", "restart", "photo-wall-player.service"]]
    event = json.loads(capsys.readouterr().out)
    assert event["event"] == "photo-wall-player-control" and event["steps"] == ["restart"]


def test_fault_stops_then_mutates_then_starts(tmp_path, monkeypatch):
    paths = {name: tmp_path / f"{name}.json" for name in ("control", "boot", "health")}
    paths["control"].write_text(json.dumps(item("delete", DIGEST)))
    paths["boot"].write_text(json.dumps({"boot_id": BOOT}))
    paths["health"].write_text(json.dumps({"player_id": PLAYER, "authority_epoch": 3}))
    events = []
    monkeypatch.setattr(control, "mutate", lambda *args: events.append("mutate"))
    moments = iter((0, 0, 2))
    monkeypatch.setattr(control.time, "monotonic", lambda: next(moments))
    def command(argv, **_kwargs):
        events.append(argv[1])
        return SimpleNamespace(returncode=0)
    control.run(control_path=paths["control"], boot_path=paths["boot"],
                health_path=paths["health"], directory=tmp_path,
                command_runner=command, sleep=lambda _: None, timeout=1)
    assert events == ["stop", "mutate", "start"]


def test_wrong_epoch_fails_before_mutation_or_restart(tmp_path):
    paths = {name: tmp_path / f"{name}.json" for name in ("control", "boot", "health")}
    paths["control"].write_text(json.dumps(item("delete", DIGEST)))
    paths["boot"].write_text(json.dumps({"boot_id": BOOT}))
    paths["health"].write_text(json.dumps({"player_id": PLAYER, "authority_epoch": 4}))
    with pytest.raises(control.ControlError, match="authority_mismatch"):
        control.run(control_path=paths["control"], boot_path=paths["boot"],
                    health_path=paths["health"], directory=tmp_path, timeout=1)


def test_restart_failure_has_a_bounded_code(tmp_path, monkeypatch):
    paths = {name: tmp_path / f"{name}.json" for name in ("control", "boot", "health")}
    paths["control"].write_text(json.dumps(item()))
    paths["boot"].write_text(json.dumps({"boot_id": BOOT}))
    paths["health"].write_text(json.dumps({"player_id": PLAYER, "authority_epoch": 3}))
    with pytest.raises(control.ControlError, match="player_restart_failed"):
        control.run(control_path=paths["control"], boot_path=paths["boot"],
                    health_path=paths["health"], directory=tmp_path,
                    command_runner=lambda *args, **kwargs: SimpleNamespace(returncode=1), timeout=1)
