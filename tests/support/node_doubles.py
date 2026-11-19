"""Recording and in-memory adapters for the Node's domain services in unit tests: Display Host's
backend and the App Effect Broker's effect journal and app driver. No network, systemd, reboot or display effect."""
from __future__ import annotations

from uuid import UUID


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
    def __init__(self, clock=lambda: 0):
        self.running = None
        self.selected = None
        self.calls = []
        self.display = "weston-1"  # the running display incarnation (None: down)
        self.launch = None  # the last start's Launch (epoch, display, started_ms)
        self.collected = True  # PID1 unloaded the old unit
        self.clock = clock  # boottime ms, read at each start
        self.purposeful = None  # Display's mark (a PurposefulRestart), None while absent

    def display_incarnation(self):
        return self.display

    def launched(self):
        return self.launch

    def unit_collected(self):
        return self.running is None and self.collected

    def purposeful_restart(self):
        return self.purposeful

    def current(self):
        return self.running

    def verify(self, environment):
        self.calls.append("verify_fixture_environment")
        return True  # Fixture identity only; no Debian closure exists in M1.

    def select(self, environment):
        self.calls.append("select_fixture_environment")
        self.selected = environment

    def start(self, environment, operation_id):
        from appliance.apps.broker import Launch, RunningApp
        from contracts.node_protocol import NodeProcessIdentity

        self.calls.append("start_simulated_process")
        # Like the systemd driver, the launch is recorded before the spawn.
        epoch = self.launch.epoch + 1 if self.launch else 1
        self.launch = Launch(epoch, self.display, self.clock())
        self.running = RunningApp(
            environment, NodeProcessIdentity(99 + epoch, 49 + epoch, UUID(int=5 + epoch)), epoch,
            operation_id,
        )
        return self.running
