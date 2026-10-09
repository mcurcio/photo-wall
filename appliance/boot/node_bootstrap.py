"""Base V2 cold handoff and exact-root preparation (release roots as images mounted read-only
through PID1, E2c); no host package installation."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from appliance.apps.environment import mounted_root, stage_image
from appliance.boot.bus_environment import write_bus_environment
from appliance.boot.storage_mount import mount_storage
from appliance.kernel.boot_stage import run_stage
from appliance.kernel.capacity import STORE, admit_cold, memory_values
from appliance.kernel.clock import boot_id
from appliance.kernel.image_mount import ImageMounter, SystemdImageMounter
from appliance.node.preparer import DownloadPreparer
from appliance.node_boot_handoff import HANDOFF, read_node_handoff
from contracts.strict_json import loads_object
from uplink.files import write_atomically

# The prepare stage's download window (every attempt and retry of every artifact ends inside
# it): photo-wall-node-prepare.service's TimeoutStartSec=1200 less STAGING_MARGIN_SECONDS for
# hashing, mounting and checking the last image downloaded (a test binds the unit to both).
STAGING_MARGIN_SECONDS = 300
DOWNLOAD_WINDOW_SECONDS = 900
COLD_STAGING = ".cold-staging"
# The directories holding one mount point per staged root; PID1 creates the mount points.
_ROOT_DIRECTORIES = ("manager-roots", "app-roots")
IMAGE_POOL = "root-images"


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
    # Host-only configuration is written as soon as the offer is this boot's, before the base
    # marker and ABI checks, so Host Management runs and reports their failures (boot stage
    # records). The base tag is the node's own record of the base this boot runs: the offer
    # whose base the initramfs verified and mounted (Host Management reports it in its facts).
    directory = root / "run/photo-wall-node"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    host = {"central": central, "serial": offer.serial, "offer_id": str(offer.offer_id),
            "base_tag": offer.base.tag}
    write_atomically(directory / "host.json", json.dumps(host).encode(), mode=0o600)
    # The bus needs only the origin and the serial, so it runs (and Central can reach it) even
    # when the checks below refuse this boot's base.
    write_bus_environment(root, central, offer.serial)
    marker =_marker(root / "usr/lib/photo-wall-node-base/abi.json", {"base_abi"})
    if marker["base_abi"] != offer.base.base_abi:
        raise ValueError("node_measured_base_abi_mismatch")
    graphics = _marker(root / "usr/lib/photo-wall-display/abi.json", {"graphics_abi", "plugin_abi"})
    abi = {**marker, **graphics}
    if any(getattr(offer.base, name) != value for name, value in abi.items()):
        raise ValueError("node_measured_graphics_abi_mismatch")
    configs = {
        "host": host,
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


def _owned_staging(staging: Path) -> None:
    status = staging.lstat()
    if staging.is_symlink() or status.st_uid != os.geteuid() or status.st_mode & 0o077:
        raise ValueError("node_cold_staging_ownership")


def _clear_cold_staging(node_store: Path) -> None:
    """Remove a killed earlier run's staging (its `.partial` and complete downloads) before
    admission counts the store. Only this stage writes COLD_STAGING, and the stages after it
    require it, so nothing else is using it."""
    staging = node_store / IMAGE_POOL / COLD_STAGING
    if staging.is_symlink() or staging.exists():
        _owned_staging(staging)
        shutil.rmtree(staging)


def prepare_roots(*, root: Path = Path("/"), mounter: ImageMounter | None = None) -> None:
    window_ends = time.monotonic() + DOWNLOAD_WINDOW_SECONDS
    mounter = SystemdImageMounter() if mounter is None else mounter
    central, offer, abi = materialize_handoff(root=root)
    node_store = root / str(STORE).lstrip("/")
    images = node_store / IMAGE_POOL
    _clear_cold_staging(node_store)
    selected = (("manager-primary", offer.manager_primary, "manager-roots"),
                ("manager-fallback", offer.manager_fallback, "manager-roots"),
                ("app", offer.app_environment, "app-roots"))
    resident = set()
    for _, environment, destination in selected:
        if environment is None:
            continue
        try:
            mounted_root(node_store / destination, environment, images=images, mounter=mounter, **abi)
        except ValueError:
            continue  # not staged (or not for this Node): fetched and staged below
        resident.add(environment.environment_sha256)
    total, available = memory_values()
    admit_cold((entry[1] for entry in selected), total=total, available=available,
               free=shutil.disk_usage(node_store).free, resident=frozenset(resident))
    for kind, environment, destination in selected:
        if environment is None or environment.environment_sha256 in resident:
            continue
        # The download lands inside the image pool's one writable bind, so adopting it into the
        # pool is one rename (a rename across two binds is EXDEV, errata E-E2C-CUT-5).
        staging = images / COLD_STAGING
        staging.mkdir(mode=0o700, exist_ok=True)
        _owned_staging(staging)
        directory = staging / kind
        url = central.rstrip("/") + f"/v2/node/boot-offers/{offer.offer_id}/artifacts/{kind}"
        # Transient download failures retry in-process inside the window; the stage record
        # stays `running` meanwhile (one oneshot, no systemd Restart=).
        preparer = DownloadPreparer(directory, url=url, retry_until=window_ends, **abi)
        preparer.prepare(environment)
        roots = node_store / destination
        stage_image(directory / environment.environment_sha256, roots, environment,
                    images=images, mounter=mounter, **abi)
        if kind == "app":
            bridge = roots / environment.environment_sha256 / "rootfs/usr/lib/photo-wall-client/libphoto-wall-frame-client.so"
            if not bridge.is_file():
                raise ValueError("node_player_frame_bridge_missing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("handoff", "prepare", "storage"))
    args = parser.parse_args()
    # Each mode is one base boot stage and records its own state (boot_stage.py).
    actions = {"storage": mount_storage, "handoff": materialize_handoff, "prepare": prepare_roots}
    run_stage(args.mode, actions[args.mode])


if __name__ == "__main__":
    main()
