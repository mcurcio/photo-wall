"""Run a short, disposable Linux/systemd preflight for the Player unit.

This is intentionally a host-side check.  It creates one temporary ``wall``
account and one uniquely named unit, then removes only objects created by the
check.  The normal CI runner is the only supported execution environment.
"""

from __future__ import annotations

import argparse
import dataclasses
import grp
import json
import os
import pathlib
import platform
import pwd
import re
import shutil
import socket
import stat
import subprocess
import threading
import uuid

WALL_NAME = "wall"
WALL_UID = 10001
SYSTEMD_MAJOR = 255
MAX_UNIT_BYTES = 128 * 1024
MAX_COMMAND_SECONDS = 40
MAX_START_SECONDS = 45
MAX_SOURCE_TEXT = 128 * 1024


class PreflightError(Exception):
    """A public, sanitized preflight failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class Paths:
    """Absolute host paths, rooted at ``/`` in production and a temp root in tests."""

    root: pathlib.Path = pathlib.Path("/")

    def at(self, absolute: str) -> pathlib.Path:
        if not absolute.startswith("/"):
            raise ValueError("absolute path required")
        return self.root / absolute.lstrip("/")

    @property
    def run(self) -> pathlib.Path:
        return self.at("/run")

    @property
    def systemd(self) -> pathlib.Path:
        return self.at("/run/systemd/system")

    @property
    def wall_run(self) -> pathlib.Path:
        return self.at("/run/photo-wall")

    @property
    def user_run(self) -> pathlib.Path:
        return self.at("/run/user/10001")

    @property
    def state(self) -> pathlib.Path:
        return self.at("/var/lib/photo-wall")

    @property
    def home(self) -> pathlib.Path:
        return self.at("/home")

    @property
    def tmp(self) -> pathlib.Path:
        return self.at("/tmp")


@dataclasses.dataclass
class Created:
    unit: pathlib.Path | None = None
    wall_run: pathlib.Path | None = None
    boot_record: pathlib.Path | None = None
    probe: pathlib.Path | None = None
    home_canary: pathlib.Path | None = None
    tmp_canary: pathlib.Path | None = None
    user_run: pathlib.Path | None = None
    socket_path: pathlib.Path | None = None
    state: pathlib.Path | None = None
    player_state: pathlib.Path | None = None
    user: bool = False
    group: bool = False


def _command(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a fixed command without ever returning its output to the caller."""
    try:
        return subprocess.run(argv, check=False, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError("command_timeout" if isinstance(exc, subprocess.TimeoutExpired)
                             else "command_unavailable") from None


def _ok(argv: list[str], *, timeout: float, failure: str) -> subprocess.CompletedProcess[str]:
    result = _command(argv, timeout=timeout)
    if result.returncode != 0:
        raise PreflightError(failure)
    return result


def _regular(path: pathlib.Path, code: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise PreflightError(code) from None
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise PreflightError(code)


def _directory(path: pathlib.Path, code: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise PreflightError(code) from None
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise PreflightError(code)


def _absent(path: pathlib.Path, code: str = "preexisting_path") -> None:
    if os.path.lexists(path):
        raise PreflightError(code)


def _chown_mode(path: pathlib.Path, uid: int, gid: int, mode: int) -> None:
    os.chown(path, uid, gid)
    path.chmod(mode)


def _unlink(path: pathlib.Path, *, socket_ok: bool = False) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or
                                  (socket_ok and stat.S_ISSOCK(mode))):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _try_command(argv: list[str], *, timeout: float) -> bool:
    try:
        return _command(argv, timeout=timeout).returncode == 0
    except PreflightError:
        return False


def _systemd_major() -> int:
    result = _ok(["systemd", "--version"], timeout=10, failure="systemd_unavailable")
    match = re.match(r"^systemd\s+([0-9]+)(?:\D|$)", result.stdout)
    if not match:
        raise PreflightError("systemd_version_unreadable")
    return int(match.group(1))


def _pid1_is_systemd() -> bool:
    try:
        return pathlib.Path("/proc/1/comm").read_text(encoding="ascii").strip() == "systemd"
    except OSError:
        return False


def _check_environment(paths: Paths, source: pathlib.Path) -> None:
    if os.geteuid() != 0:
        raise PreflightError("root_required")
    if platform.system() != "Linux":
        raise PreflightError("linux_required")
    _regular(source, "unit_source_invalid")
    if source.stat().st_size > MAX_SOURCE_TEXT:
        raise PreflightError("unit_source_limit")
    _directory(paths.run, "run_directory_missing")
    _directory(paths.systemd, "systemd_directory_missing")
    _directory(paths.at("/run/user"), "user_run_parent_missing")
    _directory(paths.at("/var/lib"), "state_parent_missing")
    _directory(paths.home, "home_directory_missing")
    _directory(paths.tmp, "tmp_directory_missing")
    if _systemd_major() != SYSTEMD_MAJOR:
        raise PreflightError("systemd_version_mismatch")
    if not _pid1_is_systemd():
        raise PreflightError("systemd_pid1_required")
    for executable in ("useradd", "userdel", "groupadd", "groupdel", "systemctl"):
        if shutil.which(executable) is None:
            raise PreflightError("required_tool_missing")
    try:
        grp.getgrnam(WALL_NAME)
        raise PreflightError("wall_group_exists")
    except KeyError:
        pass
    try:
        pwd.getpwnam(WALL_NAME)
        raise PreflightError("wall_name_exists")
    except KeyError:
        pass
    try:
        pwd.getpwuid(WALL_UID)
        raise PreflightError("wall_uid_exists")
    except KeyError:
        pass
    # These are the paths the check owns.  All are rejected before user/group
    # creation so a rerun cannot replace an unrelated runtime or state tree.
    for path in (paths.wall_run, paths.user_run, paths.state):
        _absent(path)


def _source_text(source: pathlib.Path) -> str:
    try:
        with source.open("r", encoding="utf-8") as stream:
            text = stream.read(MAX_SOURCE_TEXT + 1)
    except (OSError, UnicodeError):
        raise PreflightError("unit_source_invalid") from None
    if len(text.encode("utf-8")) > MAX_SOURCE_TEXT:
        raise PreflightError("unit_source_limit")
    required = (
        "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=read-only",
        "InaccessiblePaths=-/home -/root", "PrivateTmp=yes", "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes", "ProtectControlGroups=yes", "RestrictSUIDSGID=yes",
        "ReadWritePaths=/run/photo-wall/player", "ReadWritePaths=-/var/lib/photo-wall/player",
        "ExecStartPre=-/usr/bin/timeout 15 /bin/sh -c 'until test -S /run/user/10001/wayland-0; do sleep 0.1; done'",
    )
    if any(line not in text.splitlines() for line in required):
        raise PreflightError("unit_sandbox_missing")
    return text


def render_unit(source_text: str, unit_name: str, probe_path: str) -> str:
    """Append only the explicitly test-scoped unit overrides."""
    if not re.fullmatch(r"pw-check-[0-9a-f]{32}\.service", unit_name):
        raise ValueError("invalid unit name")
    if not probe_path.startswith("/run/photo-wall/"):
        raise ValueError("invalid probe path")
    text = source_text.rstrip("\n")
    return (text + "\n\n[Unit]\nWants=\nAfter=\n"
            "\n[Service]\nType=oneshot\nRemainAfterExit=yes\nRestart=no\nTimeoutStartSec=30s\n"
            "ExecStart=\nExecStart=/usr/bin/python3 " + probe_path + "\n")


def _probe_text(*, leaf: str, state_leaf: str, boot: str, socket_path: str, runtime: str,
                home_canary: str, tmp_canary: str) -> str:
    """Return the public probe; all paths are fixed test-owned values."""
    values = {name: json.dumps(value) for name, value in {
        "leaf": leaf, "state_leaf": state_leaf, "boot": boot,
        "socket_path": socket_path, "runtime": runtime,
        "home_canary": home_canary, "tmp_canary": tmp_canary,
    }.items()}
    return f'''#!/usr/bin/python3
import errno
import os
import socket

if os.geteuid() != 10001:
    raise SystemExit(10)

with open({values["leaf"]}, "wb") as stream:
    stream.write(b"photo-wall-preflight\\n")
with open({values["state_leaf"]}, "wb") as stream:
    stream.write(b"photo-wall-state\\n")

try:
    with open({values["boot"]}, "ab") as stream:
        stream.write(b"must-not-write\\n")
except OSError as error:
    if error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
        raise SystemExit(11) from error
else:
    raise SystemExit(12)

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(5)
    client.connect({values["socket_path"]})
    client.sendall(b"photo-wall-preflight\\n")

try:
    with open({values["runtime"]}, "xb") as stream:
        stream.write(b"must-not-write\\n")
except OSError as error:
    if error.errno != errno.EROFS:
        raise SystemExit(13) from error
else:
    raise SystemExit(14)

for path, code in (({values["home_canary"]}, 15), ({values["tmp_canary"]}, 16)):
    try:
        with open(path, "rb"):
            raise SystemExit(code)
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.EACCES, errno.ENOTDIR):
            raise SystemExit(code + 10) from error
'''


class _SocketListener:
    def __init__(self, path: pathlib.Path, uid: int, gid: int):
        self.path = path
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(os.fspath(path))
        self._socket.listen(1)
        os.chown(path, uid, gid)
        path.chmod(0o660)
        self.accepted = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)

    def _accept(self) -> None:
        try:
            self._socket.settimeout(20)
            connection, _ = self._socket.accept()
            with connection:
                connection.settimeout(5)
                connection.recv(128)
            self.accepted.set()
        except OSError:
            return

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


class Preflight:
    def __init__(self, source: pathlib.Path, *, paths: Paths | None = None):
        self.source = source
        self.paths = paths or Paths()
        self.created = Created()
        self.unit_name = "pw-check-" + uuid.uuid4().hex + ".service"
        self.listener: _SocketListener | None = None
        self.started = False
        self.verified = False

    def _create_account(self) -> tuple[int, int]:
        try:
            group = grp.getgrnam(WALL_NAME)
        except KeyError:
            _ok(["groupadd", "--system", WALL_NAME], timeout=20, failure="group_create_failed")
            self.created.group = True
            try:
                group = grp.getgrnam(WALL_NAME)
            except KeyError:
                raise PreflightError("group_create_failed") from None
        _ok(["useradd", "--system", "--uid", str(WALL_UID), "--gid", str(group.gr_gid),
             "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin",
             WALL_NAME], timeout=20, failure="user_create_failed")
        self.created.user = True
        try:
            account = pwd.getpwnam(WALL_NAME)
        except KeyError:
            raise PreflightError("user_create_failed") from None
        if account.pw_uid != WALL_UID or account.pw_gid != group.gr_gid:
            raise PreflightError("user_identity_invalid")
        return account.pw_uid, account.pw_gid

    def _prepare_files(self, uid: int, gid: int) -> None:
        paths = self.paths
        self.created.wall_run = paths.wall_run
        paths.wall_run.mkdir(mode=0o755)
        _chown_mode(paths.wall_run, 0, 0, 0o755)
        self.created.boot_record = paths.wall_run / "boot.json"
        self.created.boot_record.write_text(json.dumps({"schema": 1, "synthetic": True}) + "\n",
                                            encoding="utf-8")
        _chown_mode(self.created.boot_record, 0, 0, 0o600)

        self.created.state = paths.state
        paths.state.mkdir(mode=0o755)
        _chown_mode(paths.state, 0, 0, 0o755)
        self.created.player_state = paths.state / "player"
        self.created.player_state.mkdir(mode=0o700)
        _chown_mode(self.created.player_state, uid, gid, 0o700)

        self.created.user_run = paths.user_run
        paths.user_run.mkdir(mode=0o700)
        _chown_mode(paths.user_run, uid, gid, 0o700)
        self.created.socket_path = paths.user_run / "wayland-0"
        self.listener = _SocketListener(self.created.socket_path, uid, gid)
        self.listener.start()

        token = uuid.uuid4().hex
        self.created.home_canary = paths.home / ("pw-check-" + token)
        self.created.tmp_canary = paths.tmp / ("pw-check-" + token)
        for canary in (self.created.home_canary, self.created.tmp_canary):
            _absent(canary, "canary_collision")
            canary.write_bytes(b"public preflight canary\n")
            _chown_mode(canary, 0, 0, 0o644)

        self.created.probe = paths.wall_run / "pw-check-probe.py"
        self.created.probe.write_text(_probe_text(
            leaf="/run/photo-wall/player/leafhealth",
            state_leaf="/var/lib/photo-wall/player/preflight-state",
            boot="/run/photo-wall/boot.json",
            socket_path="/run/user/10001/wayland-0",
            runtime="/run/user/10001/pw-check-must-be-read-only",
            home_canary="/" + str(self.created.home_canary.relative_to(paths.root)),
            tmp_canary="/" + str(self.created.tmp_canary.relative_to(paths.root)),
        ), encoding="utf-8")
        _chown_mode(self.created.probe, 0, 0, 0o755)

    def _install_unit(self, source_text: str) -> None:
        target = self.paths.systemd / self.unit_name
        _absent(target, "unit_collision")
        target.write_text(render_unit(source_text, self.unit_name, "/run/photo-wall/pw-check-probe.py"),
                          encoding="utf-8")
        _chown_mode(target, 0, 0, 0o644)
        self.created.unit = target
        _ok(["systemctl", "daemon-reload"], timeout=MAX_COMMAND_SECONDS,
            failure="daemon_reload_failed")

    def _start_and_verify(self) -> None:
        self.started = True
        _ok(["systemctl", "start", self.unit_name], timeout=MAX_START_SECONDS,
            failure="unit_start_failed")
        status = _ok(["systemctl", "show", self.unit_name, "--property=Result",
                      "--property=ExecMainStatus", "--property=ActiveState"],
                     timeout=MAX_COMMAND_SECONDS, failure="unit_status_failed")
        values = {}
        for line in status.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"Result", "ExecMainStatus", "ActiveState"}:
                values[key] = value.strip()
        if (values.get("Result") != "success" or values.get("ExecMainStatus") != "0"
                or values.get("ActiveState") != "active"):
            raise PreflightError("unit_result_failed")
        if self.listener is None or not self.listener.accepted.wait(timeout=2):
            raise PreflightError("socket_probe_failed")
        leaf = self.paths.wall_run / "player" / "leafhealth"
        try:
            if leaf.read_bytes() != b"photo-wall-preflight\n":
                raise PreflightError("leafhealth_invalid")
            metadata = leaf.stat()
        except OSError:
            raise PreflightError("leafhealth_missing") from None
        if metadata.st_uid != WALL_UID or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PreflightError("leafhealth_permissions_invalid")
        state_leaf = self.paths.state / "player" / "preflight-state"
        try:
            if state_leaf.read_bytes() != b"photo-wall-state\n":
                raise PreflightError("player_state_invalid")
            state_metadata = state_leaf.stat()
        except OSError:
            raise PreflightError("player_state_missing") from None
        if state_metadata.st_uid != WALL_UID or stat.S_IMODE(state_metadata.st_mode) != 0o600:
            raise PreflightError("player_state_permissions_invalid")
        if self.created.boot_record is None:
            raise PreflightError("boot_record_missing")
        if self.created.boot_record.read_bytes() != b'{"schema": 1, "synthetic": true}\n':
            raise PreflightError("boot_record_changed")
        boot_metadata = self.created.boot_record.stat()
        if boot_metadata.st_uid != 0 or stat.S_IMODE(boot_metadata.st_mode) != 0o600:
            raise PreflightError("boot_record_permissions_invalid")
        self.verified = True

    def _boot_record_intact(self) -> bool:
        if self.created.boot_record is None:
            return False
        try:
            metadata = self.created.boot_record.stat()
            return (metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o600
                    and self.created.boot_record.read_bytes()
                    == b'{"schema": 1, "synthetic": true}\n')
        except OSError:
            return False

    def _remove_created_group(self) -> bool:
        if not self.created.group:
            return True
        try:
            grp.getgrnam(WALL_NAME)
        except KeyError:
            return True
        return _try_command(["groupdel", WALL_NAME], timeout=20)

    def _cleanup(self) -> bool:
        clean = True
        if self.started:
            clean = _try_command(["systemctl", "stop", self.unit_name], timeout=20) and clean
            runtime_player = self.paths.wall_run / "player"
            if runtime_player.exists() or runtime_player.is_symlink():
                clean = False
        if self.listener is not None:
            self.listener.close()
        if self.created.unit is not None:
            clean = _unlink(self.created.unit) and clean
        clean = _try_command(["systemctl", "daemon-reload"], timeout=MAX_COMMAND_SECONDS) and clean
        paths = [self.created.probe, self.created.home_canary, self.created.tmp_canary,
                 self.created.socket_path]
        if self.created.wall_run is not None:
            paths.extend((self.paths.wall_run / "player" / "leafhealth",
                          self.paths.state / "player" / "preflight-state"))
        boot_intact = self._boot_record_intact()
        clean = boot_intact and clean
        if boot_intact:
            paths.append(self.created.boot_record)
        for path in paths:
            if path is None:
                continue
            clean = _unlink(path, socket_ok=path == self.created.socket_path) and clean
        directories = [self.created.player_state, self.created.state, self.created.user_run]
        if self.created.wall_run is not None:
            directories.append(self.created.wall_run)
        for path in directories:
            if path is None:
                continue
            try:
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
            except OSError:
                clean = False
        if self.created.user:
            clean = _try_command(["userdel", WALL_NAME], timeout=20) and clean
        clean = self._remove_created_group() and clean
        return clean

    def run(self) -> dict:
        source_text = _source_text(self.source)
        _check_environment(self.paths, self.source)
        uid = gid = None
        try:
            uid, gid = self._create_account()
            self._prepare_files(uid, gid)
            self._install_unit(source_text)
            self._start_and_verify()
            result = {"schema": 1, "status": "passed", "checks": {
                "systemd": "255", "unit": "sandbox-and-probe", "cleanup": "pending",
            }}
        finally:
            clean = self._cleanup()
        result["checks"]["cleanup"] = "passed" if clean else "failed"
        if not clean:
            result["status"] = "failed"
            result["reason"] = "cleanup_failed"
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-source", type=pathlib.Path,
                        default=pathlib.Path(__file__).parents[1] / "appliance/systemd/player.service")
    args = parser.parse_args(argv)
    try:
        result = Preflight(args.unit_source).run()
    except PreflightError as error:
        result = {"schema": 1, "status": "failed", "reason": error.code}
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
