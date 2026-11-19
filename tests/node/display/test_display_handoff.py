"""The handoff on the Pi (decision 0034, S4b part 4): the rule (`wanted_surface`, `next_step`) over
every state it meets, and `Handoffs` driving a real `DisplayHost` over a fake shell, through the
controller's `app_run` op and the main loop's pass (`runner.settle`).

Mutation probes: 4.4 `next_step` returns None for a held surface that is not wanted -> the table
and the rebind test fail; 4.5 put each Output and trial report on every pass -> the report-count
test fails."""
from __future__ import annotations

import sys
from dataclasses import replace
from itertools import product
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from appliance.display_host import runner
from appliance.display_host.domain import DisplayHost, OutputState
from appliance.display_host.handoff import (
    RETRY_MS,
    REVISION_TTL_MS,
    AppRun,
    Handoff,
    Handoffs,
    Offer,
    Revise,
    Withdraw,
    next_step,
    wanted_surface,
)
from appliance.display_host.runner import Controller, OutputReporter
from appliance.display_host.trials import Trials
from appliance.display_host.weston import SurfaceGrant
from appliance.kernel.app_facts import APP_RUN_OP, PLAYER_UID
from contracts.node_display import Surface
from contracts.node_output import OutputBinding
from contracts.node_protocol import NodeProcessIdentity, OutputKey

BOOT, INCARNATION = uuid4(), uuid4()
KEY = OutputKey(BOOT, INCARNATION, "HDMI-A-1", 2, 2)
RUN = AppRun(NodeProcessIdentity(396, 31751781, uuid4()), 1)
OTHER_RUN = AppRun(NodeProcessIdentity(512, 31760000, uuid4()), 2)
BOUND = OutputBinding("kitchen", 3, 5)


def surface(run=RUN, binding=BOUND, key=KEY) -> Surface:
    return Surface(key, run.process, run.app_epoch, binding.generation, binding.configuration_revision,
                   binding.frame_id)


WANTED = surface()


# -- wanted_surface -----------------------------------------------------------------------------

@pytest.mark.parametrize(("connected", "binding", "run"), list(product((True, False), (BOUND, None), (RUN, None))))
def test_the_wanted_surface_needs_a_connected_bound_output_and_a_run(connected, binding, run):
    state = OutputState(KEY, connected=connected)
    expected = WANTED if (connected and binding is not None and run is not None) else None
    assert wanted_surface(state, binding, run) == expected


def test_the_wanted_surface_is_the_runs_process_and_the_bound_frame():
    wanted = wanted_surface(OutputState(KEY), OutputBinding("den", 7, 9), OTHER_RUN)
    assert (wanted.output, wanted.process, wanted.app_epoch, wanted.binding_generation,
            wanted.config_revision, wanted.frame_id) == (KEY, OTHER_RUN.process, 2, 7, 9, "den")


# -- next_step: the table -----------------------------------------------------------------------

REVISED = surface(binding=replace(BOUND, configuration_revision=6))
ANOTHER_RUN = surface(run=OTHER_RUN)
ANOTHER_FRAME = surface(binding=OutputBinding("den", 4, 1))
WANTS = {"none": None, "equal": WANTED, "another_run": ANOTHER_RUN, "another_frame": ANOTHER_FRAME,
         "higher_revision": REVISED}

HELD = {
    "nothing": OutputState(KEY),
    "candidate": OutputState(KEY, candidate=WANTED),
    "candidate_presented": OutputState(KEY, candidate=WANTED, presented=WANTED, buffer_token="b1"),
    "candidate_presented_no_token": OutputState(KEY, candidate=WANTED, presented=WANTED),
    "release_requested": OutputState(KEY, candidate=WANTED, presented=WANTED, buffer_token="b1",
                                     admitted=WANTED, diagnostic="release_requested", handoff_id=uuid4()),
    "admitted": OutputState(KEY, candidate=WANTED, admitted=WANTED, diagnostic="released"),
    "revision_pending": OutputState(KEY, candidate=WANTED, admitted=WANTED, diagnostic="released",
                                    revision=REVISED),
}


def expected_step(held: str, want: str, withdrawing: bool):
    """The rule restated per case, independently of next_step's code."""
    state, wanted = HELD[held], WANTS[want]
    holding = state.admitted or state.candidate
    if holding is not None and holding != wanted:
        if want == "higher_revision" and state.admitted is not None:
            return Revise(REVISED) if held == "admitted" else None
        return None if withdrawing else Withdraw(holding)
    if held == "candidate_presented":
        return Handoff(WANTED, "b1")
    if held == "nothing" and wanted is not None:
        return Offer(wanted)
    return None


@pytest.mark.parametrize(("held", "want", "withdrawing"), list(product(HELD, WANTS, (False, True))))
def test_next_step_over_every_held_state_and_wanted_surface(held, want, withdrawing):
    assert next_step(HELD[held], WANTS[want], withdrawing=withdrawing) == expected_step(held, want, withdrawing)


@pytest.mark.parametrize(("held", "want"), list(product(HELD, WANTS)))
def test_a_disconnected_output_takes_no_step(held, want):
    state = replace(HELD[held], connected=False, diagnostic="unknown")
    assert next_step(state, WANTS[want], withdrawing=False) is None


def test_a_rebind_withdraws_the_admitted_frame_then_offers_the_new_one():
    """Probe 4.4: the held surface that is not wanted is withdrawn, never kept."""
    assert next_step(HELD["admitted"], ANOTHER_FRAME, withdrawing=False) == Withdraw(WANTED)
    assert next_step(OutputState(KEY), ANOTHER_FRAME, withdrawing=False) == Offer(ANOTHER_FRAME)


# -- Handoffs on a real DisplayHost -------------------------------------------------------------

class Shell:
    """The shell as WestonBackend carries it, faked: each op recorded; the shell's answers (a
    presentation, the handoff's completion, an invalidation) are the tests' to send."""

    def __init__(self) -> None:
        self.grants: dict[str, tuple[SurfaceGrant, UUID]] = {}
        self.withdrawals: dict[str, tuple[Surface, UUID]] = {}
        self.trials: dict = {}
        self.pending: list = []
        self.ops: list[tuple] = []
        self.verified = True
        self.host = DisplayHost(boot_id=BOOT, incarnation_id=INCARNATION, backend=self)

    # DisplayHost's backend
    def diagnostic(self, key, reason) -> None:
        self.ops.append(("diagnostic", key.output_id))
        self.grants.pop(key.output_id, None)

    def candidate(self, surface) -> None:
        self.ops.append(("candidate", surface))

    def release_diagnostic(self, surface, buffer_token, handoff_id) -> None:
        self.ops.append(("handoff", surface, buffer_token))

    # the handoff's backend
    def allow(self, grant: SurfaceGrant) -> UUID:
        if not self.verified:
            raise ValueError("display_process_not_verified")
        if grant.surface.output.output_id in self.grants:
            raise ValueError("display_authorized_withdrawal_required")
        self.ops.append(("allow", grant))
        grant_id = uuid4()
        self.grants[grant.surface.output.output_id] = (grant, grant_id)
        return grant_id

    def revise(self, surface, decision_id, *, ttl_ms) -> None:
        self.ops.append(("revise", surface, ttl_ms))
        self.host.stage_revision(surface)

    def withdraw(self, surface, decision_id) -> None:
        self.ops.append(("withdraw", surface))
        self.withdrawals[surface.output.output_id] = (surface, decision_id)

    # the controller's
    def initialize(self) -> DisplayHost:
        return self.host

    def dispatch(self):
        return None

    # the shell's answers
    def present(self, surface: Surface, token: str = "b1") -> None:
        self.host.presented(surface, buffer_token=token, now_ms=1)

    def released(self, surface: Surface) -> None:
        self.host.diagnostic_released(surface, handoff_id=self.host.state(surface.output.output_id).handoff_id)

    def removed(self, surface: Surface) -> None:
        """The shell's answer to a withdrawal: `invalidated`, then `role_removed`."""
        self.host.backend_invalidated(surface.output, reason="authorized_withdrawal")
        self.grants.pop(surface.output.output_id, None)
        self.withdrawals.pop(surface.output.output_id, None)


@pytest.fixture
def shell():
    shell = Shell()
    shell.host.observe_output(KEY, connected=True)
    return shell


def steps(shell: Shell) -> list[str]:
    taken = [op[0] for op in shell.ops if op[0] != "diagnostic"]
    shell.ops.clear()
    return taken


def handed_off(shell: Shell, handoffs: Handoffs, wanted: Surface = WANTED) -> None:
    handoffs.settle(shell.host)
    assert steps(shell) == ["allow", "candidate"]
    shell.present(wanted)
    handoffs.settle(shell.host)
    assert steps(shell) == ["handoff"]
    shell.released(wanted)
    assert shell.host.state("HDMI-A-1").admitted == wanted


def test_a_binding_and_a_run_offer_then_a_presentation_hands_off(shell):
    handoffs = Handoffs(shell)
    handoffs.document("HDMI-A-1", BOUND)
    handoffs.run(RUN)
    handoffs.settle(shell.host)
    grant = next(op[1] for op in shell.ops if op[0] == "allow")
    assert (grant.surface, grant.uid, grant.starting_new) == (WANTED, PLAYER_UID, True)
    assert shell.host.state("HDMI-A-1").candidate == WANTED
    steps(shell)
    for _ in range(3):                                   # awaiting the compositor: nothing more
        handoffs.settle(shell.host)
    assert steps(shell) == []
    shell.present(WANTED)
    handoffs.settle(shell.host)
    assert shell.ops == [("handoff", WANTED, "b1")]
    shell.ops.clear()
    shell.released(WANTED)
    for _ in range(3):                                   # admitted: stable, nothing asked
        handoffs.settle(shell.host)
    assert steps(shell) == []


def test_no_run_or_no_binding_does_nothing(shell):
    handoffs = Handoffs(shell)
    handoffs.document("HDMI-A-1", BOUND)
    handoffs.settle(shell.host)
    handoffs.document("HDMI-A-1", None)
    handoffs.run(RUN)
    handoffs.settle(shell.host)
    assert steps(shell) == [] and shell.host.state("HDMI-A-1").candidate is None


def test_an_unbinding_document_withdraws(shell):
    handoffs = Handoffs(shell)
    handoffs.document("HDMI-A-1", BOUND)
    handoffs.run(RUN)
    handed_off(shell, handoffs)
    handoffs.document("HDMI-A-1", None)
    handoffs.settle(shell.host)
    assert shell.ops == [("withdraw", WANTED)]
    shell.ops.clear()
    handoffs.settle(shell.host)                          # withdrawing: not asked twice
    assert steps(shell) == []
    shell.removed(WANTED)
    handoffs.settle(shell.host)
    assert steps(shell) == [] and shell.host.state("HDMI-A-1").admitted is None


def test_a_new_run_withdraws_then_offers(shell):
    handoffs = Handoffs(shell)
    handoffs.document("HDMI-A-1", BOUND)
    handoffs.run(RUN)
    handed_off(shell, handoffs)
    handoffs.run(OTHER_RUN)
    handoffs.settle(shell.host)
    assert steps(shell) == ["withdraw"]
    shell.removed(WANTED)
    handed_off(shell, handoffs, ANOTHER_RUN)


def test_a_rebind_withdraws_then_hands_the_new_frame(shell):
    handoffs = Handoffs(shell)
    handoffs.document("HDMI-A-1", BOUND)
    handoffs.run(RUN)
    handed_off(shell, handoffs)
    handoffs.document("HDMI-A-1", OutputBinding("den", 4, 1))
    handoffs.settle(shell.host)
    assert shell.ops == [("withdraw", WANTED)]
    shell.ops.clear()
    shell.removed(WANTED)
    handed_off(shell, handoffs, ANOTHER_FRAME)


def test_a_position_save_revises_the_admitted_surface_within_the_shells_window(shell):
    handoffs = Handoffs(shell)
    handoffs.document("HDMI-A-1", BOUND)
    handoffs.run(RUN)
    handed_off(shell, handoffs)
    handoffs.document("HDMI-A-1", replace(BOUND, configuration_revision=6))
    handoffs.settle(shell.host)
    assert shell.ops == [("revise", REVISED, REVISION_TTL_MS)]
    shell.ops.clear()
    handoffs.settle(shell.host)                          # pending: not asked again
    assert steps(shell) == []
    shell.host.revision_adopted(REVISED)
    handoffs.settle(shell.host)
    assert steps(shell) == [] and shell.host.state("HDMI-A-1").admitted == REVISED


def test_a_refused_step_is_logged_once_and_tried_again_after_the_retry(shell, caplog):
    now = {"ms": 0}
    handoffs = Handoffs(shell, clock=lambda: now["ms"])
    handoffs.document("HDMI-A-1", BOUND)
    handoffs.run(RUN)
    shell.verified = False                               # the run is not yet the unit's MainPID
    calls = []
    real = shell.allow
    shell.allow = lambda grant: (calls.append(grant), real(grant))[1]
    for _ in range(5):
        handoffs.settle(shell.host)
    assert len(calls) == 1
    now["ms"] = RETRY_MS
    handoffs.settle(shell.host)
    assert len(calls) == 2
    assert sum("refused" in record.getMessage() for record in caplog.records) == 1
    shell.verified = True
    now["ms"] = 2 * RETRY_MS
    handoffs.settle(shell.host)
    assert shell.host.state("HDMI-A-1").candidate == WANTED


# -- through the controller -----------------------------------------------------------------------

class Cards:
    def __init__(self) -> None:
        self.told: list = []

    def admitted(self, output_id, grant, frame, *, trial) -> None:
        self.told.append((output_id, grant, frame, trial))


class Sink:
    def __init__(self) -> None:
        self.puts: list = []

    def put_report(self, report) -> None:
        self.puts.append(report)

    def put_trial(self, report) -> None:
        self.puts.append(report)

    def emit_attempt(self, attempt) -> None:
        raise AssertionError("no power attempt here")


def test_the_runners_app_run_op_and_its_pass_hand_the_output_off(shell):
    controller = Controller(shell)
    controller.handoffs.document("HDMI-A-1", BOUND)
    run = {"invocation_id": str(RUN.process.invocation_id), "pid": RUN.process.pid,
           "start_ticks": RUN.process.start_ticks, "app_epoch": RUN.app_epoch}
    wiring = SimpleNamespace(cards=Cards())
    runner.settle(controller, shell, wiring)
    assert steps(shell) == []                            # no run told yet: nothing offered
    assert controller.receive({"op": APP_RUN_OP, "run": run}) == {}
    runner.settle(controller, shell, wiring)
    assert steps(shell) == ["allow", "candidate"]
    shell.present(WANTED)
    runner.settle(controller, shell, wiring)
    assert steps(shell) == ["handoff"]


def test_the_ingress_takes_no_grant_handoff_or_withdrawal_op(shell):
    controller = Controller(shell)
    for operation in ("candidate", "handoff", "withdraw", "revision"):
        with pytest.raises(ValueError, match="^display_operation_unsupported$"):
            controller.receive({"op": operation, "surface": {}, "output_id": "HDMI-A-1"})


def test_a_stable_output_publishes_nothing_new_on_its_line(shell):
    """Probe 4.5: 50 passes over a handed-off Output put no Output or trial report after the first."""
    reports, trial_reports = Sink(), Sink()
    controller = Controller(shell, reports=OutputReporter(reports, read=lambda output_id: None))
    controller.trials = Trials(shell, trial_reports, clock=lambda: 0)
    controller.handoffs.document("HDMI-A-1", BOUND)
    controller.handoffs.run(RUN)
    wiring = SimpleNamespace(cards=Cards())
    runner.settle(controller, shell, wiring)
    shell.present(WANTED)
    runner.settle(controller, shell, wiring)
    shell.released(WANTED)
    runner.settle(controller, shell, wiring)
    before = (len(reports.puts), len(trial_reports.puts))
    assert before == (1, 1)
    for _ in range(50):
        runner.settle(controller, shell, wiring)
        controller.observe()
    assert (len(reports.puts), len(trial_reports.puts)) == before


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="the ingress is SOCK_SEQPACKET with SO_PASSCRED (Linux)")
def test_the_brokers_run_told_over_the_ingress_is_what_the_output_is_handed_to(shell):
    """Probe 4.2: the Player's run comes from the broker. `DisplayRuns.tell` over a real ingress
    socket, answered by the controller's own reader (`answer_ingress`), is the only source of the
    run: without it nothing is offered."""
    import os
    import socket
    import threading
    from pathlib import Path

    from appliance.apps.display_runs import DisplayRuns
    from appliance.apps.probe import AppRunKey
    from appliance.display_host.runner import answer_ingress

    controller = Controller(shell)
    controller.handoffs.document("HDMI-A-1", BOUND)
    wiring = SimpleNamespace(cards=Cards())
    runner.settle(controller, shell, wiring)
    assert steps(shell) == []
    directory = Path(f"/tmp/pw-handoff-{uuid4().hex[:8]}")
    directory.mkdir()
    path = directory / "ingress.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            listener.bind(str(path))
            listener.listen(1)

            def serve() -> None:
                connection, _ = listener.accept()
                with connection:
                    answer_ingress(connection, controller, root=os.getuid())

            serving = threading.Thread(target=serve, daemon=True)
            serving.start()
            key = AppRunKey(RUN.process.invocation_id, RUN.process.pid, RUN.process.start_ticks, RUN.app_epoch)
            assert DisplayRuns(path).tell(key) is True
            serving.join(5)
    finally:
        path.unlink(missing_ok=True)
        directory.rmdir()
    runner.settle(controller, shell, wiring)
    assert steps(shell) == ["allow", "candidate"]
    assert shell.host.state("HDMI-A-1").candidate == WANTED


def test_the_outputs_op_says_what_each_output_is_held_for_and_which_input_is_missing(shell):
    """Root's `outputs` op on the ingress answers each Output's state and the handoff's two inputs,
    so an Output left unhanded says why: no run told, no binding, or offered and awaiting the
    Player. tests/node_pid1_display_probe.py `handed_to` reads it, as the real-PID1 scenarios do.
    Mutation probe: `outputs` answers without "handoff" -> KeyError here and in the scenarios."""
    import json

    from node_pid1_display_probe import handed_to, held

    controller = Controller(shell)
    wiring = SimpleNamespace(cards=Cards())

    def answer() -> dict:   # as answer_ingress puts it on the wire
        return json.loads(json.dumps({"accepted": True, **controller.receive({"op": "outputs"})},
                                     default=str))

    pid, invocation = RUN.process.pid, str(RUN.process.invocation_id)
    first = answer()
    assert first["handoff"] == {"run": None, "bindings": {}}
    assert held(first, "HDMI-A-1") is None
    controller.handoffs.document("HDMI-A-1", BOUND)
    runner.settle(controller, shell, wiring)
    assert answer()["handoff"] == {"run": None, "bindings": {"HDMI-A-1": {
        "frame_id": "kitchen", "generation": 3, "configuration_revision": 5}}}
    assert held(answer(), "HDMI-A-1") is None          # bound, but no run: nothing offered
    controller.receive({"op": APP_RUN_OP, "run": {
        "invocation_id": invocation, "pid": pid, "start_ticks": RUN.process.start_ticks, "app_epoch": 1}})
    runner.settle(controller, shell, wiring)
    offered = answer()
    assert offered["handoff"]["run"] == {"invocation_id": invocation, "pid": pid,
                                         "start_ticks": RUN.process.start_ticks, "app_epoch": 1}
    assert held(offered, "HDMI-A-1")["frame_id"] == "kitchen"
    assert handed_to(offered, "HDMI-A-1", pid=pid, invocation_id=invocation.replace("-", ""), frame_id="kitchen")
    assert not handed_to(offered, "HDMI-A-1", pid=pid, invocation_id=invocation, frame_id="den")
    assert not handed_to(offered, "HDMI-A-1", pid=OTHER_RUN.process.pid,
                         invocation_id=str(OTHER_RUN.process.invocation_id), frame_id="kitchen")
    with pytest.raises(AssertionError):
        held(offered, "HDMI-A-2")


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="the ingress is SOCK_SEQPACKET with SO_PASSCRED (Linux)")
def test_the_scenarios_probe_reads_the_offer_over_a_real_ingress(shell):
    """The real-PID1 scenarios' own probe (`DISPLAY_OUTPUTS`, a separate process) over a real
    ingress socket answered by `answer_ingress`: it reads the offer the broker's run and the
    binding made."""
    import json
    import os
    import socket
    import subprocess
    import threading
    from pathlib import Path

    from node_pid1_display_probe import DISPLAY_OUTPUTS, handed_to

    from appliance.display_host.runner import answer_ingress

    controller = Controller(shell)
    controller.handoffs.document("HDMI-A-1", BOUND)
    controller.handoffs.run(RUN)
    runner.settle(controller, shell, SimpleNamespace(cards=Cards()))
    directory = Path(f"/tmp/pw-outputs-{uuid4().hex[:8]}")
    directory.mkdir()
    path = directory / "ingress.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            listener.bind(str(path))
            listener.listen(1)

            def serve() -> None:
                connection, _ = listener.accept()
                with connection:
                    answer_ingress(connection, controller, root=os.getuid())

            serving = threading.Thread(target=serve, daemon=True)
            serving.start()
            printed = subprocess.run([sys.executable, "-I", "-c", DISPLAY_OUTPUTS, str(path)],
                                     check=True, capture_output=True, text=True, timeout=10).stdout
            serving.join(5)
    finally:
        path.unlink(missing_ok=True)
        directory.rmdir()
    assert handed_to(json.loads(printed), "HDMI-A-1", pid=RUN.process.pid,
                     invocation_id=str(RUN.process.invocation_id), frame_id="kitchen")
