"""Base supervisor for exact versioned manager roots; never stops the Player."""
from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from appliance.apps.environment import verify_root
from appliance.apps.lifecycle_storage import FileManagerRecoveryStore
from appliance.apps.process_linux import process_root_matches, systemctl_show
from appliance.host.base_status import supervisor_document
from appliance.kernel.boot_store import BootStore
from appliance.kernel.clock import boot_id, boottime_ms
from appliance.node.manager import ManagerRecovery
from appliance.process_identity import read_proc_start_ticks
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import MANAGER_PACKAGE
from contracts.strict_json import loads_object

UNIT = "photo-wall-node-manager.service"


class SystemdManagerLauncher:
    def __init__(self, roots: Path, references: dict[str, AppEnvironmentRefV2], *,
                 base_abi: str, graphics_abi: str, plugin_abi: str):
        self.roots, self.references = roots, references
        self.abi = dict(base_abi=base_abi, graphics_abi=graphics_abi, plugin_abi=plugin_abi)

    def verify(self, root_digest: str) -> bool:
        reference = self.references.get(root_digest)
        if reference is None or reference.environment_sha256 != root_digest or reference.deb_name != MANAGER_PACKAGE:
            return False
        try:
            verify_root(self.roots / root_digest, reference, **self.abi)
        except (OSError, ValueError):
            return False
        return True

    def observed(self, root_digest: str) -> bool:
        rows = systemctl_show(UNIT)
        if rows["LoadState"] == "not-found" or (rows["MainPID"] == "0" and rows["ActiveState"] in ("inactive", "failed")):
            return False
        root = self.roots / root_digest / "rootfs"
        pid = int(rows["MainPID"])
        ticks = read_proc_start_ticks(Path("/proc"), pid)
        if rows["ActiveState"] != "active" or ticks is None or rows["RootDirectory"] != str(root) or not rows["ControlGroup"].startswith("/photowallpreparation.slice/"):
            raise ValueError("manager_process_unknown")
        if not process_root_matches(Path("/proc"), pid, root) or f"0::{rows['ControlGroup']}" not in Path(f"/proc/{pid}/cgroup").read_text().splitlines() or read_proc_start_ticks(Path("/proc"), pid) != ticks:
            raise ValueError("manager_process_unknown")
        if systemctl_show(UNIT) != rows or read_proc_start_ticks(Path("/proc"), pid) != ticks:
            raise ValueError("manager_process_changed_during_observation")
        return True

    def start(self, root_digest: str) -> bool:
        if not self.verify(root_digest):
            return False
        if self.observed(root_digest):
            return True
        properties = (
            f"RootDirectory={self.roots / root_digest / 'rootfs'}", "User=10003", "Group=10003",
            "Slice=photowallpreparation.slice", "ProtectSystem=strict", "ProtectHome=yes",
            "PrivateDevices=yes", "PrivateTmp=yes", "NoNewPrivileges=yes", "CapabilityBoundingSet=",
            "ProtectControlGroups=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
            "RestrictSUIDSGID=yes", "RestrictNamespaces=yes", "MemorySwapMax=0", "OOMScoreAdjust=300",
            "TasksMax=32", "CPUQuota=25%", "IOWeight=10", "Restart=no", "KillMode=control-group",
            "TemporaryFileSystem=/run:rw,nosuid,nodev,size=32M /tmp:rw,nosuid,nodev,size=64M",
            "BindPaths=/run/photo-wall-node-storage/preparation:/run/photo-wall-preparation",
            "LoadCredential=node-config:/run/photo-wall-node/manager-client.json",
            "BindReadOnlyPaths=/etc/resolv.conf:/etc/resolv.conf",
            "BindReadOnlyPaths=/run/photo-wall-node-storage/root-images:/run/photo-wall-root-images",
            "UnsetEnvironment=PYTHONPATH PYTHONHOME LD_LIBRARY_PATH LD_PRELOAD GI_TYPELIB_PATH GST_PLUGIN_PATH",
        )
        args = ["/usr/bin/systemd-run", "--quiet", "--collect", "--unit=" + UNIT, "--service-type=exec"]
        for prop in properties:
            args.extend(("--property", prop))
        args.extend(("--", self.references[root_digest].entry_point))
        subprocess.run(args, check=True, timeout=15, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={"PATH": "/usr/bin", "LANG": "C"})
        # Observe surviving exact process after a bounded import/start window. This is
        # process startup only; no manager workflow or app-control readiness is inferred.
        time.sleep(0.5)
        return self.observed(root_digest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/run/photo-wall-node/manager.json"))
    args = parser.parse_args()
    info = args.config.lstat()
    if args.config.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("manager_configuration_ownership")
    config = loads_object(args.config.read_bytes(), max_bytes=16384)
    if config is None or set(config) != {"primary", "fallback", "base_abi", "graphics_abi", "plugin_abi"}:
        raise ValueError("manager_configuration_invalid")
    primary = AppEnvironmentRefV2(**config["primary"])
    fallback = AppEnvironmentRefV2(**config["fallback"]) if config["fallback"] else None
    fallback_digest = fallback.environment_sha256 if fallback else None
    store = BootStore(Path("/run/photo-wall-manager-supervisor"), boot_id=boot_id(),
                      policy={"primary": primary.environment_sha256, "fallback": fallback_digest})
    launcher = SystemdManagerLauncher(Path("/run/photo-wall-node-storage/manager-roots"),
                                      {ref.environment_sha256: ref for ref in (primary, fallback) if ref is not None},
                                      **{name: config[name] for name in ("base_abi", "graphics_abi", "plugin_abi")})
    recovery = ManagerRecovery(launcher, primary=primary.environment_sha256,
                               fallback=fallback_digest,
                               store=FileManagerRecoveryStore(store))
    try:
        while True:
            state = recovery.state
            if state.running_root is not None:
                if not launcher.observed(state.running_root):
                    recovery.observed_exit(state.running_root)
            elif state.fault == "manager_start_unknown":
                expected = primary.environment_sha256 if state.attempts == 1 else fallback_digest
                recovery.observed_start_result(expected, running=launcher.observed(expected))
            current = recovery.recover()
            store.write("supervisor-status", supervisor_document(kernel_boot_id=boot_id(),
                sampled_boottime_ms=boottime_ms(), attempts=current.attempts,
                running=current.running_root is not None, fault=current.fault))
            time.sleep(2)
    finally:
        store.close()


if __name__ == "__main__":
    main()
