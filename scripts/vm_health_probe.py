"""Read-only, test-only health diagnostics for the generic VM.

The probe is deliberately separate from the Player and updater.  It reads the
fixed Player health report, a fixed Linux boot identity, three fixed systemd
units and the fixed Wayland socket.  It emits a small allowlisted projection;
it never publishes a Player identifier or copies arbitrary report or command
output.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from appliance import updates

HEALTH_PATH = Path("/run/photo-wall/player/service-health.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
WAYLAND_PATH = Path("/run/user/10001/wayland-0")
SERVICES = (
    "photo-wall-player.service",
    "photo-wall-weston.service",
    "photo-wall-accept-trial.service",
)
REPORT_BYTES = 4096
SAMPLE_LIMIT = 60
MAX_RUNTIME = 600.0
SAMPLE_INTERVAL = 10.0
MAX_AGE = 2.0
BOOT_ID_RE = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")
PLAYER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")

ACTIVE_STATES = {"active", "inactive", "activating", "deactivating", "failed", "reloading"}
SUB_STATES = {
    "dead", "running", "exited", "failed", "auto-restart", "start-pre", "start",
    "start-post", "stop", "stop-sigterm", "stop-sigkill", "final-sigterm",
    "final-sigkill", "waiting", "mounted", "listening", "plugged", "merged",
}
RESULTS = {
    "success", "exit-code", "signal", "core-dump", "timeout", "watchdog", "protocol",
    "resources", "start-limit-hit", "condition", "clean", "skipped",
}

EVENT_FIELDS = {
    "event", "boot_id", "sample_index", "report_status", "healthy", "persistence",
    "current_boot", "identity_valid", "sample_age", "services", "wayland_socket",
}
SERVICE_FIELDS = {"active_state", "sub_state", "result", "exec_main_status"}
PUBLIC_EVENT = "photo-wall-health-diagnostic"


class ProbeError(ValueError):
    """A bounded diagnostic failure safe to expose to the CI harness."""


def _boot_id(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError as error:
            raise ProbeError("invalid_linux_boot_id") from error
    value = raw.strip()
    if BOOT_ID_RE.fullmatch(value) is None:
        raise ProbeError("invalid_linux_boot_id")
    # UUID also rejects any accidental non-canonical value accepted above.
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ProbeError("invalid_linux_boot_id") from error
    if str(parsed) != value:
        raise ProbeError("invalid_linux_boot_id")
    return value


def read_boot_id(path: Path = BOOT_ID_PATH) -> str:
    try:
        return _boot_id(updates._regular(path, 64))
    except (updates.UpdateError, ProbeError) as error:
        raise ProbeError("invalid_linux_boot_id") from error


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_report_field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite_report_number")


def read_report(path: Path = HEALTH_PATH) -> dict[str, Any]:
    """Read and parse the fixed-size health report without following links."""
    raw = updates._regular(path, REPORT_BYTES)
    value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("report_not_object")
    return value


def _missing(error: BaseException) -> bool:
    cause = error.__cause__
    return isinstance(error, FileNotFoundError) or (
        isinstance(cause, OSError) and cause.errno == errno.ENOENT
    )


def _report_projection(value: Mapping[str, Any] | None, boot_id: str, now: float) -> dict[str, Any]:
    base = dict(report_status="missing", healthy=None, persistence="invalid",
                current_boot=False, identity_valid=False, sample_age="missing")
    if value is None:
        return base
    required = {"boot_id", "sampled_monotonic", "player_id", "authority_epoch", "persistence", "healthy"}
    if not isinstance(value, Mapping) or set(value) != required:
        base["report_status"] = "invalid"
        base["sample_age"] = "invalid"
        return base
    report_boot = value["boot_id"]
    sampled = value["sampled_monotonic"]
    identity = value["player_id"]
    epoch = value["authority_epoch"]
    valid_boot = isinstance(report_boot, str) and BOOT_ID_RE.fullmatch(report_boot) is not None
    try:
        valid_sample = (type(sampled) in (int, float) and math.isfinite(sampled)
                        and sampled >= 0)
    except (TypeError, ValueError, OverflowError):
        valid_sample = False
    valid_identity = isinstance(identity, str) and PLAYER_ID_RE.fullmatch(identity) is not None
    valid_epoch = type(epoch) is int and epoch > 0
    valid_persistence = (type(value["persistence"]) is str
                         and value["persistence"] in {"durable", "volatile"})
    valid_healthy = type(value["healthy"]) is bool
    if not all((valid_boot, valid_sample, valid_identity, valid_epoch,
                valid_persistence, valid_healthy)):
        base["report_status"] = "invalid"
        base["sample_age"] = "invalid"
        return base
    age = now - sampled
    base.update(report_status="present", healthy=value["healthy"],
                persistence=value["persistence"], current_boot=report_boot == boot_id,
                identity_valid=True,
                sample_age="future" if age < 0 else "fresh" if age <= MAX_AGE else "stale")
    return base


def _service_value(value: Any, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "other"


def _service_status(result: Any) -> dict[str, Any]:
    stdout = getattr(result, "stdout", None)
    if (getattr(result, "returncode", 1) != 0 or not isinstance(stdout, str)
            or len(stdout.encode("utf-8", errors="replace")) > REPORT_BYTES):
        return dict(active_state="other", sub_state="other", result="other", exec_main_status=None)
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"ActiveState", "SubState", "Result", "ExecMainStatus"}:
            values[key] = value
    status: dict[str, Any] = dict(
        active_state=_service_value(values.get("ActiveState"), ACTIVE_STATES),
        sub_state=_service_value(values.get("SubState"), SUB_STATES),
        result=_service_value(values.get("Result"), RESULTS),
        exec_main_status=None,
    )
    main_status = values.get("ExecMainStatus")
    if main_status is not None and re.fullmatch(r"-?[0-9]{1,10}", main_status) is not None:
        number = int(main_status)
        if -(2**31) <= number <= 2**31 - 1:
            status["exec_main_status"] = number
    return status


def _default_systemctl(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, timeout=timeout, check=False)


def service_statuses(systemctl_runner: Callable[..., Any] | None = None) -> dict[str, dict[str, Any]]:
    runner = systemctl_runner or _default_systemctl
    result: dict[str, dict[str, Any]] = {}
    for service in SERVICES:
        argv = ["systemctl", "show", service, "--no-pager", "--plain",
                "--property=ActiveState,SubState,Result,ExecMainStatus"]
        try:
            completed = runner(argv, timeout=5)
            result[service] = _service_status(completed)
        except (OSError, subprocess.SubprocessError, TimeoutError, TypeError):
            result[service] = dict(active_state="other", sub_state="other",
                                   result="other", exec_main_status=None)
    return result


def wayland_socket(path: Path = WAYLAND_PATH,
                   lstat: Callable[[Path], os.stat_result] = os.lstat) -> str:
    try:
        info = lstat(path)
    except OSError as error:
        return "missing" if error.errno == errno.ENOENT else "invalid"
    return "present" if stat.S_ISSOCK(info.st_mode) and info.st_nlink == 1 else "invalid"


def validate_event(value: Any, boot_id: str) -> dict[str, Any] | None:
    """Validate and project one public event for the host-side parser."""
    try:
        expected_boot = _boot_id(boot_id)
        if not isinstance(value, dict) or set(value) != EVENT_FIELDS:
            return None
        if (value["event"] != PUBLIC_EVENT or value["boot_id"] != expected_boot
                or type(value["sample_index"]) is not int
                or not 1 <= value["sample_index"] <= SAMPLE_LIMIT
                or value["report_status"] not in {"missing", "invalid", "present"}
                or (value["healthy"] is not None and type(value["healthy"]) is not bool)
                or value["persistence"] not in {"durable", "volatile", "invalid"}
                or type(value["current_boot"]) is not bool
                or type(value["identity_valid"]) is not bool
                or value["sample_age"] not in {"missing", "invalid", "future", "fresh", "stale"}
                or value["wayland_socket"] not in {"present", "missing", "invalid"}
                or not isinstance(value["services"], dict)
                or set(value["services"]) != set(SERVICES)):
            return None
        if value["report_status"] == "missing":
            if (value["healthy"] is not None or value["persistence"] != "invalid"
                    or value["current_boot"] or value["identity_valid"]
                    or value["sample_age"] != "missing"):
                return None
        elif value["report_status"] == "invalid":
            if (value["healthy"] is not None or value["persistence"] != "invalid"
                    or value["current_boot"] or value["identity_valid"]
                    or value["sample_age"] != "invalid"):
                return None
        elif (type(value["healthy"]) is not bool
              or value["persistence"] not in {"durable", "volatile"}
              or value["identity_valid"] is not True
              or value["sample_age"] not in {"future", "fresh", "stale"}):
            return None
        services: dict[str, dict[str, Any]] = {}
        for service in SERVICES:
            status = value["services"][service]
            if (not isinstance(status, dict) or set(status) != SERVICE_FIELDS
                    or status["active_state"] not in ACTIVE_STATES | {"other"}
                    or status["sub_state"] not in SUB_STATES | {"other"}
                    or status["result"] not in RESULTS | {"other"}
                    or (status["exec_main_status"] is not None
                        and (type(status["exec_main_status"]) is not int
                             or not -(2**31) <= status["exec_main_status"] <= 2**31 - 1))):
                return None
            services[service] = dict(status)
        # Return a fresh projection so callers cannot mutate a trusted input.
        return dict(value, services=services)
    except (ProbeError, TypeError, ValueError, AttributeError):
        return None


def sample_once(sample_index: int, boot_id: str, now: float, *,
                report_reader: Callable[[], Mapping[str, Any]] = read_report,
                systemctl_runner: Callable[..., Any] | None = None,
                socket_state: Callable[[], str] = wayland_socket) -> dict[str, Any]:
    if type(sample_index) is not int or not 1 <= sample_index <= SAMPLE_LIMIT:
        raise ProbeError("sample_index_limit")
    boot = _boot_id(boot_id)
    try:
        report = report_reader()
    except (FileNotFoundError, updates.UpdateError) as error:
        report = None
        missing = _missing(error)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        report = None
        missing = False
    else:
        missing = False
    projection = _report_projection(report, boot, now) if report is not None else {
        "report_status": "missing" if missing else "invalid", "healthy": None,
        "persistence": "invalid", "current_boot": False, "identity_valid": False,
        "sample_age": "missing" if missing else "invalid",
    }
    try:
        socket = socket_state()
    except (OSError, TypeError, ValueError):
        socket = "invalid"
    if socket not in {"present", "missing", "invalid"}:
        socket = "invalid"
    return validate_event(dict(event=PUBLIC_EVENT, boot_id=boot, sample_index=sample_index,
                               **projection, services=service_statuses(systemctl_runner),
                               wayland_socket=socket), boot) or (_ for _ in ()).throw(
                                   ProbeError("invalid_event"))


def observe(*, report_reader: Callable[[], Mapping[str, Any]] = read_report,
            boot_id_reader: Callable[[], str] = read_boot_id,
            systemctl_runner: Callable[..., Any] | None = None,
            socket_state: Callable[[], str] = wayland_socket,
            clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep,
            sample_interval: float = SAMPLE_INTERVAL,
            max_samples: int = SAMPLE_LIMIT,
            max_runtime: float = MAX_RUNTIME) -> Iterator[dict[str, Any]]:
    """Yield at most 60 samples during one bounded monotonic-time window."""
    if (type(max_samples) is not int or not 1 <= max_samples <= SAMPLE_LIMIT
            or not isinstance(sample_interval, (int, float)) or sample_interval < 0
            or not math.isfinite(sample_interval)
            or not isinstance(max_runtime, (int, float)) or max_runtime < 0
            or not math.isfinite(max_runtime) or max_runtime > MAX_RUNTIME):
        raise ProbeError("sampling_limits")
    started = clock()
    deadline = started + max_runtime
    for index in range(1, max_samples + 1):
        now = clock()
        if index > 1 and now > deadline:
            break
        yield sample_once(index, boot_id_reader(), now, report_reader=report_reader,
                          systemctl_runner=systemctl_runner, socket_state=socket_state)
        if index == max_samples:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(float(sample_interval), remaining))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        for event in observe():
            print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)
    except ProbeError as error:
        print(json.dumps({"event": PUBLIC_EVENT, "error": str(error)},
                         sort_keys=True, separators=(",", ":")))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
