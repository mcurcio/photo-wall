"""Read-only, same-boot evidence for the base-owned data-only Player app.

This collector never promotes health or authorizes a mutation. A selected,
verified root and a stable systemd process sample are separate facts. Two brief
executor snapshots fence root verification and PID1 sampling without holding
its mutation lock across slow I/O.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from appliance.app_executor import AppExecutor, ExecutorError, expected_abi
from appliance.app_payload import PayloadError, verify_root
from appliance.process_identity import ProcessSample, ProcessSampler, SystemdProcessSampler

_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


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
