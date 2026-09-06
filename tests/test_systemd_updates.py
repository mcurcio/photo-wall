"""Opt-in integration coverage for the installed update systemd units.

These tests are intentionally skipped on ordinary developer hosts.  The opt-in
environment must be a disposable Linux systemd 255 container running as root;
the test refuses every production path it owns before creating anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from appliance import updates
from appliance.updates import SlotStore
from contracts.release import Release, configuration_digest

PYTHON = Path("/usr/bin/python3.12")
ABI = "b" * 64
CONFIG_FILES = {
    "public.json": b'{"schema":1}\n',
    "ca.pem": b"public test CA\n",
    "bootstrap.json": (b'{"schema":1,"release_origin":"https://wall.example",'
                        b'"time_server":"wall.example"}'),
}


def _systemd_255_root() -> bool:
    if (os.environ.get("PHOTO_WALL_TEST_SYSTEMD_UPDATES") != "1"
            or sys.platform != "linux" or os.geteuid() != 0):
        return False
    try:
        pid1 = Path("/proc/1/comm").read_text(encoding="ascii").strip()
        result = subprocess.run(["systemd", "--version"], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    first = result.stdout.splitlines()[:1]
    return pid1 == "systemd" and result.returncode == 0 and first and first[0].startswith("systemd 255")


pytestmark = pytest.mark.skipif(
    not _systemd_255_root(),
    reason="set PHOTO_WALL_TEST_SYSTEMD_UPDATES=1 in a disposable root systemd-255 Linux container",
)


def _command(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


class SystemdRig:
    """Own the exact absolute paths consumed by the production units."""

    def __init__(self):
        self.root = Path("/var/lib/photo-wall")
        self.config = Path("/etc/photo-wall")
        self.runtime = Path("/run/photo-wall")
        self.accept_unit = Path("/etc/systemd/system/photo-wall-accept-trial.service")
        self.recovery_unit = Path("/etc/systemd/system/photo-wall-trial-recovery.service")
        self.recovery_dropin = self.recovery_unit.with_name("photo-wall-trial-recovery.service.d")
        self.module_parent: Path | None = None
        self.module_dirs: list[Path] = []
        self.owned_root = False
        self.owned_config = False
        self.owned_runtime = False
        self.owned_accept_unit = False
        self.owned_recovery_unit = False
        self.owned_recovery_dropin = False
        self.private = Ed25519PrivateKey.generate()
        self.store: SlotStore | None = None
        self.recovery_marker = self.root / "recovery-marker"
        self.health_stop: threading.Event | None = None
        self.health_thread: threading.Thread | None = None

    def _import_parent(self) -> Path:
        result = _command([str(PYTHON), "-I", "-c", "import sys; print('\\n'.join(sys.path))"], 10)
        if result.returncode:
            raise RuntimeError("python312_isolated_import_path_unavailable")
        candidates = [Path(line) for line in result.stdout.splitlines()
                      if line.startswith("/") and ("site-packages" in line or "dist-packages" in line)]
        if any(os.path.lexists(candidate / package) for candidate in candidates
               for package in ("appliance", "contracts")):
            raise RuntimeError("python312_test_module_preexisting")
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        raise RuntimeError("python312_import_path_preexisting_or_unavailable")

    def _refuse_preexisting(self) -> None:
        paths = [self.root, self.config, self.runtime, self.accept_unit, self.recovery_unit,
                 self.recovery_dropin]
        for path in paths:
            if os.path.lexists(path):
                raise RuntimeError("preexisting_test_path:" + str(path))
        for unit in (self.accept_unit.name, self.recovery_unit.name):
            loaded = self._systemctl("show", unit, "--property=FragmentPath", "--value", timeout=10)
            if loaded.stdout.strip():
                raise RuntimeError("preexisting_test_unit")
        self.module_parent = self._import_parent()

    def setup(self) -> None:
        if not PYTHON.is_file():
            raise RuntimeError("python312_missing")
        self._refuse_preexisting()
        assert self.module_parent is not None
        self.root.mkdir(mode=0o755)
        self.owned_root = True
        (self.root / ".photo-wall-state-v1").write_bytes(updates.MARKER)
        (self.root / ".photo-wall-state-v1").chmod(0o444)
        player = self.root / "player"
        player.mkdir(mode=0o700)
        (player / "identity.key").write_bytes(b"private fixture identity")
        (player / "cache.pin").write_bytes(b"scheduled secured fixture")

        self.config.mkdir(mode=0o755)
        self.owned_config = True
        public = dict(CONFIG_FILES)
        public["release.pub.pem"] = self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        config_hash = configuration_digest(public)
        for name, data in public.items():
            (self.config / name).write_bytes(data)
            (self.config / name).chmod(0o644)
        (self.config / "boot-policy.json").write_text(
            json.dumps({"schema": 1, "boot_abi": ABI, "configuration_sha256": config_hash}),
            encoding="utf-8")
        (self.config / "boot-policy.json").chmod(0o644)
        self.store = SlotStore(self.root, self.config / "release.pub.pem", ABI, config_hash)

        for package, names in (("appliance", ("__init__.py", "bootstrap.py", "updates.py")),
                               ("contracts", ("__init__.py", "release.py"))):
            destination = self.module_parent / package
            if os.path.lexists(destination):
                raise RuntimeError("preexisting_test_module:" + str(destination))
            destination.mkdir(mode=0o755)
            self.module_dirs.append(destination)
            for name in names:
                target = destination / name
                shutil.copyfile(Path(__file__).parents[1] / package / name, target)
                target.chmod(0o644)

        shutil.copyfile(Path(__file__).parents[1] / "appliance/systemd/accept-trial.service",
                        self.accept_unit)
        self.owned_accept_unit = True
        shutil.copyfile(Path(__file__).parents[1] / "appliance/systemd/trial-recovery.service",
                        self.recovery_unit)
        self.owned_recovery_unit = True
        self.install_marker_recovery()
        loaded = self._systemctl("daemon-reload", timeout=20)
        if loaded.returncode:
            raise RuntimeError("systemd_daemon_reload_failed")

    def _systemctl(self, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return _command(["systemctl", *args], timeout)

    def signed(self, data: bytes, revision: str) -> tuple[Release, bytes, bytes]:
        release = Release(revision=revision, boot_abi=ABI,
                          configuration_sha256=self.store.configuration_sha256,
                          rootfs_size=len(data), rootfs_sha256=hashlib.sha256(data).hexdigest())
        manifest = release.encode()
        return release, manifest, self.private.sign(manifest)

    def stage(self, data: bytes, revision: str) -> Release:
        assert self.store is not None
        release, manifest, signature = self.signed(data, revision)
        assert self.store.stage(manifest, signature, [data]) == release
        return release

    def write_report(self, *, boot_id: str, release: Release, slot: str | None, trial: bool) -> Path:
        self.runtime.mkdir(mode=0o755)
        self.owned_runtime = True
        path = self.runtime / "boot.json"
        path.write_text(json.dumps(dict(schema=1, boot_id=boot_id, release_id=release.release_id,
                                        slot=slot, trial=trial, persistence="durable", fault=None)),
                        encoding="utf-8")
        path.chmod(0o600)
        return path

    def start_health(self, boot_id: str, *, healthy: bool = True) -> None:
        player = self.runtime / "player"
        player.mkdir(mode=0o700, exist_ok=True)
        path = player / "service-health.json"
        self.health_stop = threading.Event()

        def publish() -> None:
            while not self.health_stop.is_set():
                temporary = player / ".service-health.test"
                temporary.write_text(json.dumps(dict(
                    boot_id=boot_id, sampled_monotonic=time.monotonic(), player_id="systemd-test",
                    authority_epoch=1, persistence="durable", healthy=healthy)), encoding="utf-8")
                temporary.chmod(0o600)
                os.replace(temporary, path)
                self.health_stop.wait(.1)

        self.health_thread = threading.Thread(target=publish, daemon=True)
        self.health_thread.start()

    def stop_health(self) -> None:
        if self.health_stop is not None:
            self.health_stop.set()
        if self.health_thread is not None:
            self.health_thread.join(timeout=2)
        self.health_stop = self.health_thread = None

    def install_marker_recovery(self) -> Path:
        self.recovery_dropin.mkdir(mode=0o755)
        self.owned_recovery_dropin = True
        (self.recovery_dropin / "test.conf").write_text(
            "[Service]\nExecStart=\nExecStart=/usr/bin/touch /var/lib/photo-wall/recovery-marker\n",
            encoding="utf-8")
        (self.recovery_dropin / "test.conf").chmod(0o644)
        loaded = self._systemctl("daemon-reload", timeout=20)
        if loaded.returncode:
            raise RuntimeError("systemd_daemon_reload_failed")
        shown = self._systemctl("show", "photo-wall-trial-recovery.service",
                                "--property=ExecStart", "--value", timeout=20)
        if (shown.returncode or shown.stdout.count("path=") != 1
                or "path=/usr/bin/touch ;" not in shown.stdout
                or "argv[]=/usr/bin/touch /var/lib/photo-wall/recovery-marker ;" not in shown.stdout
                or "reboot" in shown.stdout):
            raise RuntimeError("recovery_marker_override_failed")
        return self.recovery_marker

    def cleanup(self) -> None:
        self.stop_health()
        if self.owned_accept_unit:
            self._systemctl("stop", "photo-wall-accept-trial.service", timeout=20)
            self._systemctl("reset-failed", "photo-wall-accept-trial.service", timeout=20)
        if self.owned_recovery_unit:
            self._systemctl("stop", "photo-wall-trial-recovery.service", timeout=20)
            self._systemctl("reset-failed", "photo-wall-trial-recovery.service", timeout=20)
        if self.owned_accept_unit or self.owned_recovery_unit:
            self._systemctl("daemon-reload", timeout=20)
        if self.owned_accept_unit and self.accept_unit.is_file() and not self.accept_unit.is_symlink():
            self.accept_unit.unlink()
        if self.owned_recovery_unit and self.recovery_unit.is_file() \
                and not self.recovery_unit.is_symlink():
            self.recovery_unit.unlink()
        if self.owned_recovery_dropin and self.recovery_dropin.is_dir() \
                and not self.recovery_dropin.is_symlink():
            shutil.rmtree(self.recovery_dropin)
        if self.owned_accept_unit or self.owned_recovery_unit:
            self._systemctl("daemon-reload", timeout=20)
        for path, owned in ((self.runtime, self.owned_runtime),
                            (self.config, self.owned_config), (self.root, self.owned_root)):
            if not owned:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        for package in self.module_dirs:
            if package.is_dir() and not package.is_symlink():
                shutil.rmtree(package)


@pytest.fixture
def systemd_rig():
    rig = SystemdRig()
    try:
        rig.setup()
        yield rig
    finally:
        rig.cleanup()


def _actual_boot_id() -> str:
    return updates._linux_boot_id()


def _seed_fallback_and_trial(rig: SystemdRig) -> tuple[Release, Release, str, str]:
    assert rig.store is not None
    base = rig.stage(b"validated base", "a" * 40)
    historical = "historical-boot"
    selected = rig.store.select_boot(historical)
    assert selected and selected.trial
    assert rig.store.mark_good(base.release_id, historical)
    candidate = rig.stage(b"candidate trial", "d" * 40)
    boot_id = _actual_boot_id()
    selected = rig.store.select_boot(boot_id)
    assert selected and selected.trial and selected.release == candidate
    return base, candidate, selected.slot, boot_id


def _assert_active(rig: SystemdRig, release_id: str) -> None:
    state = json.loads((rig.root / "updates/state.json").read_text(encoding="utf-8"))
    assert state["active"]["release_id"] == release_id
    assert state["selected"]["accepted"] is True
    assert (rig.root / "player/identity.key").read_bytes() == b"private fixture identity"
    assert (rig.root / "player/cache.pin").read_bytes() == b"scheduled secured fixture"


def _persistent_bytes(rig: SystemdRig) -> dict[str, bytes]:
    return {str(path.relative_to(rig.root)): path.read_bytes()
            for directory in (rig.root / "updates", rig.root / "player")
            for path in directory.rglob("*") if path.is_file()}


def test_production_acceptance_unit_promotes_actual_trial(systemd_rig):
    rig = systemd_rig
    _base, candidate, slot, boot_id = _seed_fallback_and_trial(rig)
    rig.write_report(boot_id=boot_id, release=candidate, slot=slot, trial=True)
    rig.start_health(boot_id)
    started = time.monotonic()
    result = rig._systemctl("start", "photo-wall-accept-trial.service", timeout=205)
    elapsed = time.monotonic() - started
    rig.stop_health()
    assert result.returncode == 0
    assert 30 <= elapsed < 200
    assert not rig.recovery_marker.exists()
    print(json.dumps({"scenario": "healthy_trial", "elapsed_seconds": round(elapsed, 3)}))
    _assert_active(rig, candidate.release_id)


def test_acceptance_failure_triggers_recovery_only_for_verified_fallback(systemd_rig):
    rig = systemd_rig
    _base, candidate, slot, boot_id = _seed_fallback_and_trial(rig)
    rig.write_report(boot_id=boot_id, release=candidate, slot=slot, trial=True)
    marker = rig.recovery_marker
    before = _persistent_bytes(rig)
    started = time.monotonic()
    result = rig._systemctl("start", "photo-wall-accept-trial.service", timeout=205)
    elapsed = time.monotonic() - started
    assert result.returncode != 0, "acceptance unit unexpectedly succeeded"
    assert 180 <= elapsed < 200, "acceptance did not reach the production health deadline"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(.2)
    assert marker.is_file()
    assert _persistent_bytes(rig) == before
    state = json.loads((rig.root / "updates/state.json").read_text(encoding="utf-8"))
    assert state["active"]["release_id"] == _base.release_id
    assert state["selected"]["release_id"] == candidate.release_id
    assert state["selected"]["accepted"] is False
    print(json.dumps({"scenario": "failed_trial_recovery", "elapsed_seconds": round(elapsed, 3)}))


@pytest.mark.parametrize("scenario", ["no_active", "common", "accepted"])
def test_recovery_unit_condition_skips_nonqualifying_boots(systemd_rig, scenario):
    rig = systemd_rig
    assert rig.store is not None
    marker = rig.recovery_marker
    boot_id = _actual_boot_id()
    if scenario == "no_active":
        candidate = rig.stage(b"first trial", "a" * 40)
        selected = rig.store.select_boot(boot_id)
        assert selected and selected.trial
        rig.write_report(boot_id=boot_id, release=candidate, slot=selected.slot, trial=True)
    elif scenario == "accepted":
        base = rig.stage(b"accepted base", "a" * 40)
        historical = "historical-boot"
        selected = rig.store.select_boot(historical)
        assert selected and rig.store.mark_good(base.release_id, historical)
        selected = rig.store.select_boot(boot_id)
        assert selected and not selected.trial
        rig.write_report(boot_id=boot_id, release=base, slot=selected.slot, trial=False)
    else:
        base = rig.stage(b"common fallback", "a" * 40)
        rig.write_report(boot_id=boot_id, release=base, slot=None, trial=False)
    before = _persistent_bytes(rig)
    result = rig._systemctl("start", "photo-wall-trial-recovery.service", timeout=140)
    assert result.returncode == 0, "recovery condition unexpectedly failed as a unit start"
    assert not marker.exists()
    assert _persistent_bytes(rig) == before
