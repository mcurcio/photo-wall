"""App-effect and exact-manager recovery journals, separate from HostCore."""
from __future__ import annotations

import json
from dataclasses import asdict
from uuid import UUID

from appliance.boot_store import BootStore
from appliance.node.broker import ColdStart, EffectRecord, RunningApp
from appliance.node.manager import ManagerRecoveryState
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import NodeProcessIdentity


def primitive(value) -> dict:
    return json.loads(json.dumps(asdict(value), default=str))


def running_from(value: dict) -> RunningApp:
    row = dict(value)
    row["environment"] = AppEnvironmentRefV2(**row["environment"])
    row["process"] = NodeProcessIdentity(**{**row["process"], "invocation_id": UUID(row["process"]["invocation_id"])})
    row["operation_id"] = UUID(row["operation_id"])
    return RunningApp(**row)


class FileEffectJournal:
    def __init__(self, store: BootStore, *, capacity: int = 1024):
        self.store, self.capacity = store, capacity
        self.data = store.read("effects") or {"current": None, "records": {}}
        if set(self.data) != {"current", "records"} or not isinstance(self.data["records"], dict) or len(self.data["records"]) > capacity:
            raise ValueError("effect_journal_invalid")
        for key in self.data["records"]:
            self.get(UUID(key))
        self.current()

    def get(self, operation_id: UUID) -> EffectRecord | None:
        row = self.data["records"].get(str(operation_id))
        if row is None:
            return None
        command = dict(row["command"])
        for key in ("operation_id", "kernel_boot_id", "offer_id"):
            command[key] = UUID(command[key])
        command["environment"] = AppEnvironmentRefV2(**command["environment"])
        command = ColdStart(**command)
        if command.operation_id != operation_id or row["phase"] not in ("intent_start", "effect_unknown", "running", "exited"):
            raise ValueError("effect_journal_identity_or_phase")
        return EffectRecord(command, row["phase"], running_from(row["running"]) if row["running"] else None, row["fault"])

    def current(self) -> EffectRecord | None:
        key = self.data["current"]
        if key is None:
            return None
        record = self.get(UUID(key))
        if record is None:
            raise ValueError("effect_journal_current_missing")
        return record

    def put(self, record: EffectRecord) -> None:
        key = str(record.command.operation_id)
        if key not in self.data["records"] and len(self.data["records"]) >= self.capacity:
            raise ValueError("effect_journal_capacity")
        data = {"current": key, "records": {**self.data["records"], key: primitive(record)}}
        self.store.write("effects", data)
        self.data = data


class FileManagerRecoveryStore:
    def __init__(self, store: BootStore):
        self.store = store

    def load(self) -> ManagerRecoveryState:
        row = self.store.read("manager")
        return ManagerRecoveryState(**row) if row is not None else ManagerRecoveryState()

    def save(self, state: ManagerRecoveryState) -> None:
        self.store.write("manager", primitive(state))
