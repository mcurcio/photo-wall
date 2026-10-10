"""One privileged container booted with systemd as PID 1, for the real-PID1 node scenarios
(tests/test_node_pid1.py).

The container is --privileged (the units' sandboxing needs mount namespaces docker's default
profile refuses) and gets an equipment identity: a Pi serial as the `Serial` line of a
bind-mounted /proc/cpuinfo. A privileged container sees the runner's own /sys and may load
modules into the runner's kernel, so the two boot units that act on the host's devices are
masked in it (HOST_ACTING_UNITS): systemd-udev-trigger (replays every host device to the runner's
udev) and systemd-modules-load (loads modules into the running kernel). /sys is not made read-only
instead: systemd as PID 1 needs a writable cgroup tree under it.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

# Masked in the container: they act on the runner's devices and kernel (module docstring).
HOST_ACTING_UNITS: Final = ("systemd-udev-trigger.service", "systemd-modules-load.service")
SYSTEMD: Final = "/usr/lib/systemd/systemd"
BOOT_SECONDS: Final = 180.0
EXEC_SECONDS: Final = 120.0
# A booted system: `degraded` too, since some units (systemd-modules-load: no modules for the
# host's kernel) cannot work in a container.
BOOTED: Final = frozenset({"running", "degraded"})

Run = Callable[..., subprocess.CompletedProcess]


def cpuinfo_text(serial: str) -> str:
    """PURE. A /proc/cpuinfo carrying only the `Serial` line the Node's identity reads."""
    return f"processor\t: 0\nSerial\t\t: {serial}\n"


def docker_run_argv(image: str, name: str, cpuinfo: Path, *, target: str) -> list[str]:
    """PURE. Boot `image` with systemd as PID 1 into `target`. Arguments after SYSTEMD are its
    command line in a container: systemd.mask= keeps HOST_ACTING_UNITS from running."""
    return ["docker", "run", "--detach", "--name", name, "--privileged",
            "--env", "container=docker", "--tmpfs", "/run", "--tmpfs", "/run/lock",
            "--volume", f"{cpuinfo}:/proc/cpuinfo:ro", image, SYSTEMD,
            f"systemd.unit={target}", *(f"systemd.mask={unit}" for unit in HOST_ACTING_UNITS)]


class Container:
    """One booted container; every call a `docker` command with text output."""

    def __init__(self, name: str, *, run: Run) -> None:
        self.name, self._run = name, run

    def exec(self, *argv: str, timeout: float = EXEC_SECONDS,
             env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
        options = [item for key, value in (env or {}).items()
                   for item in ("--env", f"{key}={value}")]
        return self._run(["docker", "exec", *options, self.name, *argv], check=False,
                         capture_output=True, text=True, timeout=timeout)

    def copy_in(self, source: Path, target: str) -> None:
        self._run(["docker", "cp", str(source), f"{self.name}:{target}"], check=True,
                  capture_output=True)

    def wait_booted(self, *, seconds: float = BOOT_SECONDS,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> str:
        """systemd's own view once boot is over (`systemctl is-system-running --wait`), polled
        until its bus answers; the last answer when `seconds` pass first."""
        deadline, state = clock() + seconds, ""
        while True:
            try:
                state = self.exec("systemctl", "is-system-running", "--wait",
                                  timeout=max(1.0, deadline - clock())).stdout.strip()
            except subprocess.TimeoutExpired:
                return state
            if state in BOOTED or clock() >= deadline:
                return state
            sleep(1.0)
