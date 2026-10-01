"""Independent HostCore composition root; opt-in base service, never imports Player.

The base supplies a root-owned boot configuration and dedicated /run directory.
Local observation continues during Central/session failure. HTTP replies cannot
redirect credentials. Accepted commands are journaled before PID1 invocation.
"""
from __future__ import annotations

import argparse
import http.client
import json
import time
from pathlib import Path
from uuid import UUID

from appliance.node.host import HostCore
from appliance.node.host_linux import LinuxHostSampler, SystemdRebootDriver, boot_id, boottime_ms
from appliance.node.host_storage import FileRebootJournal, RebootDelivery
from appliance.node.http import NodeHTTP
from appliance.node.session import NodeSession
from appliance.node.storage import BootStore
from contracts.node_commands import parse_reboot_request
from contracts.node_observation import HostMetricV2, HostObservationV2, encode_host_observation
from contracts.node_protocol import encode_node_message
from contracts.strict_json import loads_object


class HostRunner:
    def __init__(self, store: BootStore, transport: NodeHTTP, *, serial: str,
                 offer_id: UUID, kernel_boot_id: UUID):
        self.store, self.transport = store, transport
        self.journal = FileRebootJournal(store)
        self.delivery = RebootDelivery(store)
        self.session = NodeSession(store, transport, owner="host_core", serial=serial,
                                   offer_id=offer_id, kernel_boot_id=kernel_boot_id)
        self.core = None
        self.sampler = LinuxHostSampler()

    @property
    def claim(self):
        return self.session.claim

    def establish_session(self) -> bool:
        grant = self.session.ensure()
        if grant is None:
            return False
        self.core = HostCore(producer=grant.producer, session_id=grant.session_id,
                             offer_id=grant.offer_id, session_expires_boottime_ms=grant.expires_boottime_ms,
                             journal=self.journal, driver=SystemdRebootDriver())
        return True

    def _send_evidence(self, message) -> bool:
        status, _ = self.transport.request("POST", "/v2/node/evidence", encode_node_message(message), self.claim)
        return status == 200

    def tick(self) -> None:
        now = boottime_ms()
        metrics = self.sampler.sample() + self.sampler.supervision()
        self.store.write("observation", {"sampled_boottime_ms": now, "metrics": metrics})
        if self.core is None or now >= self.core.session_expires:
            if not self.establish_session():
                return
        # New authority is polled before backlog or telemetry HTTP. No network
        # response is required between durable admission and the effect fence.
        try:
            self._poll_commands()
        except (OSError, ValueError, http.client.HTTPException):
            if self.store.failed:
                raise
        observation = HostObservationV2(self.core.producer, self.journal.next_sequence(),
                                        now, tuple(HostMetricV2(*row) for row in metrics))
        try:
            self.transport.request("POST", "/v2/node/observations",
                                   encode_host_observation(observation), self.claim)
        except (OSError, ValueError, http.client.HTTPException):
            pass
        self.delivery.flush(self.journal, session_id=self.claim.session_id,
                            send=self._send_evidence, budget=2)

    def _poll_commands(self) -> None:
        status, raw = self.transport.request("GET", "/v2/node/commands", claim=self.claim)
        if status != 200:
            return
        document = loads_object(raw, max_bytes=65536)
        if document is None or set(document) != {"commands"} or not isinstance(document["commands"], list) or len(document["commands"]) > 16:
            raise ValueError("host_command_envelope")
        for value in document["commands"]:
            command = parse_reboot_request(json.dumps(value, separators=(",", ":")).encode())
            response = self.core.receive(command, now_ms=boottime_ms())
            if response.decision == "accepted":
                self.core.initiate(command.command_id, now_ms=boottime_ms())



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/run/photo-wall-node/host.json"))
    parser.add_argument("--state", type=Path, default=Path("/run/photo-wall-host-core"))
    args = parser.parse_args()
    info = args.config.lstat()
    if args.config.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("host_configuration_ownership")
    value = loads_object(args.config.read_bytes(), max_bytes=8192)
    if value is None or set(value) != {"central", "serial", "offer_id"}:
        raise ValueError("host_configuration_invalid")
    kernel_boot_id = boot_id()
    store = BootStore(args.state, boot_id=kernel_boot_id,
                      policy={"owner": "host_core", "offer_id": value["offer_id"], "serial": value["serial"]})
    runner = HostRunner(store, NodeHTTP(value["central"]), serial=value["serial"],
                        offer_id=UUID(value["offer_id"]), kernel_boot_id=kernel_boot_id)
    try:
        while True:
            try:
                runner.tick()
            except (OSError, ValueError, http.client.HTTPException):
                if store.failed:
                    raise
                # Fixed bounded cadence; no credential or packet logging.
            time.sleep(2)
    finally:
        store.close()


if __name__ == "__main__":
    main()
