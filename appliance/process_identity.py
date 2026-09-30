"""Base-owned PID1 and kernel process identity sampling for the Player unit.

The sample is a local concurrency fence. It does not prove app control, trusted
device identity, or visible output.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from appliance.app_launcher import UNIT

_INVOCATION = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class ProcessSample:
    pid: int
    start_ticks: int
    invocation_id: str


class ProcessSampler(Protocol):
    def sample(self) -> ProcessSample | None: ...


class SystemdProcessSampler:
    """Sample PID1's invocation and the current MainPID's kernel birth tick."""

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root

    def sample(self) -> ProcessSample | None:
        try:
            result = subprocess.run(
                ["systemctl", "show", "--property=MainPID,InvocationID,ActiveState", UNIT],
                capture_output=True, text=True, check=False, timeout=3)
            if result.returncode != 0:
                return None
            rows = [line.split("=", 1) for line in result.stdout.splitlines()]
            if len(rows) != 3 or any(len(row) != 2 for row in rows):
                return None
            values = dict(rows)
            pid_text, invocation = values.get("MainPID"), values.get("InvocationID")
            if (set(values) != {"MainPID", "InvocationID", "ActiveState"}
                    or values["ActiveState"] != "active" or pid_text is None
                    or not pid_text.isdecimal() or int(pid_text) <= 0
                    or invocation is None or _INVOCATION.fullmatch(invocation) is None):
                return None
            pid = int(pid_text)
            with (self.proc_root / pid_text / "stat").open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096 or not raw.startswith(f"{pid} (".encode()):
                return None
            close = raw.rfind(b") ")
            if close < 0:
                return None
            fields = raw[close + 2:].split()
            # /proc/<pid>/stat field 22 is starttime; field 3 begins here.
            if len(fields) < 20 or not fields[19].isdigit():
                return None
            ticks = int(fields[19])
            return ProcessSample(pid, ticks, invocation) if ticks > 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
