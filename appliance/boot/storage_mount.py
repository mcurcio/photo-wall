"""Base-only bounded tmpfs mount adapter; never imported by AppManager."""
import logging
import os
import subprocess
from pathlib import Path

from appliance.kernel.capacity import (
    CONTROLLERS,
    MEMINFO,
    STORE,
    device_class,
    memory_controller_present,
    memory_total,
)

LOG = logging.getLogger(__name__)


def require_mounted_size(store: Path, store_bytes: int) -> None:
    """The mounted store is non-empty and no larger than the device class's store."""
    info = os.statvfs(store)
    if not 0 < info.f_blocks * info.f_frsize <= store_bytes:
        raise ValueError("node_storage_mount_budget")


def mount_storage(*, controllers: Path = CONTROLLERS, meminfo: Path = MEMINFO) -> None:
    # Without the memory controller no slice cap is enforced, but the store still mounts: the
    # controller comes from the separately staged boot tree's cmdline, and Select is fleet-wide,
    # so refusing here would darken a Player whose tree predates cgroup_enable=memory. HostCore
    # reports the absence (`memcg_present` 0) and the console warns (errata E-FX2-1).
    if not memory_controller_present(controllers):
        LOG.warning("memory controller absent: memory limits not enforced")
    store_bytes = device_class(memory_total(meminfo)).store_bytes
    STORE.mkdir(mode=0o755, exist_ok=True)
    if STORE.is_symlink() or STORE.stat().st_uid != 0 or STORE.stat().st_mode & 0o022:
        raise ValueError("node_storage_ownership")
    if not os.path.ismount(STORE):
        if any(STORE.iterdir()):
            raise ValueError("node_storage_unmounted_contents")
        subprocess.run(["/usr/bin/mount", "-t", "tmpfs", "-o", f"size={store_bytes},nr_inodes=600000,mode=0755,nosuid,nodev", "photo-wall-node", str(STORE)], check=True, timeout=10)
    rows = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    match = next((row for row in rows if row[4] == str(STORE)), None)
    if match is None or match[match.index("-") + 1] != "tmpfs":
        raise ValueError("node_storage_mount_type")
    require_mounted_size(STORE, store_bytes)
    for name, mode, owner in (("app-roots", 0o755, 0), ("manager-roots", 0o755, 0), ("root-images", 0o755, 0), ("downloads", 0o700, 0), ("preparation", 0o700, 10003)):
        path = STORE / name
        path.mkdir(mode=mode, exist_ok=True)
        if path.is_symlink():
            raise ValueError("node_storage_child_symlink")
        os.chown(path, owner, owner)
        path.chmod(mode)
