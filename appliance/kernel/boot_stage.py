"""Base boot stage records (docs/node-4gb-memory-design.md §4.2 rule 2, §4.3 T3): stdlib,
contracts, capacity.

Each base stage (`handoff`, `storage`, `prepare`) owns one file in DIRECTORY: `running` at
entry, then `done`, `refused` (a `StorageShort`, with its numbers) or `failed` (with a fault
token). Writes are best-effort: a record that cannot be written never changes the stage's
outcome, and PID1's failed-unit list still names the unit. HostCore is the only reader and
folds the records into its facts record (`read_boot_report`). DIRECTORY is 0755 (tmpfiles.d
in node-base), outside /run/photo-wall-node (0700), so the display controller can read it.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import stat
from collections.abc import Callable
from pathlib import Path

from appliance.kernel.capacity import StorageShort
from contracts.node_host_facts import BOOT_STAGES, BootReportV2, BootStageV2
from contracts.node_protocol import token
from contracts.strict_json import loads_object
from uplink.files import write_atomically

DIRECTORY = Path("/run/photo-wall-boot-stage")
MAX_STAGE_BYTES = 512
_FIELDS = frozenset({"stage", "state", "fault", "required_bytes", "room_bytes"})
LOG = logging.getLogger(__name__)


def _valid(value: str, maximum: int = 64) -> bool:
    try:
        token(value, maximum)
    except ValueError:
        return False
    return True


def fault_token(error: BaseException) -> str:
    """The stage fault for `error`, always a valid 64-character token; never raises."""
    if isinstance(error, StorageShort) and _valid(error.fault):
        return error.fault
    name = type(error).__name__
    if isinstance(error, OSError):
        # An OSError without an errno (http.client.RemoteDisconnected is a ConnectionResetError
        # raised with no errno) is named by its type.
        code = errno.errorcode.get(error.errno) if isinstance(error.errno, int) else None
        candidate = "os:" + (code if code is not None else name[:60])
    elif isinstance(error, ValueError) and len(error.args) == 1 and _valid(str(error.args[0])):
        candidate = str(error.args[0])
    else:
        candidate = "unexpected:" + name[:52]
    return candidate if _valid(candidate) else "unexpected:error"


def write_stage(stage: BootStageV2, *, directory: Path = DIRECTORY) -> None:
    """Atomically replace `<stage>.json` (0644 whatever the unit's umask)."""
    document = {"stage": stage.stage, "state": stage.state, "fault": stage.fault,
                "required_bytes": stage.required_bytes, "room_bytes": stage.room_bytes}
    write_atomically(directory / (stage.stage + ".json"),
                     json.dumps(document, sort_keys=True).encode(), mode=0o644)


def run_stage(stage: str, action: Callable[[], None], *, directory: Path = DIRECTORY) -> None:
    """Run `action` as base stage `stage`, recording its state; re-raises `action`'s error only."""
    logged = False

    def record(state: str, fault: str | None = None, required: int | None = None,
               room: int | None = None) -> None:
        nonlocal logged
        try:
            write_stage(BootStageV2(stage, state, fault, required, room), directory=directory)
        except (OSError, ValueError) as error:
            if not logged:
                logged = True
                LOG.warning("boot stage record not written: %s %s", stage, fault_token(error))

    record("running")
    try:
        action()
    except StorageShort as error:
        fault = fault_token(error)
        numbers = (error.required, error.room)
        if all(type(value) is int for value in numbers) and min(numbers) >= 0:
            record("refused", fault, error.required, error.room)
        else:
            record("failed", fault)
        raise
    except BaseException as error:
        record("failed", fault_token(error))
        raise
    record("done")


def _read_bounded(path: Path) -> bytes | None:
    """At most MAX_STAGE_BYTES + 1 bytes of a regular file, never following a symlink nor
    blocking on a FIFO; None for anything else or any error."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        chunks, remaining = [], MAX_STAGE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _read_stage(path: Path, stage: str) -> BootStageV2 | None:
    raw = _read_bounded(path)
    if raw is None:
        return None
    try:
        value = loads_object(raw, max_bytes=MAX_STAGE_BYTES)
        if value is None or set(value) != _FIELDS or value["stage"] != stage:
            return None
        return BootStageV2(**value)
    except (ValueError, TypeError):
        return None


def read_boot_report(*, directory: Path = DIRECTORY,
                     units: Callable[[], tuple[tuple[str, ...], int] | None]) -> BootReportV2 | None:
    """The stage records present (a missing or invalid file is omitted) and `units()`'s failed
    photo-wall units with their overflow count, or None for both when `units()` returns None or
    fails (not read, never "nothing failed"). None only when no stage record is readable and
    the units are not read; never raises."""
    stages = tuple(record for record in (_read_stage(directory / (stage + ".json"), stage)
                                         for stage in BOOT_STAGES) if record is not None)
    try:
        read = units()
        failed = None if read is None else BootReportV2((), tuple(read[0]), read[1])
    except Exception:  # a broken reader is "not read", never a broken record
        failed = None
    if failed is None:
        return BootReportV2(stages, None, None) if stages else None
    return BootReportV2(stages, failed.failed_units, failed.failed_units_more)
