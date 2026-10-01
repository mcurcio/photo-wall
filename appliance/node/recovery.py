"""Host-owned local recovery authority, independent of remote command sessions.

One writer persists immutable deadlines and reboot intent. Transport and Linux
observation are ports; this module cannot access broker journals or app roots.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from uuid import UUID

from contracts.node_protocol import NodeProcessIdentity, counter, digest, identifier

STOP_TIMEOUT_SECONDS = 30
STOP_MARGIN_MS = 10000
STOP_BUDGET_MS = 2 * STOP_TIMEOUT_SECONDS * 1000 + STOP_MARGIN_MS
RESTORE_BUDGET_MS = 60000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


@dataclass(frozen=True)
class RecoveryObligation:
    boot_id: UUID
    operation_id: UUID
    request_sha256: str
    old_process: NodeProcessIdentity
    old_app_epoch: int
    old_environment: str
    target: str
    fallback: str
    armed_ms: int
    stop_deadline_ms: int
    restore_deadline_ms: int
    policy_revision: int = 1

    def __post_init__(self):
        identifier(self.boot_id)
        identifier(self.operation_id)
        for value in (self.request_sha256, self.old_environment, self.target, self.fallback):
            digest(value)
        if type(self.old_process) is not NodeProcessIdentity or type(self.policy_revision) is not int or self.policy_revision != 1:
            raise ValueError("recovery_policy")
        for value in (self.old_app_epoch, self.armed_ms, self.stop_deadline_ms, self.restore_deadline_ms):
            counter(value, 1)
        if (self.stop_deadline_ms != self.armed_ms + STOP_BUDGET_MS
                or self.restore_deadline_ms != self.stop_deadline_ms + RESTORE_BUDGET_MS):
            raise ValueError("recovery_deadline_policy")

    def document(self):
        return json.loads(canonical(asdict(self)))

    @property
    def receipt(self):
        return hashlib.sha256(canonical(self.document())).hexdigest()

    @classmethod
    def parse(cls, value):
        row = dict(value)
        row["boot_id"], row["operation_id"] = UUID(row["boot_id"]), UUID(row["operation_id"])
        process = row["old_process"]
        row["old_process"] = NodeProcessIdentity(**{**process, "invocation_id": UUID(process["invocation_id"])})
        return cls(**row)


class RecoverySupervisor:
    def __init__(self, store, driver, observer):
        self.store, self.driver, self.observer = store, driver, observer

    def _load(self):
        value = self.store.read("local-recovery") or {"schema": 1, "records": {}}
        if set(value) != {"schema", "records"} or value["schema"] != 1 or type(value["records"]) is not dict or len(value["records"]) > 1024:
            raise ValueError("recovery_journal")
        for key, row in value["records"].items():
            obligation = RecoveryObligation.parse(row["obligation"])
            if (key != str(obligation.operation_id) or str(obligation.boot_id) != self.store.binding["boot_id"]
                    or row["phase"] not in ("armed", "stopped", "controlled", "reboot_intent", "reboot_requested", "reboot_unknown")):
                raise ValueError("recovery_journal")
        return value

    def _save(self, value, key, row):
        self.store.write("local-recovery", {**value, "records": {**value["records"], key: row}})

    def arm(self, obligation, *, now_ms):
        if str(obligation.boot_id) != self.store.binding["boot_id"]:
            raise ValueError("recovery_boot")
        value = self._load()
        key = str(obligation.operation_id)
        prior = value["records"].get(key)
        if prior is not None:
            if prior["obligation"] != obligation.document():
                raise ValueError("recovery_conflict")
            if prior["phase"].startswith("reboot"):
                raise ValueError("recovery_reboot_already_intended")
            return obligation.receipt
        if (len(value["records"]) >= 1024 or any(r["phase"] != "controlled" for r in value["records"].values())
                or not obligation.armed_ms <= now_ms < obligation.stop_deadline_ms):
            raise ValueError("recovery_admission")
        self._save(value, key, {"obligation": obligation.document(), "phase": "armed", "diagnostic": None})
        return obligation.receipt

    def advance(self, operation_id, receipt, progress, *, now_ms):
        value = self._load()
        key = str(operation_id)
        row = value["records"][key]
        obligation = RecoveryObligation.parse(row["obligation"])
        if receipt != obligation.receipt:
            raise ValueError("recovery_receipt")
        if row["phase"].startswith("reboot"):
            raise ValueError("recovery_reboot_already_intended")
        if row["phase"] == "controlled":
            return
        if progress == {"kind": "stopped"}:
            if not self.observer.stopped(obligation):
                raise ValueError("recovery_stop_unproven")
            self._save(value, key, {**row, "phase": "stopped"})
        elif self.observer.controlled(obligation, progress, now_ms=now_ms):
            self._save(value, key, {**row, "phase": "controlled"})
        else:
            raise ValueError("recovery_control_unproven")

    def service(self, *, now_ms):
        value = self._load()
        for key, row in value["records"].items():
            if row["phase"] not in ("armed", "stopped"):
                continue
            obligation = RecoveryObligation.parse(row["obligation"])
            if row["phase"] == "armed":
                try:
                    if self.observer.stopped(obligation):
                        row = {**row, "phase": "stopped"}
                        self._save(value, key, row)
                except (OSError, ValueError, subprocess.SubprocessError):
                    if self.store.failed:
                        raise
            deadline = obligation.stop_deadline_ms if row["phase"] == "armed" else obligation.restore_deadline_ms
            if now_ms < deadline:
                continue
            # An uncertain reboot dispatch can never be repeated after restart.
            row = {**row, "phase": "reboot_intent", "diagnostic": "stop_deadline" if row["phase"] == "armed" else "restore_deadline"}
            self._save(value, key, row)
            try:
                initiated = self.driver.initiate()
            except (OSError, ValueError, subprocess.SubprocessError):
                initiated = False
            self._save(value, key, {**row, "phase": "reboot_requested" if initiated else "reboot_unknown"})
