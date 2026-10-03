"""Base-owned preparation worker: exact stream import, no process-control port."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from appliance.node.capacity import (
    EMERGENCY_HEADROOM,
    OVERHEAD,
    STORE,
    device_class,
    memory_values,
)
from appliance.node.clock import boot_id
from appliance.node.environment import stage_archive, verify_root
from contracts.node_lifecycle import parse_stage_command
from contracts.strict_json import loads_object
from uplink.files import write_atomically

REQUEST = Path("/run/photo-wall-app-broker/import-request.json")
RESULTS = Path("/run/photo-wall-root-import")


def main() -> None:
    descriptor = os.open(REQUEST, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("root_import_request_ownership")
        value = loads_object(source.read(32769), max_bytes=32768)
    if value is None or set(value) != {"command"}:
        raise ValueError("root_import_request_invalid")
    command = parse_stage_command(value["command"].encode())
    if command.producer.kernel_boot_id != boot_id():
        raise ValueError("root_import_wrong_boot")
    roots = STORE / "app-roots"
    for reference in (command.target, command.fallback):
        if reference is None:
            continue
        abi = {key: getattr(reference, key) for key in ("base_abi", "graphics_abi", "plugin_abi")}
        target = roots / reference.environment_sha256
        if target.exists():
            verify_root(target, reference, **abi)
            continue
        total, available = memory_values()
        disk = shutil.disk_usage(STORE)
        incremental = reference.size_bytes + OVERHEAD
        if disk.used + incremental > device_class(total).store_bytes or incremental > min(disk.free, available - EMERGENCY_HEADROOM):
            raise ValueError("root_import_capacity")
        archive = STORE / "preparation/downloads" / (reference.environment_sha256 + ".tar")
        stage_archive(archive, roots, reference, **abi)
    RESULTS.mkdir(mode=0o700, exist_ok=True)
    if RESULTS.is_symlink() or RESULTS.stat().st_uid != 0 or RESULTS.stat().st_mode & 0o077:
        raise ValueError("root_import_result_ownership")
    write_atomically(RESULTS / (str(command.operation_id) + ".json"), json.dumps({
        "command_sha256": command.command_sha256, "operation_id": str(command.operation_id),
        "boot_id": str(command.producer.kernel_boot_id), "roots_verified": True}, sort_keys=True).encode(), mode=0o600)


if __name__ == "__main__":
    main()
