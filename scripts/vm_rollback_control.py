"""Test-only request to reboot one exact boot after central stages a release.

The read-only control share contains no commands or paths. Central owns release
selection; this helper can only initiate the requested trial boot. Production's
watchdog must perform the subsequent failed-trial reboot without host assistance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from appliance import updates

CONTROL_BYTES = 4096
WATCH_TIMEOUT = 3600
POLL_INTERVAL = 1
BOOT_ID_PATTERN = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
DIGEST = re.compile(r"[a-f0-9]{64}")
BOOT_REPORT = Path("/run/photo-wall/boot.json")
CONTROL = Path("/run/photo-wall-ci/control.json")


class ControlError(ValueError):
    pass


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
    except (updates.UpdateError, UnicodeError):
        raise ControlError("invalid_linux_boot_id") from None
    if not BOOT_ID_PATTERN.fullmatch(value):
        raise ControlError("invalid_linux_boot_id")
    return value


def _control(path: Path) -> dict | None:
    if not os.path.lexists(path):
        return None
    try:
        value = json.loads(updates._regular(path, CONTROL_BYTES), object_pairs_hook=_unique)
        if (not isinstance(value, dict) or set(value) != {"schema", "action", "current", "candidate"}
                or type(value["schema"]) is not int or value["schema"] != 2
                or value["action"] != "reboot-for-trial"):
            raise ValueError
        current, candidate = value["current"], value["candidate"]
        if (not isinstance(current, dict)
                or set(current) != {"boot_id", "device_id", "ticket_sha256", "release_id"}
                or not isinstance(current["boot_id"], str) or not BOOT_ID_PATTERN.fullmatch(current["boot_id"])
                or not isinstance(current["device_id"], str)
                or not re.fullmatch(r"device-[a-f0-9]{64}", current["device_id"])
                or any(not isinstance(current[k], str) or not DIGEST.fullmatch(current[k])
                       for k in ("ticket_sha256", "release_id"))
                or not isinstance(candidate, dict) or set(candidate) != {"release_id"}
                or not isinstance(candidate["release_id"], str) or not DIGEST.fullmatch(candidate["release_id"])
                or candidate["release_id"] == current["release_id"]):
            raise ValueError
        return value
    except ControlError:
        raise
    except (updates.UpdateError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ControlError("invalid_control") from None


def _report(path: Path, boot_id: str) -> dict:
    try:
        value = updates.boot_report(path, boot_id)
    except (updates.UpdateError, OSError):
        raise ControlError("invalid_boot_report") from None
    if value["trial"] or value.get("persistence") != "volatile" or value.get("fault") is not None:
        raise ControlError("boot_not_accepted")
    return value


def run_watcher(*, boot_report: Path = BOOT_REPORT,
                boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"), control: Path = CONTROL,
                command_runner: Callable = subprocess.run, sleep: Callable = time.sleep,
                monotonic: Callable = time.monotonic, timeout: float = WATCH_TIMEOUT) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        boot_id = _boot_id(boot_id_path)
        try:
            report = _report(boot_report, boot_id)
        except ControlError as error:
            if str(error) == "boot_not_accepted":
                return False
            raise
        item = _control(control)
        if item is None:
            sleep(min(POLL_INTERVAL, max(0, deadline - monotonic())))
            continue
        if item["current"]["boot_id"] != boot_id:
            return False
        expected = {k: report[k] for k in ("boot_id", "device_id", "release_id")}
        expected["ticket_sha256"] = hashlib.sha256(report["ticket_id"].encode()).hexdigest()
        if item["current"] != expected:
            raise ControlError("control_boot_mismatch")
        print(json.dumps(dict(event="photo-wall-trial-reboot-requested", **expected,
                              candidate_release_id=item["candidate"]["release_id"])), flush=True)
        try:
            result = command_runner(["/usr/bin/systemctl", "--no-block", "reboot"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ControlError("control_command_failed") from None
        if result.returncode:
            raise ControlError("control_command_failed")
        return True
    return False


def main():
    try:
        run_watcher()
    except (ControlError, OSError, ValueError):
        raise SystemExit("photo-wall-ci: control_failed") from None


if __name__ == "__main__":
    main()
