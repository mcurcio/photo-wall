"""Base-owned preparation worker: stages a stage command's roots as images, no process-control port.

The broker writes the request: the command and the measured ABI it holds (the cold
configuration's), never the reference's own fields (errata E-E2C-DR-1). Each root is staged by
`stage_image` from AppManager's digest-checked download; the bytes were admitted at download, and
adoption is a rename, so nothing is admitted here."""
from __future__ import annotations

import json
import os
from pathlib import Path

from appliance.apps.environment import stage_image
from appliance.kernel.capacity import ROOT_IMAGES, STORE
from appliance.kernel.clock import boot_id
from appliance.kernel.image_mount import ImageMounter, SystemdImageMounter
from contracts.node_lifecycle import parse_stage_command
from contracts.node_protocol import token
from contracts.strict_json import loads_object
from uplink.files import write_atomically

REQUEST = Path("/run/photo-wall-app-broker/import-request.json")
RESULTS = Path("/run/photo-wall-root-import")
# The measured ABI's keys in the request, beside "command" (the broker writes them).
ABI_KEYS = ("base_abi", "graphics_abi", "plugin_abi")


def parse_request(value: object):
    """The request's command and measured ABI: exactly the four keys, each ABI a contract token."""
    if not isinstance(value, dict) or set(value) != {"command", *ABI_KEYS}:
        raise ValueError("root_import_request_invalid")
    measured = {key: value[key] for key in ABI_KEYS}
    for item in measured.values():
        token(item)
    return parse_stage_command(value["command"].encode()), measured


def stage_roots(command, measured: dict[str, str], *, mounter: ImageMounter, store: Path = STORE,
                images: Path = ROOT_IMAGES) -> None:
    """Stage the command's target and fallback from AppManager's downloads, against the measured
    ABI (never the reference's own fields)."""
    for reference in (command.target, command.fallback):
        if reference is not None:
            stage_image(store / "preparation/downloads" / reference.environment_sha256, store / "app-roots",
                        reference, images=images, mounter=mounter, **measured)


def main() -> None:
    descriptor = os.open(REQUEST, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("root_import_request_ownership")
        value = loads_object(source.read(32769), max_bytes=32768)
    command, measured = parse_request(value)
    if command.producer.kernel_boot_id != boot_id():
        raise ValueError("root_import_wrong_boot")
    stage_roots(command, measured, mounter=SystemdImageMounter())
    RESULTS.mkdir(mode=0o700, exist_ok=True)
    if RESULTS.is_symlink() or RESULTS.stat().st_uid != 0 or RESULTS.stat().st_mode & 0o077:
        raise ValueError("root_import_result_ownership")
    write_atomically(RESULTS / (str(command.operation_id) + ".json"), json.dumps({
        "command_sha256": command.command_sha256, "operation_id": str(command.operation_id),
        "boot_id": str(command.producer.kernel_boot_id), "roots_verified": True}, sort_keys=True).encode(), mode=0o600)


if __name__ == "__main__":
    main()
