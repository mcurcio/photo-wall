"""Independent standard-library Linux host observation and reboot adapters."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from appliance.node.base_status import STATUS, read_supervisor_status
from appliance.node.clock import boot_id, boottime_ms  # noqa: F401


class LinuxHostSampler:
    def __init__(self, proc: Path = Path("/proc"), filesystem: Path = Path("/run")):
        self.proc, self.filesystem = proc, filesystem

    def sample(self) -> tuple:
        memory = {}
        for line in (self.proc / "meminfo").read_text().splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2] == "kB":
                memory[fields[0].rstrip(":")] = int(fields[1]) * 1024
        load = float((self.proc / "loadavg").read_text().split()[0])
        disk = os.statvfs(self.filesystem)
        metrics = (("uptime", boottime_ms() / 1000, "seconds"),
                ("load_1m", load, "tasks"),
                ("memory_total", memory["MemTotal"], "bytes"),
                ("memory_available", memory["MemAvailable"], "bytes"),
                ("runtime_available", disk.f_bavail * disk.f_frsize, "bytes"))
        return metrics

    def supervision(self) -> tuple:
        rows = []
        units = {"manager_supervisor": "photo-wall-manager-supervisor.service",
                 "manager": "photo-wall-node-manager.service", "broker": "photo-wall-app-broker.service",
                 "display": "photo-wall-display.service", "display_controller": "photo-wall-display-controller.service"}
        for name, unit in units.items():
            state = None
            try:
                result = subprocess.run(["/usr/bin/systemctl", "show", unit,
                    "--property=LoadState,ActiveState"], capture_output=True, text=True,
                    check=True, timeout=0.25, env={"PATH": "/usr/bin", "LANG": "C"})
                if len(result.stdout) > 1024:
                    raise ValueError("unit_observation_bound")
                value = dict(line.split("=", 1) for line in result.stdout.splitlines())
                if (set(value) != {"LoadState", "ActiveState"}
                        or value["ActiveState"] not in {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "refreshing"}):
                    raise ValueError("unit_observation_invalid")
                state = value["ActiveState"] if value["LoadState"] == "loaded" else "absent"
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            rows.extend(((name + "_known", int(state is not None), "boolean", "pid1"),
                         (name + "_active", int(state == "active"), "boolean", "pid1"),
                         (name + "_failed", int(state == "failed"), "boolean", "pid1")))
        try:
            value = read_supervisor_status(STATUS, kernel_boot_id=boot_id(), now_ms=boottime_ms())
        except (OSError, ValueError):
            rows.append(("manager_summary_known", 0, "boolean", "base_supervisor"))
        else:
            rows.extend((("manager_summary_known", 1, "boolean", "base_supervisor"),
                         ("manager_running", int(value["running"]), "boolean", "base_supervisor"),
                         ("manager_attempts", value["attempts"], "count", "base_supervisor"),
                         ("manager_recovery_required", int(value["fault"] == "manager_recovery_required"), "boolean", "base_supervisor"),
                         ("manager_start_unknown", int(value["fault"] == "manager_start_unknown"), "boolean", "base_supervisor"),
                         ("manager_summary_age", boottime_ms() - value["sampled_boottime_ms"], "milliseconds", "base_supervisor")))
        return tuple(rows)



class SystemdRebootDriver:
    """Request PID1 reboot, then observe stopping state; never completed-boot evidence."""

    def initiate(self) -> bool:
        result = subprocess.run(["/usr/bin/systemctl", "--no-block", "reboot"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False,
                                env={"PATH": "/usr/sbin:/usr/bin", "LANG": "C"})
        if result.returncode != 0:
            return False
        # Job submission alone is admission. Only the separate PID1 state read
        # supports an initiation fact; service death/timeout remains unknown.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = subprocess.run(["/usr/bin/systemctl", "show", "--property=SystemState", "--value"],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=1, check=False,
                                   env={"PATH": "/usr/bin", "LANG": "C"})
            if state.returncode == 0 and state.stdout.strip() == "stopping":
                return True
            time.sleep(0.05)
        return False
