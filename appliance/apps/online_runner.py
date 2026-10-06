"""Broker HTTP/worker orchestration; the local effect owner converges on its own.

Central supplies the latest desired stage and receives reported effects. Neither
exchange gates local progress: once a stage is accepted and its roots are
prepared, the switch runs even while Central is unreachable.
"""
from __future__ import annotations

import json
from pathlib import Path

from appliance.apps.import_worker import RootImportWorker
from appliance.apps.online_broker import OnlineEffectBroker
from contracts.node_lifecycle import parse_stage_command
from contracts.strict_json import loads_object

PREPARED = Path("/run/photo-wall-node-storage/preparation/prepared.json")


class OnlineRunner:
    def __init__(self, store, driver, session, recovery, *, prepared: Path = PREPARED):
        self.store, self.driver, self.session = store, driver, session
        self.broker = OnlineEffectBroker(store, driver, session, recovery)
        self.worker = RootImportWorker(store)
        self.prepared = prepared

    def tick(self):
        self.broker.reconcile()
        try:
            if self.session.grant is not None:
                self.broker.flush()
                self._poll()
        finally:
            # Local convergence never waits on Central: a failed exchange still advances.
            self._advance()
        self.broker.flush()

    def _poll(self):
        status, raw = self.session.request("GET", "/v2/node/app-commands")
        if status != 200:
            return
        value = loads_object(raw, max_bytes=65536)
        if (value is None or set(value) != {"commands", "scope"} or value["scope"] != "app_effect"
                or not isinstance(value["commands"], list) or len(value["commands"]) > 1):
            raise ValueError("online_command_response")
        if value["commands"]:
            self.broker.accept(parse_stage_command(json.dumps(value["commands"][0]).encode()))

    def _advance(self):
        record = self.broker.record
        if record is None or record["phase"] != "preparing":
            return
        command = parse_stage_command(record["command"].encode())
        if not self.prepared.exists() or self.prepared.is_symlink():
            return
        with self.prepared.open("rb") as source:
            value = loads_object(source.read(8193), max_bytes=8192)
        if (value is None or value.get("command_sha256") != command.command_sha256
                or value.get("operation_id") != str(command.operation_id)):
            return
        self.worker.advance(command)
        if not self.worker.ready(command):
            return
        self.broker.execute()
