"""Base-owned bounded recovery of exact manager roots; never controls the Player."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from contracts.app_environment import AppEnvironmentRefV2


class ManagerLauncher(Protocol):
    def verify(self, root_digest: str) -> bool: ...
    def start(self, root_digest: str) -> bool:
        """Return true only after observing successful startup, not spawn acceptance."""
        ...


@dataclass(frozen=True)
class ManagerRecoveryState:
    attempts: int = 0
    running_root: str | None = None
    fault: str | None = None


class ManagerRecoveryStore(Protocol):
    def load(self) -> ManagerRecoveryState: ...
    def save(self, state: ManagerRecoveryState) -> None:
        """Persist to the protected boot-scoped store before returning."""
        ...


class ManagerRecovery:
    """Base supervisor with a charged-before-spawn, boot-scoped recovery budget.

    Reconstructing with the same store retains an interrupted start as unknown;
    only an explicit base observation can release that fence. Store adapters must
    enforce one writer and bind the store to this boot and exact root policy.
    """

    def __init__(self, launcher: ManagerLauncher, *, primary: str, fallback: str | None,
                 store: ManagerRecoveryStore, budget: int = 2):
        if (not isinstance(primary, str) or not re.fullmatch(r"[0-9a-f]{64}", primary)
                or (fallback is not None and (not isinstance(fallback, str) or not re.fullmatch(r"[0-9a-f]{64}", fallback)))
                or type(budget) is not int or not 1 <= budget <= 16):
            raise ValueError("invalid_manager_recovery_policy")
        if fallback is None:
            budget = 1
        state = store.load()
        if (type(state) is not ManagerRecoveryState or type(state.attempts) is not int
                or not 0 <= state.attempts <= budget
                or state.running_root not in (None, primary, fallback)
                or state.fault not in (None, "manager_start_unknown", "manager_recovery_required")
                or (state.attempts == 0 and (state.running_root is not None or state.fault))
                or (state.running_root is not None and state.fault is not None)):
            raise ValueError("invalid_manager_recovery_state")
        self.launcher = launcher
        self.primary = primary
        self.fallback = fallback
        self.budget = budget
        self.store = store
        self.state = state
        self._storage_failed = False

    def _save(self, state: ManagerRecoveryState) -> None:
        if self._storage_failed:
            raise ValueError("manager_store_unavailable")
        self.state = state
        try:
            self.store.save(state)
        except Exception:
            self._storage_failed = True
            raise

    def recover(self) -> ManagerRecoveryState:
        if self._storage_failed:
            raise ValueError("manager_store_unavailable")
        if self.state.running_root is not None or self.state.fault == "manager_start_unknown":
            return self.state
        while self.state.attempts < self.budget:
            root = self.primary if self.state.attempts == 0 else self.fallback
            self._save(ManagerRecoveryState(self.state.attempts + 1,
                                             fault="manager_start_unknown"))
            try:
                verified = self.launcher.verify(root)
                started = self.launcher.start(root) if verified is True else False
                if type(started) is not bool:
                    raise ValueError("manager_start_outcome_invalid")
            except Exception:
                return self.state
            if started:
                self._save(ManagerRecoveryState(self.state.attempts, root))
                return self.state
            self._save(ManagerRecoveryState(self.state.attempts))
        self._save(ManagerRecoveryState(self.state.attempts,
                                        fault="manager_recovery_required"))
        return self.state

    def observed_start_result(self, root_digest: str, *, running: bool) -> None:
        expected = self.primary if self.state.attempts == 1 else self.fallback
        if (self.state.fault != "manager_start_unknown" or root_digest != expected
                or type(running) is not bool):
            raise ValueError("manager_reconciliation_mismatch")
        self._save(ManagerRecoveryState(self.state.attempts,
                                        root_digest if running else None))

    def observed_exit(self, root_digest: str) -> None:
        if self.state.running_root == root_digest:
            self._save(ManagerRecoveryState(self.state.attempts))


# App preparation owns no effect driver, active pointer, reboot or display port.


class EnvironmentPreparer(Protocol):
    def prepare(self, environment: AppEnvironmentRefV2) -> bool:
        """Stage/verify exact bytes within reserved background resource headroom."""
        ...


@dataclass(frozen=True)
class Preparation:
    attempt_id: UUID
    environment: AppEnvironmentRefV2
    state: str
    fault: str | None = None


class AppManager:
    """Serial control-loop owner; preparers must not mutate effect/display state."""

    def __init__(self, preparer: EnvironmentPreparer, *, max_attempts: int = 128):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 1024:
            raise ValueError("attempt_bound_invalid")
        self.preparer = preparer
        self.max_attempts = max_attempts
        self._attempts: dict[UUID, Preparation] = {}

    def prepare(self, attempt_id: UUID, environment: AppEnvironmentRefV2) -> Preparation:
        previous = self._attempts.get(attempt_id)
        if previous is not None:
            if previous.environment != environment:
                raise ValueError("attempt_identity_conflict")
            return previous
        if len(self._attempts) >= self.max_attempts:
            raise ValueError("attempt_capacity")
        self._attempts[attempt_id] = Preparation(attempt_id, environment, "preparing")
        try:
            prepared = self.preparer.prepare(environment)
        except Exception:
            prepared = False
        # Supersession may happen while a bounded preparer performs background IO.
        current = self._attempts[attempt_id]
        if current.state == "superseded":
            return current
        result = Preparation(attempt_id, environment, "prepared" if prepared else "refused",
                             None if prepared else "preparation_failed")
        self._attempts[attempt_id] = result
        return result

    def supersede(self, attempt_id: UUID) -> Preparation:
        previous = self._attempts[attempt_id]
        result = Preparation(attempt_id, previous.environment, "superseded")
        self._attempts[attempt_id] = result
        return result
