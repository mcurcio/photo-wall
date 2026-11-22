"""The app-effect journal, separate from HostCore."""
from __future__ import annotations

import json
from dataclasses import asdict
from uuid import UUID

from appliance.apps.broker import ColdStart, EffectRecord, Restart, RunningApp
from appliance.kernel.boot_store import BootStore
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


# A record's phases; `stopping` is a person's restart in flight (broker.Restart, step `stopping`).
PHASES = ("intent_start", "effect_unknown", "running", "exited", "down", "stopping")


def restart_from(value: object) -> Restart | None:
    """A record's `restart` as the journal wrote it (absent in a record written before a person
    could ask: None); ValueError("effect_journal_identity_or_phase") for anything else."""
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"request_id", "step"}:
        raise ValueError("effect_journal_identity_or_phase")
    try:
        return Restart(**value)
    except (TypeError, ValueError) as error:
        raise ValueError("effect_journal_identity_or_phase") from error


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
        if command.operation_id != operation_id or row["phase"] not in PHASES:
            raise ValueError("effect_journal_identity_or_phase")
        exited_under = row.get("exited_under")
        if exited_under is not None and type(exited_under) is not str:
            raise ValueError("effect_journal_identity_or_phase")
        restart = restart_from(row.get("restart"))
        if (row["phase"] == "stopping") != (restart is not None and restart.step == "stopping"):
            raise ValueError("effect_journal_identity_or_phase")
        return EffectRecord(command, row["phase"], running_from(row["running"]) if row["running"] else None,
                            row["fault"], exited_under, restart)

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

