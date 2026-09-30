"""Read-only, same-boot evidence for the base-owned data-only Player app.

This collector never promotes health or authorizes a mutation. A selected,
verified root and a stable systemd process sample are separate facts. Two brief
executor snapshots fence root verification and PID1 sampling without holding
its mutation lock across slow I/O.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from appliance.app_executor import UNIT, AppExecutor, ExecutorError, expected_abi
from appliance.app_payload import PayloadError, verify_root

_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_INVOCATION = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class ProcessSample:
    pid: int
    start_ticks: int
    invocation_id: str


@dataclass(frozen=True, slots=True)
class RunningEvidence:
    sha256: str
    process: ProcessSample


@dataclass(frozen=True, slots=True)
class AppEvidence:
    kernel_boot_id: str
    installed_sha256: str | None
    running: RunningEvidence | None
    installed_reason: str | None
    running_reason: str | None


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


class AppEvidenceCollector:
    """Observe one terminal data-only attempt without changing executor state."""

    def __init__(self, executor: AppExecutor, *,
                 sampler: ProcessSampler | None = None,
                 expected_abi_reader: Callable[[], str] = expected_abi) -> None:
        self.executor = executor
        self.sampler = sampler or SystemdProcessSampler()
        self.expected_abi_reader = expected_abi_reader

    @staticmethod
    def _unknown(boot_id: str, reason: str) -> AppEvidence:
        return AppEvidence(boot_id, None, None, reason, reason)

    def collect(self, kernel_boot_id: str) -> AppEvidence:
        if _BOOT_ID.fullmatch(kernel_boot_id) is None:
            raise ValueError("boot_id_invalid")
        try:
            abi = self.expected_abi_reader()
            before = self.executor.selected_snapshot()
            verify_root(self.executor.roots / before.digest, expected_abi=abi)
            sample = self._sample_selected(kernel_boot_id, before.digest)
            after = self.executor.selected_snapshot()
            if after != before:
                return self._unknown(kernel_boot_id, "executor_selection_changed")
            return sample
        except (OSError, ExecutorError, PayloadError) as exc:
            reason = str(exc) if isinstance(exc, (ExecutorError, PayloadError)) else "evidence_unavailable"
            return self._unknown(kernel_boot_id, reason)

    def _sample_selected(self, boot_id: str, selected: str) -> AppEvidence:
        if not self.executor.service.active():
            return AppEvidence(boot_id, selected, None, None, "unit_inactive")
        first = self.sampler.sample()
        digest = self.executor.service.running_digest()
        second = self.sampler.sample()
        if (first is None or second != first or digest != selected
                or not self.executor.service.active()):
            return AppEvidence(boot_id, selected, None, None, "process_unconfirmed")
        return AppEvidence(boot_id, selected, RunningEvidence(selected, first), None, None)
