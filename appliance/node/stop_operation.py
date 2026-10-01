"""Owned stop port: observation outlives callers and never renews authority."""
from __future__ import annotations

from concurrent.futures import InvalidStateError
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from appliance.node.broker import RunningApp
from contracts.node_protocol import counter, digest, identifier


@dataclass(frozen=True)
class StopRequest:
    operation_id: UUID
    boot_id: UUID
    command_sha256: str
    permit_id: UUID
    permit_sha256: str
    old: RunningApp
    dispatch_not_after_boottime_ms: int

    def __post_init__(self):
        for value in (self.operation_id, self.boot_id, self.permit_id):
            identifier(value)
        for value in (self.command_sha256, self.permit_sha256):
            digest(value)
        counter(self.dispatch_not_after_boottime_ms, 1)
        if type(self.old) is not RunningApp:
            raise ValueError("stop_request_identity")


@dataclass(frozen=True)
class StopCompleted:
    request: StopRequest
    observed_boottime_ms: int


class StopGuaranteeUnavailable(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class StopOperation(Protocol):
    def done(self) -> bool: ...
    def result(self) -> StopCompleted: ...


class StopDriver(Protocol):
    def stop(self, request: StopRequest, *, reattach_only: bool) -> StopOperation: ...
    def service(self, *, now_ms: int, budget_ms: int) -> None: ...


class StopView:
    """Read-only view. Dropping a waiter has no effect on the owned operation."""
    def __init__(self, request, read):
        self.request, self._read = request, read

    def done(self):
        row = self._read()
        return row["completed_ms"] is not None or row["fault"] is not None

    def result(self):
        row = self._read()
        if row["fault"] is not None:
            raise StopGuaranteeUnavailable(row["fault"])
        if row["completed_ms"] is None:
            raise InvalidStateError("stop_pending")
        return StopCompleted(self.request, row["completed_ms"])
