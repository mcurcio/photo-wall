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
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from appliance.node.boot_stage import DIRECTORY as BOOT_STAGE_DIRECTORY
from appliance.node.boot_stage import read_boot_report
from appliance.node.host import HostCore
from appliance.node.host_linux import LinuxHostSampler, SystemdRebootDriver, boot_id, boottime_ms
from appliance.node.host_storage import FileRebootJournal, RebootDelivery
from appliance.node.http import NodeHTTP
from appliance.node.recovery import RecoverySupervisor
from appliance.node.recovery_linux import RecoveryObserver, RecoveryServer
from appliance.node.session import REFUSED, NodeSession
from appliance.node.storage import BootStore
from contracts.node_commands import parse_reboot_request
from contracts.node_host_facts import (
    HostFactsV2,
    encode_host_facts,
    fact_values_document,
    valid_fact,
)
from contracts.node_observation import (
    HOST_OBSERVATION_INTERVAL_SECONDS,
    HostObservationV2,
    encode_host_observation,
    valid_metrics,
)
from contracts.node_protocol import encode_node_message
from contracts.strict_json import loads_object


class HostRunner:
    # The observation cadence (console DDD §63): every tick samples and journals; one
    # observation is posted per contract interval on this process's own monotonic clock, and
    # the first at once after each new session. `_posted` is (session id, monotonic seconds)
    # of the last post. Central coalesces faster posts anyway; this only saves it the work.
    monotonic = staticmethod(time.monotonic)
    _posted: tuple | None = None
    # The host facts sender (console DDD §64), in memory only: `_facts` is the document last
    # built (None before the first post) and `_facts_state` one of "pending", "stored",
    # "dropped" or "off". A process start therefore sends once. "off" (a Central replica
    # without the route, 404) lasts FACTS_ROUTE_RETRY_SECONDS on this process's monotonic
    # clock (`_facts_off_at`), then the document is pending again, so a rolling deploy or a
    # rollback does not silence facts for the rest of the boot.
    FACTS_ROUTE_RETRY_SECONDS = 3600
    _facts: HostFactsV2 | None = None
    _facts_state: str = "pending"
    _facts_off_at: float | None = None
    # The base this boot runs, as the node records it: the tag of the boot offer whose base the
    # initramfs verified and mounted, carried into host.json by bootstrap.py from the boot
    # handoff. Reported in the facts record (`base_tag`) beside Central's offer, never as it.
    base_tag: str | None = None
    # The base boot stages' records (boot_stage.py), folded into the facts record's `boot`.
    boot_stage_directory: Path = BOOT_STAGE_DIRECTORY

    def __init__(self, store: BootStore, transport: NodeHTTP, *, serial: str,
                 offer_id: UUID, kernel_boot_id: UUID, monotonic=time.monotonic,
                 base_tag: str | None = None):
        self.store, self.transport = store, transport
        self.base_tag = base_tag
        self.monotonic = monotonic
        self.journal = FileRebootJournal(store)
        self.delivery = RebootDelivery(store)
        self.session = NodeSession(store, transport, owner="host_core", serial=serial,
                                   offer_id=offer_id, kernel_boot_id=kernel_boot_id)
        self.core = None
        self.sampler = LinuxHostSampler()
        self.recovery = RecoverySupervisor(store, SystemdRebootDriver(), RecoveryObserver())

    @property
    def claim(self):
        return self.session.claim

    def establish_session(self) -> bool:
        """Reuse the locally unexpired grant; a renewed session gets a new HostCore scope."""
        grant = self.session.ensure()
        if grant is None:
            return False
        if self.core is None or self.core.session_id != grant.session_id:
            self.core = HostCore(producer=grant.producer, session_id=grant.session_id,
                                 offer_id=grant.offer_id, journal=self.journal, driver=SystemdRebootDriver())
        return True

    def _send_evidence(self, message) -> bool:
        status, _ = self.session.request("POST", "/v2/node/evidence", encode_node_message(message))
        return status == 200

    def tick(self) -> None:
        now = boottime_ms()
        recovery_metrics, recovery_fault = self.recovery.telemetry()
        metrics = (self.sampler.sample() + self.sampler.throttling() + self.sampler.supervision()
                   + self.sampler.memory_rows() + recovery_metrics)
        self.store.write("observation", {"sampled_boottime_ms": now, "metrics": metrics})
        if not self.establish_session():
            return
        # New authority is polled before backlog or telemetry HTTP. No network
        # response is required between durable admission and the effect fence.
        try:
            self._poll_commands()
        except (OSError, ValueError, http.client.HTTPException):
            if self.store.failed:
                raise
        if self._observation_due():
            self._posted = (self.core.session_id, self.monotonic())
            observation = HostObservationV2(self.core.producer, self.journal.next_sequence(),
                                            now, valid_metrics(metrics),
                                            fault_code=recovery_fault)
            try:
                self.session.request("POST", "/v2/node/observations", encode_host_observation(observation))
            except (OSError, ValueError, http.client.HTTPException):
                pass
            self._send_facts(now)
        self.delivery.flush(self.journal, session_id=self.claim.session_id,
                            send=self._send_evidence, budget=2)

    def _send_facts(self, now_ms: int) -> None:
        """The §64 state machine, run once per observation post: facts are read here only, so
        a flapping link sends at most one document per interval. A new document (next journal
        sequence) is built on the first post and whenever a value or the producer changed;
        otherwise a pending document is resent unchanged: after no answer, a 5xx, a 429 or a
        refused session (401/403). A refusal of the document itself (another 4xx) drops it
        until a value changes; a 404 turns facts off for FACTS_ROUTE_RETRY_SECONDS."""
        if self._facts_state == "off":
            if self.monotonic() - self._facts_off_at < self.FACTS_ROUTE_RETRY_SECONDS:
                return
            self._facts_state = "pending"
        values = {**self.sampler.facts(),
                  "base_tag": self.base_tag if valid_fact("base_tag", self.base_tag) else None}
        boot = read_boot_report(directory=self.boot_stage_directory,
                                units=lambda: self.sampler.failed_units())
        # Compared as the whole JSON-ready document (`boot` included), the same comparison
        # Central makes; the candidate's sequence is spent only when something changed.
        candidate = HostFactsV2(self.core.producer, 1, now_ms, **values, boot=boot)
        current = self._facts
        if (current is None or current.producer != candidate.producer
                or fact_values_document(current) != fact_values_document(candidate)):
            self._facts = replace(candidate, sequence=self.journal.next_sequence())
            self._facts_state = "pending"
        if self._facts_state != "pending":
            return
        try:
            status, _ = self.session.request("POST", "/v2/node/host-facts", encode_host_facts(self._facts))
        except (OSError, ValueError, http.client.HTTPException):
            return  # no answer: the same document at the next post
        if status == 200:  # recorded, duplicate or stale
            self._facts_state = "stored"
        elif status == 404:
            self._facts_state, self._facts_off_at = "off", self.monotonic()
        elif status in REFUSED or status == 429:
            # A refused session (401/403) is temporary: the next ensure() enrolls a new one
            # under the same producer, and that tick's post resends this document.
            return
        elif 400 <= status < 500:
            self._facts_state = "dropped"  # Central refused this document (409, 422, ...)

    def _observation_due(self) -> bool:
        if self._posted is None or self._posted[0] != self.core.session_id:
            return True
        return self.monotonic() - self._posted[1] >= HOST_OBSERVATION_INTERVAL_SECONDS

    def _poll_commands(self) -> None:
        status, raw = self.session.request("GET", "/v2/node/commands")
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
    if value is None or set(value) != {"central", "serial", "offer_id", "base_tag"}:
        raise ValueError("host_configuration_invalid")
    kernel_boot_id = boot_id()
    store = BootStore(args.state, boot_id=kernel_boot_id,
                      policy={"owner": "host_core", "offer_id": value["offer_id"], "serial": value["serial"]})
    runner = HostRunner(store, NodeHTTP(value["central"], timeout=0.5), serial=value["serial"],
                        offer_id=UUID(value["offer_id"]), kernel_boot_id=kernel_boot_id,
                        base_tag=value["base_tag"])
    recovery = runner.recovery
    server = RecoveryServer(recovery)
    last_tick = 0.0
    try:
        while True:
            # The local recovery owner runs before telemetry/session work.
            server.serve_one()
            recovery.service(now_ms=boottime_ms())
            try:
                if time.monotonic() - last_tick >= 2:
                    last_tick = time.monotonic()
                    runner.tick()
            except (OSError, ValueError, http.client.HTTPException):
                if store.failed:
                    raise
                # Fixed bounded cadence; no credential or packet logging.
            time.sleep(0.05)
    finally:
        server.close()
        store.close()


if __name__ == "__main__":
    main()
