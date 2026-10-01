"""Broker HTTP/worker orchestration; exact local effect owner retains all authority."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from appliance.node.clock import boottime_ms
from appliance.node.import_worker import RootImportWorker
from appliance.node.online_broker import OnlineEffectBroker
from contracts.node_app_link import parse_node_app_link
from contracts.node_lifecycle import (
    NoEffectProofV2,
    encode_no_effect_proof,
    encode_revalidation_grant,
    parse_revalidation_grant,
    parse_stage_command,
    parse_stop_permit,
    parse_stop_permit_receipt,
)
from contracts.strict_json import loads_object


class OnlineRunner:
    def __init__(self, store, driver, session):
        self.store, self.driver, self.session = store, driver, session
        self.broker = OnlineEffectBroker(store, driver, session)
        self.worker = RootImportWorker(store)

    def tick(self):
        self.broker.reconcile()
        self.broker.flush()
        record = self.broker.record
        if record is not None and not record.get("permit"):
            self.broker.cancel_expired(worker_quiescent=self.worker.quiescent())
            self.broker.flush()
            record = self.broker.record
            if record.get("stage_cancelled") and not record.get("stage_closed") and not record["pending"]:
                command = parse_stage_command(record["command"].encode())
                status, raw = self.session.transport.request("GET",
                    f"/v2/node/app-attempts/{command.operation_id}/stop-permit-receipt", claim=self.session.claim)
                if status == 200:
                    self.broker.retain_permit_receipt(parse_stop_permit_receipt(raw))
                # A 404 is only absence, never an immutable cancellation receipt.
                record = self.broker.record
        if record and record.get("permit"):
            self._no_effect()
            record = self.broker.record
        status, raw = self.session.transport.request("GET", "/v2/node/app-commands", claim=self.session.claim)
        if status == 200:
            value = loads_object(raw, max_bytes=65536)
            if value is None or set(value) != {"commands", "scope"} or value["scope"] != "app_effect" or not isinstance(value["commands"], list) or len(value["commands"]) > 1:
                raise ValueError("online_command_response")
            if value["commands"]:
                command = parse_stage_command(json.dumps(value["commands"][0]).encode())
                self.broker.accept(command)
                record = self.broker.record
        if record is None or record["phase"] not in ("preparing", "awaiting_permit"):
            return
        command = parse_stage_command(record["command"].encode())
        if record["phase"] == "preparing":
            prepared = Path("/run/photo-wall-node-storage/preparation/prepared.json")
            if not prepared.exists() or prepared.is_symlink():
                return
            with prepared.open("rb") as source:
                value = loads_object(source.read(8193), max_bytes=8192)
            if value is None or value.get("command_sha256") != command.command_sha256 or value.get("operation_id") != str(command.operation_id):
                return
            self.worker.advance(command)
            if not self.worker.ready(command):
                return
            self.broker.ready()
            record = self.broker.record
        # Retry identical readiness bytes after a lost response. An accepted stop
        # permit is immutable; a timeout never manufactures a new authorization.
        status, raw = self.session.transport.request("POST", "/v2/node/app-ready",
                                                       record["ready"].encode(), self.session.claim)
        if status == 200:
            self.broker.execute(parse_stop_permit(raw))
        else:
            status, raw = self.session.transport.request("GET",
                f"/v2/node/app-attempts/{command.operation_id}/stop-permit-receipt", claim=self.session.claim)
            if status == 200:
                self.broker.retain_permit_receipt(parse_stop_permit_receipt(raw))

    def _no_effect(self):
        record = self.broker.record
        if record["phase"] in ("running", "fallback_running") or record.get("no_effect_released") or record.get("intent_stop_written"):
            return
        permit = parse_stop_permit(record["permit"].encode())
        if boottime_ms() < permit.expires_boottime_ms + 2000:
            return
        command = parse_stage_command(record["command"].encode())
        old = self.broker._old(command)
        if not self.driver.quiescent(old) or not self.worker.quiescent():
            return
        if record["phase"] != "no_stop_quiescent":
            if record["pending"]:
                return
            self.broker._event("no_stop_quiescent", running=old)
        self.broker.flush()
        record = self.broker.record
        if record["pending"]:
            return
        if not record.get("revalidation"):
            body = json.dumps({"operation_id": str(command.operation_id),
                               "quiescent_event_id": record["quiescent_event_id"],
                               "revalidation_id": record["revalidation_id"]}).encode()
            status, raw = self.session.transport.request("POST", "/v2/node/app-revalidation", body, self.session.claim)
            if status != 200:
                return
            grant = parse_revalidation_grant(raw)
            if (grant.producer != command.producer or grant.operation_id != command.operation_id
                    or grant.revalidation_id != UUID(record["revalidation_id"])
                    or grant.quiescent_event_id != UUID(record["quiescent_event_id"])
                    or grant.carrier_session_id != self.session.claim.session_id
                    or grant.old_process != old.process or grant.old_app_epoch != old.app_epoch
                    or grant.old_environment_sha256 != old.environment.environment_sha256):
                raise ValueError("online_revalidation_binding")
            self.broker._save({**record, "revalidation": encode_revalidation_grant(grant).decode()})
            record = self.broker.record
        grant = parse_revalidation_grant(record["revalidation"].encode())
        latest = self.store.read("latest-app-link")
        if latest is None or boottime_ms() >= grant.expires_boottime_ms:
            return
        link = parse_node_app_link(latest["link"].encode())
        if (link.challenge.process != old.process or link.challenge.app_epoch != old.app_epoch
                or link.challenge.sampled_boottime_ms <= record["quiescent_at"]):
            return
        proof = NoEffectProofV2(command.operation_id, grant.revalidation_id, grant.quiescent_event_id, link)
        status, raw = self.session.transport.request("POST", "/v2/node/app-no-effect", encode_no_effect_proof(proof), self.session.claim)
        if status == 200:
            result = loads_object(raw, max_bytes=8192)
            if result is not None and result.get("released") is True:
                self.broker._save({**record, "no_effect_released": True})
