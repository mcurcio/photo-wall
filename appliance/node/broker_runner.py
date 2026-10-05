"""Base-owned cold-start composition root from a protected exact boot handoff."""
from __future__ import annotations

import argparse
import http.client
import time
from pathlib import Path
from uuid import UUID, uuid4

from appliance.boot_store import BootStore
from appliance.central_session.http import NodeHTTP
from appliance.central_session.session import NodeSession
from appliance.clock import boot_id, boottime_ms
from appliance.node.app_link import BrokerLinkService
from appliance.node.broker import AppEffectBroker, ColdStart
from appliance.node.lifecycle_storage import FileEffectJournal, primitive, running_from
from appliance.node.online_runner import OnlineRunner
from appliance.node.process_linux import SystemdAppProcessDriver
from appliance.node.recovery_linux import RecoveryClient
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import (
    AppProcessFact,
    NodeEventV2,
    encode_node_message,
    parse_node_message,
)
from contracts.strict_json import loads_object


def emit_process_evidence(store, session, running, state: str) -> None:
    document = store.read("process-evidence") or {"sequence": 0, "last": None, "pending": []}
    identity = [str(running.process.invocation_id), running.app_epoch, state]
    if document["last"] != identity:
        if len(document["pending"]) >= 128:
            raise ValueError("process_evidence_capacity")
        sequence = document["sequence"] + 1
        event = NodeEventV2(session.grant.producer, uuid4(), sequence, boottime_ms(),
                            (AppProcessFact(running.process, running.app_epoch,
                                            running.environment.environment_sha256, state),))
        document = {"sequence": sequence, "last": identity,
                    "pending": [*document["pending"], encode_node_message(event).decode()]}
        store.write("process-evidence", document)
    for _ in range(min(4, len(document["pending"]))):
        event = parse_node_message(document["pending"][0].encode())
        code, _ = session.request("POST", "/v2/node/evidence", encode_node_message(event))
        if code != 200:
            break
        document = {**document, "pending": document["pending"][1:]}
        store.write("process-evidence", document)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/run/photo-wall-node/cold.json"))
    args = parser.parse_args()
    info = args.config.lstat()
    if args.config.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("cold_configuration_ownership")
    value = loads_object(args.config.read_bytes(), max_bytes=16384)
    if value is None or set(value) != {"boot_id", "offer_id", "operation_id", "environment", "base_abi", "graphics_abi", "plugin_abi"}:
        raise ValueError("cold_configuration_invalid")
    kernel_boot_id = boot_id()
    if UUID(value["boot_id"]) != kernel_boot_id:
        raise ValueError("cold_boot_mismatch")
    environment = AppEnvironmentRefV2(**value["environment"])
    store = BootStore(Path("/run/photo-wall-app-broker"), boot_id=kernel_boot_id,
                      policy={"offer_id": value["offer_id"], "environment": value["environment"]})
    driver = SystemdAppProcessDriver(Path("/run/photo-wall-node-storage/app-roots"), store,
                                     **{key: value[key] for key in ("base_abi", "graphics_abi", "plugin_abi")})
    broker = AppEffectBroker(boot_id=kernel_boot_id, offer_id=UUID(value["offer_id"]),
                            authorized_environment=environment,
                            journal=FileEffectJournal(store), driver=driver)
    # Independently scoped broker credential; no HostCore credential or reboot port.
    configuration = Path("/run/photo-wall-node/broker.json")
    status = configuration.lstat()
    if configuration.is_symlink() or status.st_uid != 0 or status.st_mode & 0o077:
        raise ValueError("broker_session_configuration_ownership")
    endpoint = loads_object(configuration.read_bytes(), max_bytes=8192)
    if endpoint is None or set(endpoint) != {"central", "serial"}:
        raise ValueError("broker_session_configuration")
    session = NodeSession(store, NodeHTTP(endpoint["central"], timeout=0.5), owner="app_effect_broker",
                          serial=endpoint["serial"], offer_id=UUID(value["offer_id"]),
                          kernel_boot_id=kernel_boot_id)
    online = OnlineRunner(store, driver, session, RecoveryClient())
    last_online_poll = 0.0
    links = BrokerLinkService(driver, session, Path("/run/photo-wall-app-proof/app-link.sock"))
    try:
        if broker.journal.current() is None:
            broker.cold_start(ColdStart(UUID(value["operation_id"]), kernel_boot_id,
                                        UUID(value["offer_id"]), environment))
        while True:
            if online.broker.record is None:
                broker.reconcile()
            try:
                online.broker.service()
            except (OSError, ValueError, http.client.HTTPException):
                if store.failed:
                    raise
            # Local proofs and stop progress must run even without a Central session.
            links.serve_one()
            try:
                links.remember_grant()
                grant = session.ensure()
                links.remember_grant()
                if grant is not None:
                    current = driver.current()
                    prior = store.read("observed-app")
                    previous = running_from(prior["running"]) if prior and prior.get("running") else None
                    if previous is not None and (current is None or current.process != previous.process):
                        emit_process_evidence(store, session, previous, "exited")
                    if current is not None:
                        emit_process_evidence(store, session, current, "running")
                    store.write("observed-app", {"running": primitive(current) if current else None})
            except (OSError, ValueError, http.client.HTTPException):
                if store.failed:
                    raise
            # A switch converges even when enrollment or Central is unavailable.
            if time.monotonic() - last_online_poll >= 2:
                last_online_poll = time.monotonic()
                try:
                    online.tick()
                except (OSError, ValueError, http.client.HTTPException):
                    if store.failed:
                        raise
            time.sleep(0.1)
    finally:
        driver.stops.close()
        links.close()
        store.close()


if __name__ == "__main__":
    main()
