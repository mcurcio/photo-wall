import pathlib
import socket
import subprocess
import threading
import uuid

import pytest

from scripts import check_player_unit as check

SOURCE = pathlib.Path(__file__).parents[1] / "appliance/systemd/player.service"


def test_render_unit_keeps_production_preflight_and_sandbox():
    source = SOURCE.read_text(encoding="utf-8")
    rendered = check.render_unit(source, "pw-check-" + "a" * 32 + ".service",
                                 "/run/photo-wall/pw-check-probe.py")

    for line in (
        "ExecStartPre=-/usr/bin/timeout 15 /bin/sh -c 'until test -S /run/user/10001/wayland-0; do sleep 0.1; done'",
        "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=read-only",
        "InaccessiblePaths=-/home -/root", "PrivateTmp=yes",
        "ReadWritePaths=/run/photo-wall/player",
        "RuntimeDirectoryPreserve=yes",
    ):
        assert line in rendered
    assert "Wants=\nAfter=" in rendered
    assert "Type=oneshot" in rendered
    assert "RemainAfterExit=yes" in rendered
    assert "Restart=no" in rendered
    assert "TimeoutStartSec=30s" in rendered
    assert rendered.count("ExecStart=\n") == 1
    assert rendered.endswith("ExecStart=/usr/bin/python3 /run/photo-wall/pw-check-probe.py\n")
    assert "/var/lib/photo-wall/player" not in rendered


def test_environment_rejects_existing_runtime_before_any_account_mutation(tmp_path, monkeypatch):
    paths = check.Paths(tmp_path)
    for path in (paths.run, paths.systemd, paths.at("/run/user"), paths.at("/var/lib"),
                 paths.home, paths.tmp):
        path.mkdir(parents=True)
    source = tmp_path / "player.service"
    source.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    paths.wall_run.mkdir(parents=True)

    monkeypatch.setattr(check.os, "geteuid", lambda: 0)
    monkeypatch.setattr(check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(check, "_systemd_major", lambda: 255)
    monkeypatch.setattr(check, "_pid1_is_systemd", lambda: True)
    monkeypatch.setattr(check.shutil, "which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(check.pwd, "getpwnam", lambda _: (_ for _ in ()).throw(KeyError()))
    monkeypatch.setattr(check.pwd, "getpwuid", lambda _: (_ for _ in ()).throw(KeyError()))

    with pytest.raises(check.PreflightError, match="preexisting_path"):
        check._check_environment(paths, source)


def test_environment_rejects_existing_wall_group_before_account_mutation(tmp_path, monkeypatch):
    paths = check.Paths(tmp_path)
    for path in (paths.run, paths.systemd, paths.at("/run/user"), paths.at("/var/lib"),
                 paths.home, paths.tmp):
        path.mkdir(parents=True)
    source = tmp_path / "player.service"
    source.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(check.os, "geteuid", lambda: 0)
    monkeypatch.setattr(check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(check, "_systemd_major", lambda: 255)
    monkeypatch.setattr(check, "_pid1_is_systemd", lambda: True)
    monkeypatch.setattr(check.shutil, "which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(check.grp, "getgrnam", lambda _: object())

    with pytest.raises(check.PreflightError, match="wall_group_exists"):
        check._check_environment(paths, source)


def test_probe_is_uid_bounded_and_contains_only_public_checks():
    text = check._probe_text(leaf="/run/photo-wall/player/leafhealth",
                             state_leaf="/var/lib/photo-wall/preflight-state",
                             boot="/run/photo-wall/boot.json",
                             socket_path="/run/user/10001/wayland-0",
                             runtime="/run/user/10001/write-test",
                             home_canary="/home/pw-check-canary",
                             tmp_canary="/tmp/pw-check-canary")
    compile(text, "probe", "exec")
    assert "geteuid() != 10001" in text
    assert "errno.EROFS" in text
    assert "must-not-write" in text
    assert "private" not in text.lower()
    assert "credential" not in text.lower()


def test_socket_cleanup_unlinks_only_real_socket():
    path = pathlib.Path("/tmp") / ("pw-check-test-" + uuid.uuid4().hex)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            listener.bind(str(path))
        except PermissionError:
            pytest.skip("sandbox does not permit filesystem Unix sockets")
        listener.listen(1)
        assert check._unlink(path, socket_ok=True)
        assert not path.exists()
    finally:
        listener.close()
        if path.exists():
            path.unlink()


def test_cleanup_accepts_userdel_removing_primary_group(monkeypatch):
    preflight = check.Preflight(SOURCE)
    preflight.created.group = True
    monkeypatch.setattr(check.grp, "getgrnam",
                        lambda _: (_ for _ in ()).throw(KeyError()))
    monkeypatch.setattr(check, "_try_command",
                        lambda *_args, **_kwargs: pytest.fail("groupdel must be skipped"))
    assert preflight._remove_created_group()


def test_run_orders_creation_start_and_cleanup_and_leaves_no_owned_files(tmp_path, monkeypatch):
    paths = check.Paths(tmp_path)
    for path in (paths.run, paths.systemd, paths.at("/run/user"), paths.at("/var/lib"),
                 paths.home, paths.tmp):
        path.mkdir(parents=True)
    source = tmp_path / "player.service"
    source.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    events = []
    monkeypatch.setattr(check, "_source_text", lambda _: events.append("source") or source.read_text())
    monkeypatch.setattr(check, "_check_environment", lambda *_: events.append("environment"))
    monkeypatch.setattr(check, "_chown_mode", lambda *_: None)
    monkeypatch.setattr(check.Preflight, "_boot_record_intact", lambda self: True)
    monkeypatch.setattr(check, "WALL_UID", 0)

    class FakeListener:
        def __init__(self, *_args):
            self.accepted = threading.Event()
            self.accepted.set()

        def start(self):
            events.append("listener")

        def close(self):
            events.append("listener-close")

    monkeypatch.setattr(check, "_SocketListener", FakeListener)

    def command(args, **_kwargs):
        if args[1] == "stop":
            leaf = paths.wall_run / "player" / "leafhealth"
            leaf.unlink()
            (paths.wall_run / "player").rmdir()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(check, "_command", command)

    original_account = check.Preflight._create_account
    monkeypatch.setattr(check.Preflight, "_create_account",
                        lambda self: events.append("account") or (0, 0))
    preflight = check.Preflight(source, paths=paths)
    original_prepare = preflight._prepare_files
    original_install = preflight._install_unit
    original_start = preflight._start_and_verify

    def prepare(uid, gid):
        events.append("prepare")
        original_prepare(uid, gid)

    def install(text):
        events.append("install")
        original_install(text)

    def start():
        events.append("start")
        # Simulate RuntimeDirectory and the public probe's leaf write, while
        # retaining the real listener and cleanup path.
        (paths.wall_run / "player").mkdir(mode=0o700)
        (paths.wall_run / "player" / "leafhealth").write_bytes(b"photo-wall-preflight\n")
        preflight.started = True
        preflight.verified = True
        assert preflight.listener is not None

    preflight._prepare_files = prepare
    preflight._install_unit = install
    preflight._start_and_verify = start
    result = preflight.run()

    assert result["status"] == "passed"
    assert result["checks"]["cleanup"] == "passed"
    assert events == ["source", "environment", "account", "prepare", "listener", "install",
                      "start", "listener-close"]
    assert not paths.wall_run.exists()
    assert not paths.user_run.exists()
    assert not paths.state.exists()
    assert original_account is not None and original_start is not None


def test_main_reports_only_sanitized_failure(monkeypatch, capsys):
    monkeypatch.setattr(check.Preflight, "run",
                        lambda self: (_ for _ in ()).throw(check.PreflightError("unit_start_failed")))
    assert check.main(["--unit-source", str(SOURCE)]) == 1
    output = capsys.readouterr().out
    assert output.strip() == '{"reason": "unit_start_failed", "schema": 1, "status": "failed"}'
    assert "/" not in output.replace("/", "", 1)
