"""Production DisplayHost network driver; native policy remains on its owning thread.

A bounded worker handles Central and durable session I/O. It never touches Weston
or domain state. Responses cross back as inert decisions and are revalidated
against current local identity, receipt, process and deadline before effects.
"""

from __future__ import annotations

import http.client
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from appliance.node.clock import boottime_ms
from appliance.node.http import NodeHTTP
from appliance.node.session import NodeSession
from appliance.node.storage import BootStore
from contracts.node_display import (
    DisplayExchange,
    DisplayReceipt,
    Surface,
    encode_display_exchange,
    parse_display_decision,
)
from contracts.node_protocol import NodeSnapshotV2, SurfaceFact, encode_node_message, fact_key

from .weston import CompositorPresentation, DiagnosticRelease, RoleRemoval, SurfaceGrant


@dataclass(frozen=True)
class Sample:
    states: tuple
    receipts: tuple[DisplayReceipt, ...]
    completed: tuple[tuple[str, tuple[UUID, int]], ...]
    facts: tuple[SurfaceFact, ...]
    sampled_boottime_ms: int


class DisplayService:
    def __init__(self, controller, *, central: str, serial: str, offer_id: UUID, runtime: Path):
        self.controller = controller
        self.receipts: dict[str, DisplayReceipt] = {}
        self.completed: dict[str, tuple[UUID, int]] = {}
        self.session_id: UUID | None = None
        self.facts: dict[tuple, SurfaceFact] = {}
        self.requests: queue.Queue[Sample] = queue.Queue(maxsize=1)
        self.responses: queue.Queue[tuple] = queue.Queue(maxsize=16)
        self.stop = threading.Event()
        self.next_sample = 0
        self.failure: BaseException | None = None
        self.thread = threading.Thread(
            target=self._work,
            args=(central, serial, offer_id, runtime),
            daemon=True,
            name="display-central",
        )
        self.thread.start()

    def observe(self, observed) -> None:
        if isinstance(observed, CompositorPresentation):
            fact = observed.fact
            surface = Surface(
                fact.output,
                fact.process,
                fact.app_epoch,
                fact.binding_generation,
                fact.config_revision,
                fact.frame_id,
            )
            # Native uses CLOCK_MONOTONIC. Preserve its age when converting to boot time.
            age = max(0, time.monotonic_ns() // 1_000_000 - observed.observed_monotonic_ms)
            self.receipts[fact.output.output_id] = DisplayReceipt(
                surface,
                observed.grant_id,
                fact.buffer_id,
                observed.frame_tag,
                max(0, boottime_ms() - age),
            )
            self._fact(fact)
        elif isinstance(observed, DiagnosticRelease):
            self.completed[observed.surface.output.output_id] = (observed.handoff_id, boottime_ms())
        elif isinstance(observed, RoleRemoval):
            self.completed[observed.surface.output.output_id] = (observed.decision_id, boottime_ms())
        elif isinstance(observed, SurfaceFact):
            self._fact(observed)
            self.receipts.pop(observed.output.output_id, None)
            self.completed.pop(observed.output.output_id, None)
        # Identity changes fence cached evidence even if no app was ever present.
        current = {state.key.output_id: state.key for state in self.controller.host.states()}
        for output, receipt in tuple(self.receipts.items()):
            if current.get(output) != receipt.surface.output:
                self.receipts.pop(output, None)
                self.completed.pop(output, None)

    def _fact(self, fact: SurfaceFact) -> None:
        # Keep only the newest tuple per Output; old tuples are retained by Central.
        for key, old in tuple(self.facts.items()):
            if old.output.output_id == fact.output.output_id:
                self.facts.pop(key)
        self.facts[fact_key(fact)] = fact

    def tick(self) -> None:
        if self.failure is not None:
            raise RuntimeError("display_network_worker_failed") from self.failure
        for _ in range(16):
            try:
                request, decision, session_id = self.responses.get_nowait()
            except queue.Empty:
                break
            if session_id == self.session_id:
                self._apply(request, decision)
        now = boottime_ms()
        if now < self.next_sample:
            return
        states = self.controller.host.states()
        # Cold/handoff transitions poll faster. Stable operational frames need no command heartbeat.
        transitional = any(state.connected and state.diagnostic != "released" for state in states)
        self.next_sample = now + (
            250 if self.controller.backend.trials else (1000 if transitional else 3000)
        )
        sample = Sample(
            states,
            tuple(self.receipts.values()),
            tuple(self.completed.items()),
            tuple(self.facts.values()),
            now,
        )
        try:
            self.requests.put_nowait(sample)
        except queue.Full:
            pass  # One in flight and one pending; native dispatch never blocks on network.

    def _apply(self, request, decision) -> None:
        if (
            decision.producer != request.producer
            or decision.request_id != request.request_id
            or decision.output != request.output
            or boottime_ms() >= decision.expires_boottime_ms
        ):
            return
        if request.completed_decision_id is not None:
            completed = self.completed.get(decision.output.output_id)
            if completed is not None and completed[0] == request.completed_decision_id:
                self.completed.pop(decision.output.output_id, None)
        state = self.controller.host.state(decision.output.output_id)
        if state.key != decision.output or not state.connected:
            return
        surface = decision.surface
        try:
            previous_trial = self.controller.backend.trials.get(state.key.output_id)
            self.controller.backend.trial(state.key, decision.trial)
            current_trial = self.controller.backend.trials.get(state.key.output_id)
            if current_trial is not None and (previous_trial is None or
                    (previous_trial.trial_id, previous_trial.sequence) !=
                    (current_trial.trial_id, current_trial.sequence)):
                # A just-delivered Trial must not wait out the prior idle cadence
                # before reporting its first real presentation.
                self.next_sample = min(self.next_sample, boottime_ms() + 100)
            if (
                decision.operation == "candidate"
                and state.candidate is None
                and state.admitted is None
            ):
                grant = SurfaceGrant(surface, 10004, True)
                self.controller.backend.allow(grant)
                self.controller.host.offer(surface)
            elif decision.operation == "handoff" and state.admitted is None:
                receipt = self.receipts.get(state.key.output_id)
                authorized = decision.receipt
                if (receipt is None or authorized is None or receipt.surface != authorized.surface
                        or receipt.grant_id != authorized.grant_id
                        or boottime_ms() - authorized.sampled_boottime_ms >= 5000):
                    return
                self.controller.host.authorize_handoff(
                    surface,
                    now_ms=time.monotonic_ns() // 1_000_000,
                    handoff_id=decision.decision_id,
                    buffer_token=authorized.buffer_id,
                )
            elif decision.operation == "withdraw":
                if surface not in (state.admitted, state.candidate):
                    return
                self.controller.backend.withdraw(surface, decision.decision_id)
            elif decision.operation == "revision":
                # The dedicated native revision path is added alongside the same-process
                # adoption domain; never approximate it with diagnostic withdrawal.
                self.controller.revise(decision)
        except (KeyError, ValueError):
            # Stale process/configuration/buffer: the next sample requests fresh authority.
            return

    def _work(self, central: str, serial: str, offer_id: UUID, runtime: Path) -> None:
        store = None
        try:
            host = self.controller.host
            directory = runtime / ("session-" + host.incarnation_id.hex)
            directory.mkdir(mode=0o700, exist_ok=True)
            store = BootStore(
                directory,
                boot_id=host.boot_id,
                owner_uid=os.getuid(),
                policy={
                    "owner": "display_host",
                    "incarnation_id": str(host.incarnation_id),
                    "offer_id": str(offer_id),
                    "serial": serial,
                },
            )
            transport = NodeHTTP(central)
            session = NodeSession(
                store,
                transport,
                owner="display_host",
                serial=serial,
                offer_id=offer_id,
                kernel_boot_id=host.boot_id,
                incarnation_id=host.incarnation_id,
            )
            while not self.stop.is_set():
                try:
                    sample = self.requests.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    grant = session.ensure()
                    if grant is None:
                        continue
                    self.session_id = grant.session_id
                    receipts, completed = (
                        {r.surface.output.output_id: r for r in sample.receipts},
                        dict(sample.completed),
                    )
                    for state in sample.states:
                        request = DisplayExchange(
                            grant.producer,
                            uuid4(),
                            sample.sampled_boottime_ms,
                            state.key,
                            state.connected,
                            state.admitted if state.diagnostic == "released" else None,
                            state.candidate,
                            receipts.get(state.key.output_id),
                            completed.get(state.key.output_id, (None, None))[0],
                            completed.get(state.key.output_id, (None, None))[1],
                        )
                        status, raw = session.request(
                            "POST",
                            "/v2/node/display",
                            encode_display_exchange(request),
                        )
                        if status == 200:
                            decision = parse_display_decision(raw)
                            try:
                                self.responses.put_nowait((request, decision, grant.session_id))
                            except queue.Full:
                                pass  # A skipped decision confers no local authority.
                    if sample.facts:
                        # Snapshot-only stream is explicitly incomplete; no event continuity invented.
                        evidence = NodeSnapshotV2(
                            grant.producer,
                            uuid4(),
                            0,
                            sample.sampled_boottime_ms,
                            sample.facts,
                            stream_gap=True,
                        )
                        session.request(
                            "POST",
                            "/v2/node/evidence",
                            encode_node_message(evidence),
                        )
                except (OSError, ValueError, http.client.HTTPException):
                    if store.failed:
                        raise
                    # Fixed main-loop cadence bounds retry; no credential or packet logging.
        except BaseException as exc:
            self.failure = exc
        finally:
            if store is not None:
                store.close()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=6)
