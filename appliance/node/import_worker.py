"""Broker-owned preparation worker lifetime; no manager-selected command or path."""
from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID

from appliance.node.process_linux import systemctl_show
from appliance.process_identity import read_proc_start_ticks
from contracts.node_lifecycle import encode_stage_command
from contracts.strict_json import loads_object


class RootImportWorker:
    def __init__(self, store):
        self.store = store

    def quiescent(self) -> bool:
        prior = self.store.read("import-worker")
        if prior is None:
            return True
        rows = systemctl_show(prior["unit"])
        if rows["ActiveState"] not in ("inactive", "failed") or rows["MainPID"] != "0":
            return False
        job = subprocess.run(["/usr/bin/systemctl", "show", prior["unit"], "--property=Job", "--value"],
                             check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        return job in ("", "0")

    def ready(self, command) -> bool:
        result = Path("/run/photo-wall-root-import") / (str(command.operation_id) + ".json")
        if result.exists():
            info = result.lstat()
            if result.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
                raise ValueError("root_import_result_ownership")
            value = loads_object(result.read_bytes(), max_bytes=1024)
            if value != {"command_sha256": command.command_sha256, "operation_id": str(command.operation_id),
                         "boot_id": str(command.producer.kernel_boot_id), "roots_verified": True}:
                raise ValueError("root_import_result_binding")
            return True
        return False

    def advance(self, command) -> None:
        if self.ready(command):
            return
        prior = self.store.read("import-worker")
        unit = "photo-wall-root-import-" + command.operation_id.hex + ".service"
        if prior is not None:
            rows = systemctl_show(prior["unit"])
            if prior["command_sha256"] == command.command_sha256:
                if rows["ActiveState"] in ("inactive", "failed") and rows["MainPID"] == "0":
                    raise ValueError("root_import_outcome_unknown")
                if prior.get("identity"):
                    identity = prior["identity"]
                    if (rows["InvocationID"] != identity["invocation"] or int(rows["MainPID"]) != identity["pid"]
                            or read_proc_start_ticks(Path("/proc"), identity["pid"]) != identity["ticks"]):
                        raise ValueError("root_import_process_changed")
                return  # Charged before spawn: never repeat an ambiguous worker.
            if rows["ActiveState"] not in ("inactive", "failed") or rows["MainPID"] != "0":
                raise ValueError("root_import_prior_active")
        self.store.write("import-request", {"command": encode_stage_command(command).decode()})
        record = {"command_sha256": command.command_sha256, "unit": unit, "phase": "intent", "identity": None}
        self.store.write("import-worker", record)
        properties = ("Slice=photowallpreparation.slice", "User=root", "MemorySwapMax=0",
            "CPUQuota=25%", "TasksMax=16", "IOWeight=10", "NoNewPrivileges=yes", "PrivateDevices=yes",
            "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=yes", "CapabilityBoundingSet=CAP_DAC_READ_SEARCH",
            "RuntimeDirectory=photo-wall-root-import", "RuntimeDirectoryMode=0700", "RuntimeDirectoryPreserve=yes",
            "ReadWritePaths=/run/photo-wall-node-storage/app-roots /run/photo-wall-root-import",
            "ReadOnlyPaths=/run/photo-wall-node-storage/preparation", "Restart=no", "RuntimeMaxSec=600")
        args = ["/usr/bin/systemd-run", "--quiet", "--collect", "--unit=" + unit, "--service-type=exec"]
        for value in properties:
            args.extend(("--property", value))
        args.extend(("--", "/usr/bin/python3", "-I", "-B", "/usr/lib/photo-wall-root-import"))
        subprocess.run(args, check=True, timeout=10, stdin=subprocess.DEVNULL)
        rows = systemctl_show(unit)
        if rows["ActiveState"] == "active" and rows["MainPID"] != "0":
            pid = int(rows["MainPID"])
            ticks = read_proc_start_ticks(Path("/proc"), pid)
            if ticks is None or not rows["ControlGroup"].startswith("/photowallpreparation.slice/"):
                raise ValueError("root_import_process_unavailable")
            identity = {"pid": pid, "ticks": ticks, "invocation": str(UUID(rows["InvocationID"]))}
            self.store.write("import-worker", {**record, "phase": "observed", "identity": identity})
