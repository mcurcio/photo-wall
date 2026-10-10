#!/usr/bin/env python3
"""Opt-in M1 software simulator; no network, systemd, reboot, or display effects.

The real node domain services are composed with recording adapters. Explicitly
injected presentation callbacks are simulator evidence, never measurements from
a compositor or panel. This scenario does not configure production trust.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from central.fleet.node_evidence import (  # noqa: E402
    EvidenceProjection,
    evidence_identity,
    reconcile_evidence,
)
from contracts.node_protocol import (  # noqa: E402
    NodeEventV2,
    NodeProducerV2,
    NodeSnapshotV2,
    encode_node_message,
    parse_node_message,
)


def json_value(value):
    """Trace serialization only; validation stays in the shared contracts."""
    if is_dataclass(value):
        return {key: json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


class SimulatedEvidenceInbox:
    """Single-threaded in-memory simulation of storage and reducer composition.

    Production must replace this with atomic durable uniqueness plus projection
    transactions. Admission is explicit construction, not first-message wins.
    """

    def __init__(self, producers: tuple[NodeProducerV2, ...]):
        self.projections = {producer: EvidenceProjection(producer) for producer in producers}
        self.messages = {}
        self.sequences = {}

    def receive(self, message: NodeEventV2 | NodeSnapshotV2, *, received_at: float):
        message = parse_node_message(encode_node_message(message))
        identity = evidence_identity(message)
        if message.producer not in self.projections:
            raise ValueError("simulator_producer_not_admitted")
        if isinstance(message, NodeEventV2):
            sequence_key = (message.producer, message.sequence)
            prior = self.sequences.get(sequence_key)
            if prior is not None and prior != message:
                raise ValueError("conflicting_simulator_sequence")
        result = reconcile_evidence(
            self.projections[message.producer], message,
            received_at=received_at, previous=self.messages.get(identity),
        )
        self.messages[identity] = message
        if isinstance(message, NodeEventV2):
            self.sequences[(message.producer, message.sequence)] = message
        self.projections[message.producer] = result.projection
        return result


class RecordingDisplayBackend:
    def __init__(self):
        self.requests = []

    def diagnostic(self, key, reason):
        self.requests.append({"action": "diagnostic", "output": key, "reason": reason})

    def candidate(self, surface):
        self.requests.append({"action": "candidate", "surface": surface})

    def release_diagnostic(self, surface, buffer_token, handoff_id):
        self.requests.append({"action": "release_diagnostic", "surface": surface,
                              "buffer_token": buffer_token, "handoff_id": handoff_id})


class MemoryRebootJournal:
    def __init__(self):
        self.records = {}
        self.sequence = 0

    def get(self, command_id):
        return self.records.get(command_id)

    def put(self, record):
        self.records[record.request.command_id] = record

    def next_sequence(self):
        self.sequence += 1
        return self.sequence


class RecordingRebootDriver:
    def __init__(self):
        self.calls = 0

    def initiate(self):
        self.calls += 1
        return True  # Simulator-observed initiation, not a real reboot.


class SimulatedHostSampler:
    def sample(self):
        return (("simulated_memory_available", 1024.0, "MiB"),)


class MemoryEffectJournal:
    def __init__(self):
        self.records = {}
        self.latest = None

    def get(self, operation_id):
        return self.records.get(operation_id)

    def current(self):
        return self.latest

    def put(self, record):
        self.records[record.command.operation_id] = record
        self.latest = record


class RecordingAppDriver:
    def __init__(self):
        self.running = None
        self.selected = None
        self.calls = []
        self.display = "weston-1"  # the running display incarnation (None: down)
        self.launch_display = None  # the incarnation the last start recorded
        self.collected = True  # PID1 unloaded the old unit

    def display_incarnation(self):
        return self.display

    def launched_display(self):
        return self.launch_display

    def unit_collected(self):
        return self.running is None and self.collected


    def current(self):
        return self.running

    def verify(self, environment):
        self.calls.append("verify_fixture_environment")
        return True  # Fixture identity only; no Debian closure exists in M1.

    def select(self, environment):
        self.calls.append("select_fixture_environment")
        self.selected = environment

    def start(self, environment, operation_id):
        from appliance.apps.broker import RunningApp
        from contracts.node_protocol import NodeProcessIdentity

        self.calls.append("start_simulated_process")
        self.launch_display = self.display
        self.running = RunningApp(
            environment, NodeProcessIdentity(100, 50, UUID(int=6)), 1, operation_id,
        )
        return self.running


class SimulatedPreparer:
    def __init__(self):
        self.succeeds = False
        self.calls = []

    def prepare(self, environment):
        self.calls.append(environment.environment_sha256)
        return self.succeeds


def run_demo() -> dict:
    """Return a trace of real domain transitions driven by simulator adapters."""
    from appliance.apps.broker import AppEffectBroker, ColdStart
    from appliance.display_host.domain import DisplayHost, Surface
    from appliance.host.host import HostCore, RebootRequest
    from appliance.node.manager import AppManager
    from contracts.app_environment import AppEnvironmentRefV2
    from contracts.node_protocol import AppProcessFact

    boot, offer, session = UUID(int=1), UUID(int=2), UUID(int=3)
    host_producer = NodeProducerV2("simulator", "device-" + "a" * 64, 1,
                                   boot, "host_core", UUID(int=4))
    display_producer = NodeProducerV2("simulator", "device-" + "a" * 64, 1,
                                      boot, "display_host", UUID(int=5))
    broker_producer = NodeProducerV2("simulator", "device-" + "a" * 64, 1,
                                     boot, "app_effect_broker", UUID(int=7))
    inbox = SimulatedEvidenceInbox((host_producer, display_producer, broker_producer))
    display_backend = RecordingDisplayBackend()
    display = DisplayHost(boot_id=boot, incarnation_id=display_producer.incarnation_id,
                          backend=display_backend)
    reboot_driver = RecordingRebootDriver()
    host = HostCore(producer=host_producer, session_id=session, offer_id=offer,
                    journal=MemoryRebootJournal(), driver=reboot_driver)
    sampler = SimulatedHostSampler()
    trace = []

    def record(step, **details):
        trace.append({"step": step, "source": "simulator", **json_value(details)})

    record("boot_without_app", display=display.connect("HDMI-A-1"),
           host=host.observe(sampler, now_ms=0))
    display.diagnostic_presented(display.state("HDMI-A-1").key)
    record("injected_diagnostic_presentation", display=display.state("HDMI-A-1"))
    environment = AppEnvironmentRefV2(
        "b" * 64, 1024, "c" * 64, "photo-wall-player", "1.0", "arm64",
        "d" * 64, "e" * 64, "/usr/bin/photo-wall-player", "fixture-base-v2",
        "fixture-graphics-v1", "fixture-plugins-v1",
    )
    app_driver = RecordingAppDriver()
    broker = AppEffectBroker(boot_id=boot, offer_id=offer, authorized_environment=environment,
                             journal=MemoryEffectJournal(), driver=app_driver)
    cold = broker.cold_start(ColdStart(UUID(int=8), boot, offer, environment))
    running = cold.running
    if running is None:
        raise RuntimeError("simulator_cold_start_has_no_process")
    process_event = NodeEventV2(
        broker_producer, UUID(int=9), 1, 100,
        (AppProcessFact(running.process, running.app_epoch,
                        environment.environment_sha256, "running"),),
    )
    record("exact_cold_start", operation=cold,
           central=inbox.receive(process_event, received_at=1.0))
    surface = Surface(display.state("HDMI-A-1").key, running.process,
                      running.app_epoch, 1, 0, "demo-frame")
    record("candidate_requested", display=display.offer(surface))
    presented = display.presented(surface, buffer_token="simulated-buffer-1", now_ms=200)
    handoff = display.authorize_handoff(surface, now_ms=200)
    display.diagnostic_released(surface, handoff_id=handoff.handoff_id)
    presentation_event = NodeEventV2(
        display_producer, UUID(int=10), 1, 200, (presented,),
    )
    record("injected_compositor_presentation", display=display.state("HDMI-A-1"),
           central=inbox.receive(presentation_event, received_at=2.0))
    preparer = SimulatedPreparer()
    manager = AppManager(preparer)
    record("background_staging_failed", preparation=manager.prepare(UUID(int=11), environment),
           display=display.state("HDMI-A-1"), process=app_driver.current())
    preparer.succeeds = True
    manager.prepare(UUID(int=12), environment)
    record("background_staging_superseded", preparation=manager.supersede(UUID(int=12)),
           display=display.state("HDMI-A-1"), process=app_driver.current())
    # A transport failure carries no display invalidation or process-exit fact.
    record("control_transport_lost", transport="disconnected",
           display=display.state("HDMI-A-1"), process=app_driver.current(),
           host=host.observe(sampler, now_ms=300))
    app_driver.running = None
    invalidations = display.process_exited(running.process)
    invalidation_event = NodeEventV2(display_producer, UUID(int=13), 2, 400, invalidations)
    snapshot = NodeSnapshotV2(display_producer, UUID(int=14), 2, 400, invalidations)
    record("process_exit_snapshot_arrives_first", display=display.state("HDMI-A-1"),
           broker=broker.reconcile(),
           central=inbox.receive(snapshot, received_at=4.0),
           host=host.observe(sampler, now_ms=400))
    record("delayed_invalidation_event", central=inbox.receive(
        invalidation_event, received_at=4.1))
    record("old_presentation_duplicate", central=inbox.receive(
        presentation_event, received_at=4.2))
    request = RebootRequest(UUID(int=15), "f" * 64, session, offer, host_producer)
    response = host.receive(request, now_ms=500)
    record("reboot_command_admitted", response=response,
           reboot_effect_calls=reboot_driver.calls)
    initiated = host.initiate(request.command_id, now_ms=501)
    if initiated.event is None:
        raise RuntimeError("simulator_reboot_initiation_missing")
    record("reboot_initiation_injected", operation=initiated,
           central=inbox.receive(initiated.event, received_at=5.1),
           reboot_effect_calls=reboot_driver.calls)
    return {
        "evidence_class": "simulated", "production_effects": False,
        "limitations": ["No physical boot or pixels", "No compositor adapter",
                        "No actual Debian environment verification or launch",
                        "No durable database/session/command ingress",
                        "No production credentials or reboot"],
        "trace": trace, "display_requests": json_value(display_backend.requests),
        "app_driver_calls": app_driver.calls,
        "final_projections": json_value(tuple(inbox.projections.values())),
    }


def main() -> int:
    try:
        result = run_demo()
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"evidence_class": "simulated", "status": "failed",
                          "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
