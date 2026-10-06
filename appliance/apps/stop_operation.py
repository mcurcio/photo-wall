"""Owned stop port: observation outlives callers and never renews authority."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import InvalidStateError
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from appliance.apps.broker import RunningApp
from appliance.apps.lifecycle_storage import primitive, running_from
from contracts.node_protocol import counter, digest, identifier, token


@dataclass(frozen=True)
class StopRequest:
    operation_id: UUID
    boot_id: UUID
    command_sha256: str
    old: RunningApp
    dispatch_not_after_boottime_ms: int

    def __post_init__(self):
        for value in (self.operation_id, self.boot_id):
            identifier(value)
        digest(self.command_sha256)
        counter(self.dispatch_not_after_boottime_ms, 1)
        if type(self.old) is not RunningApp:
            raise ValueError("stop_request_identity")


@dataclass(frozen=True)
class StopCompleted:
    request: StopRequest
    observed_boottime_ms: int

    def __post_init__(self):
        if type(self.request) is not StopRequest:
            raise ValueError("stop_completion_identity")
        counter(self.observed_boottime_ms, 1)


def stop_request_document(request: StopRequest) -> dict:
    """One persisted representation shared by broker, adapter and recovery digest."""
    if type(request) is not StopRequest:
        raise ValueError("stop_request_identity")
    return primitive(request)


def stop_request_from(value: dict) -> StopRequest:
    if type(value) is not dict or set(value) != {
            "operation_id", "boot_id", "command_sha256", "old", "dispatch_not_after_boottime_ms"}:
        raise ValueError("stop_request_document")
    return StopRequest(UUID(value["operation_id"]), UUID(value["boot_id"]), value["command_sha256"],
        running_from(value["old"]), value["dispatch_not_after_boottime_ms"])


def stop_request_digest(request: StopRequest) -> str:
    raw = json.dumps(stop_request_document(request), sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


class StopGuaranteeUnavailable(ValueError):
    def __init__(self, code: str, diagnostic_id: str | None = None):
        token(code)
        if diagnostic_id is not None:
            token(diagnostic_id)
        self.code, self.diagnostic_id = code, diagnostic_id
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
            raise StopGuaranteeUnavailable(row["fault"], row.get("diagnostic_id"))
        if row["completed_ms"] is None:
            raise InvalidStateError("stop_pending")
        return StopCompleted(self.request, row["completed_ms"])
