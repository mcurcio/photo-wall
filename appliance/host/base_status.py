"""Neutral base-supervisor observation schema; no app or effect imports."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import UUID

from contracts.strict_json import loads_object

STATUS = Path("/run/photo-wall-manager-supervisor/supervisor-status.json")
MAX_AGE_MS = 10000
FAULTS = (None, "manager_start_unknown", "manager_recovery_required")


def supervisor_document(
    *,
    kernel_boot_id: UUID,
    sampled_boottime_ms: int,
    attempts: int,
    running: bool,
    fault: str | None,
) -> dict:
    if (
        type(sampled_boottime_ms) is not int
        or sampled_boottime_ms < 0
        or type(attempts) is not int
        or not 0 <= attempts <= 16
        or type(running) is not bool
        or fault not in FAULTS
        or (running and fault is not None)
    ):
        raise ValueError("supervisor_status_invalid")
    return {
        "schema": 2,
        "boot_id": str(kernel_boot_id),
        "sampled_boottime_ms": sampled_boottime_ms,
        "attempts": attempts,
        "running": running,
        "fault": fault,
    }


def read_supervisor_status(
    path: Path, *, kernel_boot_id: UUID, now_ms: int, owner_uid: int = 0
) -> dict:
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != owner_uid
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise ValueError("supervisor_status_directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ValueError("supervisor_status_ownership")
        value = loads_object(source.read(1025), max_bytes=1024)
    if (
        value is None
        or set(value)
        != {"schema", "boot_id", "sampled_boottime_ms", "attempts", "running", "fault"}
        or value["schema"] != 2
        or value["boot_id"] != str(kernel_boot_id)
    ):
        raise ValueError("supervisor_status_binding")
    expected = supervisor_document(
        kernel_boot_id=kernel_boot_id,
        sampled_boottime_ms=value["sampled_boottime_ms"],
        attempts=value["attempts"],
        running=value["running"],
        fault=value["fault"],
    )
    if value != expected or not 0 <= now_ms - value["sampled_boottime_ms"] <= MAX_AGE_MS:
        raise ValueError("supervisor_status_stale")
    return value
