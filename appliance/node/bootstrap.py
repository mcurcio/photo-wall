"""Base V2 cold handoff and exact-root preparation; no host package installation."""
from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from appliance.node.capacity import STORE, admit_cold, memory_values
from appliance.node.clock import boot_id
from appliance.node.environment import verify_root
from appliance.node.preparer import DownloadPreparer
from appliance.node.storage_mount import mount_storage
from appliance.node_boot_handoff import HANDOFF, read_node_handoff
from contracts.strict_json import loads_object
from uplink.files import write_atomically


def _marker(path: Path, keys: set[str]) -> dict:
    info = path.lstat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 1024:
        raise ValueError("node_base_marker_ownership")
    value = loads_object(path.read_bytes(), max_bytes=1024)
    if value is None or set(value) != keys:
        raise ValueError("node_base_marker_invalid")
    return value


def materialize_handoff(*, root: Path = Path("/")) -> tuple:
    central, offer = read_node_handoff(root / HANDOFF)
    if offer.kernel_boot_id != boot_id():
        raise ValueError("node_boot_handoff_stale")
    marker = _marker(root / "usr/lib/photo-wall-node-base/abi.json", {"base_abi"})
    directory = root / "run/photo-wall-node"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if marker["base_abi"] != offer.base.base_abi:
        raise ValueError("node_measured_base_abi_mismatch")
    # Host-only configuration is available even when graphics packaging is broken.
    write_atomically(directory / "host.json", json.dumps({"central": central,
        "serial": offer.serial, "offer_id": str(offer.offer_id)}).encode(), mode=0o600)
    graphics = _marker(root / "usr/lib/photo-wall-display/abi.json", {"graphics_abi", "plugin_abi"})
    abi = {**marker, **graphics}
    if any(getattr(offer.base, name) != value for name, value in abi.items()):
        raise ValueError("node_measured_graphics_abi_mismatch")
    configs = {
        "host": {"central": central, "serial": offer.serial, "offer_id": str(offer.offer_id)},
        "broker": {"central": central, "serial": offer.serial},
        "display": {"central": central, "serial": offer.serial, "offer_id": str(offer.offer_id)},
        "manager-client": {"central": central, "serial": offer.serial, "offer_id": str(offer.offer_id), **abi},
        "manager": {"primary": asdict(offer.manager_primary),
                    "fallback": asdict(offer.manager_fallback) if offer.manager_fallback else None, **abi},
    }
    if offer.app_environment is not None:
        configs["cold"] = {"boot_id": str(offer.kernel_boot_id), "offer_id": str(offer.offer_id),
                            "operation_id": str(offer.cold_operation_id), "environment": asdict(offer.app_environment), **abi}
    for name, value in configs.items():
        write_atomically(directory / (name + ".json"), json.dumps(value, sort_keys=True).encode(), mode=0o600)
    # A public app endpoint and bounded volatile cache are configuration, not credentials.
    write_atomically(root / "etc/photo-wall/public.json", json.dumps({"schema": 1,
        "central_origin": central, "cache_dir": "/tmp/media", "cache_bytes": 64 * 1024**2}).encode(), mode=0o644)
    return central, offer, abi


def prepare_roots(*, root: Path = Path("/")) -> None:
    central, offer, abi = materialize_handoff(root=root)
    node_store = root / str(STORE).lstrip("/")
    selected = (("manager-primary", offer.manager_primary, "manager-roots"),
                ("manager-fallback", offer.manager_fallback, "manager-roots"),
                ("app", offer.app_environment, "app-roots"))
    resident = set()
    for _, environment, destination in selected:
        if environment is not None and (node_store / destination / environment.environment_sha256).exists():
            verify_root(node_store / destination / environment.environment_sha256, environment, **abi)
            resident.add(environment.environment_sha256)
    total, available = memory_values()
    admit_cold((entry[1] for entry in selected), total=total, available=available,
               free=shutil.disk_usage(node_store).free, resident=frozenset(resident))
    for kind, environment, destination in selected:
        if environment is None or environment.environment_sha256 in resident:
            continue
        # Each root pool is a separate systemd writable bind mount. Keep staging
        # under its destination so immutable publication is one atomic rename.
        roots = node_store / destination
        roots.mkdir(mode=0o755, exist_ok=True)
        staging = roots / ".cold-staging"
        staging.mkdir(mode=0o700, exist_ok=True)
        status = staging.lstat()
        if staging.is_symlink() or status.st_uid != os.geteuid() or status.st_mode & 0o077:
            raise ValueError("node_cold_staging_ownership")
        directory = staging / kind
        url = central.rstrip("/") + f"/v2/node/boot-offers/{offer.offer_id}/artifacts/{kind}"
        preparer = DownloadPreparer(directory, url=url, **abi)
        preparer.prepare(environment)
        target = roots / environment.environment_sha256
        verified = directory / "verified" / environment.environment_sha256
        if target.exists():
            verify_root(target, environment, **abi)
        else:
            os.rename(verified, target)
        (directory / (environment.environment_sha256 + ".tar")).unlink(missing_ok=True)
        if kind == "app":
            bridge = roots / environment.environment_sha256 / "rootfs/usr/lib/photo-wall-client/libphoto-wall-frame-client.so"
            if not bridge.is_file():
                raise ValueError("node_player_frame_bridge_missing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("handoff", "prepare", "storage"))
    args = parser.parse_args()
    if args.mode == "storage":
        mount_storage()
    elif args.mode == "handoff":
        materialize_handoff()
    else:
        prepare_roots()


if __name__ == "__main__":
    main()
