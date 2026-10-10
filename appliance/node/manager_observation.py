"""Durable bounded manager observations, independent of broker readiness authority."""
from __future__ import annotations

import http.client

from appliance.kernel.clock import boottime_ms
from contracts.node_preparation import ManagerPreparationV2, encode_manager_preparation


class PreparationObservation:
    def __init__(self, store, session):
        self.store, self.session = store, session

    def flush(self) -> bool:
        row = self.store.read("preparation-observation") or {}
        if row.get("pending") is None:
            return True
        status, _ = self.session.request("POST", "/v2/node/app-preparation", row["pending"].encode())
        if status != 200:
            return False
        self.store.write("preparation-observation", {**row, "pending": None})
        return True

    def sample(self, state: str, *, command=None, fault: str | None = None,
               available_bytes: int | None = None, required_bytes: int | None = None) -> None:
        grant = self.session.grant
        if grant is None or not self.flush():
            return
        row = self.store.read("preparation-observation") or {"sequence": 0}
        context = {"state": state, "operation": str(command.operation_id) if command else None, "fault": fault,
                   "available_bytes": available_bytes, "required_bytes": required_bytes}
        now = boottime_ms()
        if row.get("context") == context and now - row["sampled"] < 10_000:
            return
        event = ManagerPreparationV2(grant.producer, row["sequence"] + 1, now, state,
            operation_id=command.operation_id if command else None,
            target_sha256=command.target.environment_sha256 if command else None,
            fallback_sha256=command.fallback.environment_sha256 if command and command.fallback else None,
            available_bytes=available_bytes, required_bytes=required_bytes, fault=fault)
        self.store.write("preparation-observation", {"sequence": event.sequence, "sampled": now,
            "context": context, "pending": encode_manager_preparation(event).decode()})
        self.flush()

    def failure(self, *, command=None) -> None:
        # The bounded token exposes no arbitrary exception text or credential.
        self.store.write("preparation-local-fault", {"fault": "manager_preparation_fault", "sampled": boottime_ms()})
        try:
            self.sample("fault", command=command, fault="manager_preparation_fault")
        except (OSError, ValueError, http.client.HTTPException):
            pass  # Exact pending event remains journaled for the next poll.
