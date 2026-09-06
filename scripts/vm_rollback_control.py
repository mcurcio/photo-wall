"""Test-only, fixed-path control for staging one signed A/B trial update.

The host can publish a candidate bundle and then atomically publish one small
control record through a read-only 9p share.  This process accepts no command
or path from that record and delegates all slot mutations to the production
update CLI.  It is intentionally not part of the production Pi boot policy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from appliance import updates
from appliance.bootstrap import BootConfig
from contracts.release import MAX_MANIFEST_BYTES, MAX_ROOTFS_BYTES, Release

CONTROL_BYTES = 4096
COMMAND_TIMEOUT = 30
STAGE_TIMEOUT = 900
WATCH_TIMEOUT = 3600
POLL_INTERVAL = 1
PYTHON = "/usr/bin/python3.12"
BOOT_ID_PATTERN = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
RELEASE_ID_PATTERN = re.compile(r"[a-f0-9]{64}")

STATE_ROOT = Path("/var/lib/photo-wall")
CONFIG_DIR = Path("/etc/photo-wall")
BOOT_REPORT = Path("/run/photo-wall/boot.json")
CONTROL = Path("/run/photo-wall-ci/control.json")
CANDIDATE_DIR = Path("/run/photo-wall-ci/candidate")


class ControlError(ValueError):
    """A bounded public failure code without command output or credentials."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ControlError("duplicate_control_field")
        result[key] = value
    return result


def _boot_id(path: Path) -> str:
    try:
        value = updates._regular(path, 64).decode("ascii").strip()
    except (updates.UpdateError, UnicodeError) as error:
        raise ControlError("invalid_linux_boot_id") from error
    if BOOT_ID_PATTERN.fullmatch(value) is None:
        raise ControlError("invalid_linux_boot_id")
    return value


def _control(path: Path) -> dict | None:
    if not os.path.lexists(path):
        return None
    try:
        raw = updates._regular(path, CONTROL_BYTES)
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except ControlError:
        raise
    except (updates.UpdateError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ControlError("invalid_control") from error
    if (not isinstance(value, dict) or set(value) != {"schema", "action", "current", "candidate"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or value["action"] != "stage-trial"):
        raise ControlError("invalid_control")
    current, candidate = value["current"], value["candidate"]
    if not isinstance(current, dict) or set(current) != {"boot_id", "release_id", "slot"}:
        raise ControlError("invalid_control")
    if (not isinstance(current["boot_id"], str)
            or BOOT_ID_PATTERN.fullmatch(current["boot_id"]) is None
            or not isinstance(current["release_id"], str)
            or RELEASE_ID_PATTERN.fullmatch(current["release_id"]) is None
            or current["slot"] != "A"):
        raise ControlError("invalid_control")
    if (not isinstance(candidate, dict) or set(candidate) != {"release_id"}
            or not isinstance(candidate["release_id"], str)
            or RELEASE_ID_PATTERN.fullmatch(candidate["release_id"]) is None
            or candidate["release_id"] == current["release_id"]):
        raise ControlError("invalid_control")
    return value


def _report(path: Path, boot_id: str) -> dict:
    try:
        value = updates._boot_report(path, boot_id, require_trial=False)
    except updates.UpdateError as error:
        raise ControlError("invalid_boot_report") from error
    if (value["slot"] != "A" or value["trial"] is not False
            or value["persistence"] != "durable" or value["fault"] is not None):
        raise ControlError("boot_not_accepted")
    return value


def _regular_size(path: Path, maximum: int, expected: int | None = None) -> None:
    try:
        with updates._reader(path, maximum) as stream:
            size = os.fstat(stream.fileno()).st_size
    except updates.UpdateError as error:
        raise ControlError("invalid_candidate_file") from error
    if expected is not None and size != expected:
        raise ControlError("candidate_size_mismatch")


def _run(command_runner: Callable, argv: list[str], *, text: bool = True,
         timeout: int = COMMAND_TIMEOUT) -> str:
    try:
        result = command_runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=text,
                                timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ControlError("control_command_failed") from error
    if result.returncode:
        raise ControlError("control_command_failed")
    output = result.stdout if isinstance(result.stdout, str) else ""
    if len(output.encode("utf-8", errors="replace")) > CONTROL_BYTES:
        raise ControlError("control_output_limit")
    return output


def _accepted_noop(state_root: Path, config_dir: Path, boot_id: str,
                   command_runner: Callable) -> None:
    output = _run(command_runner, [PYTHON, "-I", "-m", "appliance.updates",
                                   "--state-root", str(state_root), "accept-current",
                                   "--config-dir", str(config_dir)])
    try:
        value = json.loads(output, object_pairs_hook=_unique)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ControlError("invalid_acceptance_event") from error
    if (not isinstance(value, dict) or set(value) != {"event", "boot_id", "accepted"}
            or value["event"] != "photo-wall-trial-acceptance"
            or value["boot_id"] != boot_id or value["accepted"] is not False
            or type(value["accepted"]) is not bool):
        raise ControlError("acceptance_not_noop")


def _candidate(config: BootConfig, candidate_dir: Path, release_id: str) -> tuple[Release, Path, Path, Path]:
    if candidate_dir.is_symlink() or not candidate_dir.is_dir():
        raise ControlError("candidate_directory_invalid")
    manifest_path = candidate_dir / "release.json"
    signature_path = candidate_dir / "release.sig"
    try:
        manifest_bytes = updates._regular(manifest_path, MAX_MANIFEST_BYTES)
        release = Release.decode(manifest_bytes)
    except (updates.UpdateError, ValueError) as error:
        raise ControlError("invalid_candidate_manifest") from error
    if release.release_id != release_id:
        raise ControlError("candidate_release_mismatch")
    try:
        release.require_compatible(config.boot_abi, config.configuration_sha256)
    except ValueError as error:
        raise ControlError("candidate_incompatible") from error
    try:
        updates._regular(signature_path, 64)
    except updates.UpdateError as error:
        raise ControlError("invalid_candidate_signature") from error
    rootfs_path = candidate_dir / release.rootfs_name
    _regular_size(rootfs_path, MAX_ROOTFS_BYTES, release.rootfs_size)
    return release, manifest_path, signature_path, rootfs_path


def _state_proves_staged(state_root: Path, config: BootConfig, current: dict,
                         candidate: Release) -> None:
    try:
        store = updates.SlotStore(state_root, config.directory / "release.pub.pem",
                                  config.boot_abi, config.configuration_sha256)
        with store._locked():
            state = store._state()
    except (updates.UpdateError, OSError, ValueError) as error:
        raise ControlError("staged_state_unreadable") from error
    active, pending, selected = state["active"], state["pending"], state["selected"]
    if (active != {"slot": "A", "release_id": current["release_id"]}
            or pending != {"slot": "B", "release_id": candidate.release_id, "consumed": False}
            or selected is None
            or selected["slot"] != "A" or selected["release_id"] != current["release_id"]
            or selected["boot_id"] != current["boot_id"]
            or selected["trial"] is not False or selected["accepted"] is not True):
        raise ControlError("staged_state_mismatch")


def _stage_and_reboot(state_root: Path, config_dir: Path, current: dict,
                      candidate: Release, manifest: Path, signature: Path, rootfs: Path,
                      command_runner: Callable) -> None:
    config = BootConfig.load(config_dir)
    output = _run(command_runner, [PYTHON, "-I", "-m", "appliance.updates",
                                   "--state-root", str(state_root),
                                   "--public-key", str(config.directory / "release.pub.pem"),
                                   "--boot-abi", config.boot_abi,
                                   "--configuration-sha256", config.configuration_sha256,
                                   "stage", "--manifest", str(manifest),
                                   "--signature", str(signature), "--rootfs", str(rootfs)],
                  timeout=STAGE_TIMEOUT)
    if output != candidate.release_id + "\n":
        raise ControlError("stage_result_mismatch")
    _state_proves_staged(state_root, config, current, candidate)
    print(json.dumps({"event": "photo-wall-stage-trial", "boot_id": current["boot_id"],
                      "current_release_id": current["release_id"],
                      "candidate_release_id": candidate.release_id, "slot": "B"},
                     sort_keys=True, separators=(",", ":")), flush=True)
    _run(command_runner, ["/usr/bin/systemctl", "reboot"])


def run_watcher(*, state_root: Path = STATE_ROOT, config_dir: Path = CONFIG_DIR,
                boot_report: Path = BOOT_REPORT, boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
                control: Path = CONTROL, candidate_dir: Path = CANDIDATE_DIR,
                command_runner: Callable = subprocess.run, sleep: Callable = time.sleep,
                monotonic: Callable = time.monotonic, timeout: float = WATCH_TIMEOUT) -> bool:
    """Watch one fixed control file and stage at most one candidate."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        current_boot_id = _boot_id(boot_id_path)
        try:
            report = _report(boot_report, current_boot_id)
        except ControlError as error:
            # B trials and fallback boots are valid non-actioning paths.  The
            # volatile helper must not turn those into a boot service failure.
            if str(error) == "boot_not_accepted":
                return False
            raise
        item = _control(control)
        if item is None:
            sleep(min(POLL_INTERVAL, max(0, deadline - monotonic())))
            continue
        current = item["current"]
        # A stale control is ignored before BootConfig loading or any subprocess.
        if current["boot_id"] != current_boot_id:
            return False
        if (current["release_id"] != report["release_id"] or current["slot"] != report["slot"]):
            raise ControlError("control_boot_mismatch")
        config = BootConfig.load(config_dir)
        _accepted_noop(state_root, config_dir, current_boot_id, command_runner)
        candidate, manifest, signature, rootfs = _candidate(config, candidate_dir,
                                                              item["candidate"]["release_id"])
        _stage_and_reboot(state_root, config_dir, current, candidate, manifest, signature,
                          rootfs, command_runner)
        return True
    return False


def main() -> None:
    try:
        run_watcher()
    except (ControlError, updates.UpdateError, OSError, ValueError):
        raise SystemExit("photo-wall-ci: control_failed") from None


if __name__ == "__main__":
    main()
