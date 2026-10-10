"""The health judge: node facts in, one sequenced verdict out. Pure (no I/O, no clock of its own).

The caller passes every fact with the judge's own observation time (`now_ms`, one node clock);
facts carry no time the judge compares. Rules for `app_unresponsive` (the one M1 code):

- `probe_unanswered` (or `probe_kill_due`) for a run opens the condition **pending**;
- pending becomes **raised** once it has stayed unanswered for the catalogue's raise window;
  a `probe_answered` while pending withdraws it (it was never shown);
- a `probe_answered` while raised starts the clear hold; any unanswered fact restarts it;
  the condition **clears** once the hold has elapsed with no unanswered fact;
- `app_killed` (reason `unresponsive`) raises the run's condition at once and pins it: answers
  for the killed run never start the hold (M1 has no restart; a new run's answers do);
- a feed gap (`forget`) withdraws a pending condition and restarts a running clear hold, but
  keeps a raised one: the wall is not untinted on evidence the judge did not see.

`software_renderer` (degraded, never display-affecting) follows the run's `app_renderer` fact:
a software rasterizer (llvmpipe, softpipe, swrast) raises it at once for that run; a GPU
renderer reported by any later run clears it. A feed gap keeps it, like any raised condition.

Every other feed kind (channel, link and relink facts, `kill_withheld`) is recorded or ignored,
never a fault; `app_link_accepted` is kept for its `player_id`. A code outside the catalogue is
refused (`ValueError`). Construction refuses timing under which the card could miss the kill:
K > k·T + raise + D and K > S + raise + D (the K rule).

Per Output (B10b): Display's `outputs` snapshot (every display feed read carries one) names the
connected Outputs and each one's admitted app run; display events only refine it until the next
snapshot (an invalidation drops that Output's admission). Each connected Output's underlay is
`slate` (nothing admitted), `held` (the admitted run is the raised unresponsive run) or `live`;
its codes are the raised codes (node-wide in M1: one app drives every Output). The underlay is
taken from `admitted` only, never from the snapshot's `fault` (E-B10a-7). The projection to
Display's `OverlayInstruction` per connected Output: tint on iff a display-affecting code is
raised; line 1 the household line, line 2 `"{code} · Player {player_id} · Output {output}"`;
the serial (one judge-wide counter) bumps whenever an Output's tint or lines change. A
`PresentedReport` for a serial the judge projected is kept in the ring as `presented`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from appliance.display_host.overlay.instruction import (
    MAX_TEXT,
    OverlayInstruction,
    PresentedReport,
)
from appliance.feed import FeedEvent
from contracts.node_faults import Fault

APP_UNRESPONSIVE = "app_unresponsive"
SOFTWARE_RENDERER = "software_renderer"
APP_ABSENT = "app_absent"  # 1b P1b: no app process (the broker's app_exited .. app_started)
APP_RESOURCE_EXHAUSTED = "app_resource_exhausted"  # 1b P1b: the run near its open-file limit
# A run holding at least this share of its soft open-file limit raises app_resource_exhausted
# (1b P1b). At the leak root-caused in 1b (about 240 an hour against 1,024), 80 % left ~50 minutes.
RESOURCE_PRESSURE_PERCENT = 80
# Mesa's CPU rasterizers, as GL_RENDERER names them (zink over lavapipe reports "llvmpipe" too).
_SOFTWARE_RENDERERS = ("llvmpipe", "softpipe", "software rasterizer", "swrast")
RING_CAPACITY = 256
_RUN_KEYS = {"invocation_id": str, "pid": int, "start_ticks": int, "app_epoch": int}
_UNANSWERED = ("probe_unanswered", "probe_kill_due")
MAX_OUTPUTS = 64  # a snapshot naming more Outputs is refused (Display ships 16)


@dataclass(frozen=True, slots=True)
class Condition:
    code: str
    run: dict  # the AppRunKey document of the run that holds the condition
    state: Literal["pending", "raised"]
    age_ms: int  # since the condition opened (pending), on the judge's clock


@dataclass(frozen=True, slots=True)
class OutputVerdict:
    output: str  # OutputKey.output_id: the name the health layer is taken for
    underlay: Literal["live", "held", "slate"]
    codes: tuple[str, ...]  # raised codes this Output shows


@dataclass(frozen=True, slots=True)
class Verdict:
    sequence: int  # bumps on every transition and every change of an Output's verdict
    conditions: tuple[Condition, ...]
    outputs: tuple[OutputVerdict, ...] = ()  # each connected Output of the latest snapshot


@dataclass(frozen=True, slots=True)
class DisplayOutput:
    """One Output of Display's `outputs` snapshot, as the judge keeps it."""

    output: str
    connected: bool
    admitted: dict | None  # the admitted app run's AppRunKey document


@dataclass(frozen=True, slots=True)
class Presented:
    sequence: int  # the verdict sequence in force when the report arrived
    output: str
    serial: int
    at_ms: int


@dataclass(frozen=True, slots=True)
class Transition:
    sequence: int  # the verdict sequence this transition produced
    code: str
    run: dict
    state: Literal["pending", "raised", "cleared", "withdrawn"]
    reason: str  # unanswered | window_elapsed | app_killed | run_changed | answered | hold_elapsed | feed_gap
    # | software_renderer | gpu_renderer
    at_ms: int


@dataclass(slots=True)
class _Open:
    run: dict
    since_ms: int
    raised: bool = False
    clearing_ms: int | None = None  # the clear hold started here
    killed: bool = False  # the run was killed: its own answers never clear it


def _run(value: object) -> dict | None:
    """A well-formed AppRunKey document, or None (a malformed fact is ignored, never raised on)."""
    if not isinstance(value, dict) or set(value) != set(_RUN_KEYS):
        return None
    for key, kind in _RUN_KEYS.items():
        if type(value[key]) is not kind:
            return None
    return dict(value)


def software_renderer(renderer: str) -> bool:
    """True iff `renderer` (a GL_RENDERER string) names a CPU rasterizer, not a GPU."""
    lowered = renderer.lower()
    return any(name in lowered for name in _SOFTWARE_RENDERERS)


def descriptor_pressure(open_descriptors: int, soft_limit: int) -> bool:
    """PURE (1b P1b). True iff `open_descriptors` is at least RESOURCE_PRESSURE_PERCENT of
    `soft_limit`, in integer arithmetic (`open * 100 >= percent * limit`). Both are the broker's
    `app_descriptors` fact; a non-int, a negative count or a limit below 1 is ValueError
    ("descriptor_fact") and the judge ignores that fact, as it ignores any malformed one."""
    raise NotImplementedError


def _positive(*values: object) -> bool:
    return all(type(value) is int and value > 0 for value in values)


def display_outputs(snapshot: object) -> tuple[DisplayOutput, ...]:
    """Display's `outputs` snapshot, checked: `ValueError("display_snapshot")` if malformed."""
    if not isinstance(snapshot, list) or len(snapshot) > MAX_OUTPUTS:
        raise ValueError("display_snapshot")
    outputs: dict[str, DisplayOutput] = {}
    for entry in snapshot:
        if not isinstance(entry, dict):
            raise ValueError("display_snapshot")
        name, connected, admitted = (entry.get("output_id"), entry.get("connected"),
                                     entry.get("admitted"))
        if not (isinstance(name, str) and name and type(connected) is bool) or name in outputs:
            raise ValueError("display_snapshot")
        run = None
        if admitted is not None:
            run = _run({key: admitted.get(key) for key in _RUN_KEYS}
                       if isinstance(admitted, dict) else None)
            if run is None:
                raise ValueError("display_snapshot")
        outputs[name] = DisplayOutput(name, connected, run)
    return tuple(outputs.values())


class HealthJudge:
    def __init__(self, *, period_ms: int, miss_limit: int, startup_ms: int, kill_after_ms: int,
                 pulse_deadline_ms: int, catalogue: Mapping[str, Fault]):
        if not _positive(period_ms, miss_limit, startup_ms, kill_after_ms, pulse_deadline_ms):
            raise ValueError("judge_timing")
        if not all(isinstance(fault, Fault) and fault.code == code
                   for code, fault in catalogue.items()):
            raise ValueError("fault_catalogue")
        self.catalogue = catalogue
        unresponsive = self._fault(APP_UNRESPONSIVE)
        margin = unresponsive.raise_window_ms + pulse_deadline_ms
        # The card must be raised and presented before the broker kills (system design rule 24).
        if not (kill_after_ms > miss_limit * period_ms + margin
                and kill_after_ms > startup_ms + margin):
            raise ValueError("k_rule")
        self.sequence = 0
        self.player: tuple[dict, str] | None = None  # (run, player_id) from app_link_accepted
        self._open: dict[str, _Open] = {}
        self._outputs: dict[str, DisplayOutput] = {}  # the latest snapshot, refined by events
        self._projected: dict[str, OverlayInstruction] = {}  # last instruction per Output
        self._serial = 0
        self._presented: dict[str, int] = {}  # last serial recorded presented per Output
        self._ring: deque[Transition | Presented] = deque(maxlen=RING_CAPACITY)
        self.ring_dropped = 0
        self._now = 0

    def _fault(self, code: str) -> Fault:
        try:
            return self.catalogue[code]
        except KeyError:
            raise ValueError("fault_code_unknown") from None

    # -- input --------------------------------------------------------------------------

    def observe(self, event: FeedEvent, now_ms: int) -> None:
        now = self._tick(now_ms)
        value = event.value if isinstance(event.value, dict) else {}
        run = _run(value.get("run"))
        if run is not None:
            if event.kind in _UNANSWERED:
                self._unanswered(APP_UNRESPONSIVE, run, now)
            elif event.kind == "probe_answered":
                self._answered(APP_UNRESPONSIVE, run, now)
            elif event.kind == "app_killed" and value.get("reason") == "unresponsive":
                self._killed(APP_UNRESPONSIVE, run, now)
            elif event.kind == "app_link_accepted" and isinstance(value.get("player_id"), str):
                self.player = (run, value["player_id"])
            elif event.kind == "app_renderer" and isinstance(value.get("renderer"), str):
                self._renderer(run, value["renderer"], now)
        self._advance(now)

    def observe_outputs(self, snapshot: object, now_ms: int) -> None:
        """Display's `outputs` snapshot replaces the Output set (malformed: `display_snapshot`,
        nothing changed). A gap in the display feed needs nothing more: every read has one."""
        outputs = display_outputs(snapshot)
        self._display(now_ms, lambda: {output.output: output for output in outputs})

    def observe_display(self, event: FeedEvent, now_ms: int) -> None:
        """A display event refines the snapshot until the next one: an invalidated surface drops
        its Output's admission. Every other display event (presentations, diagnostics) is
        evidence for the display feed's own readers, not a verdict input."""
        value = event.value if isinstance(event.value, dict) else {}
        output = value.get("output")
        name = output.get("output_id") if isinstance(output, dict) else None
        known = self._outputs.get(name) if isinstance(name, str) else None
        if event.kind != "SurfaceFact" or value.get("state") != "invalidated" or known is None:
            self._tick(now_ms)
            return
        self._display(now_ms, lambda: {**self._outputs,
                                       name: DisplayOutput(name, known.connected, None)})

    def forget(self, now_ms: int) -> None:
        """The publisher's feed had a gap (or a new incarnation): drop what the missed facts
        may have changed. A raised condition stays raised until fresh answers clear it."""
        now = self._tick(now_ms)
        for code, condition in list(self._open.items()):
            if condition.raised:
                condition.clearing_ms = None
            else:
                self._close(code, condition, "withdrawn", "feed_gap", now)
        self._advance(now)

    # -- output -------------------------------------------------------------------------

    def verdict(self, now_ms: int) -> Verdict:
        now = self._tick(now_ms)
        self._advance(now)
        return Verdict(self.sequence, tuple(
            Condition(code, dict(condition.run), "raised" if condition.raised else "pending",
                      now - condition.since_ms)
            for code, condition in sorted(self._open.items())), self._output_verdicts())

    def instructions(self, now_ms: int) -> tuple[OverlayInstruction, ...]:
        """Display's overlay instruction for each connected Output of the latest snapshot; an
        Output keeps its serial while its tint and lines are unchanged."""
        projected: dict[str, OverlayInstruction] = {}
        for output in self.verdict(now_ms).outputs:
            if len(output.output) > MAX_TEXT:
                continue  # a name the health layer protocol cannot carry
            tint, lines = self._card(output)
            previous = self._projected.get(output.output)
            if previous is None or (previous.tint, previous.lines) != (tint, lines):
                self._serial += 1
                previous = OverlayInstruction(output.output, self._serial, tint, lines)
            projected[output.output] = previous
        self._projected = projected
        self._presented = {name: serial for name, serial in self._presented.items()
                           if name in projected}
        return tuple(projected.values())

    def presented(self, report: PresentedReport, now_ms: int) -> bool:
        """The overlay client presented `serial` on `output`: kept in the ring once, if the judge
        projected that serial (or a later one) for that Output."""
        now = self._tick(now_ms)
        instruction = self._projected.get(report.output)
        if (instruction is None or report.serial > instruction.serial
                or self._presented.get(report.output, -1) >= report.serial):
            return False
        self._presented[report.output] = report.serial
        self._append(Presented(self.sequence, report.output, report.serial, now))
        return True

    def transitions(self) -> tuple[Transition | Presented, ...]:
        """The bounded ring (condition transitions and `presented` reports), oldest first;
        `ring_dropped` counts what fell off."""
        return tuple(self._ring)

    # -- rules --------------------------------------------------------------------------

    def _tick(self, now_ms: int) -> int:
        if type(now_ms) is not int:
            raise ValueError("judge_clock")
        self._now = max(self._now, now_ms)  # one clock, never read backwards
        return self._now

    def _display(self, now_ms: int, outputs) -> None:
        now = self._tick(now_ms)
        self._advance(now)
        before = self._output_verdicts()
        self._outputs = outputs()
        if self._output_verdicts() != before:
            self.sequence += 1

    def _output_verdicts(self) -> tuple[OutputVerdict, ...]:
        raised = {code: condition for code, condition in sorted(self._open.items())
                  if condition.raised}
        unresponsive = raised.get(APP_UNRESPONSIVE)
        verdicts = []
        for output in sorted(self._outputs.values(), key=lambda output: output.output):
            if not output.connected:
                continue
            if output.admitted is None:
                underlay = "slate"
            elif unresponsive is not None and output.admitted == unresponsive.run:
                underlay = "held"
            else:
                underlay = "live"
            verdicts.append(OutputVerdict(output.output, underlay, tuple(raised)))
        return tuple(verdicts)

    def _card(self, output: OutputVerdict) -> tuple[bool, tuple[str, str]]:
        for code in output.codes:
            fault = self._fault(code)
            if fault.display_affecting:
                player = "" if self.player is None else f" · Player {self.player[1]}"
                return True, (fault.household_line,
                              f"{code}{player} · Output {output.output}"[:MAX_TEXT])
        return False, ("", "")

    def _unanswered(self, code: str, run: dict, now: int) -> None:
        condition = self._open.get(code)
        if condition is None:
            self._begin(code, _Open(run, now), "pending", "unanswered", now)
        elif not condition.raised:
            if condition.run != run:  # an older run's window does not carry over
                self._close(code, condition, "withdrawn", "run_changed", now)
                self._begin(code, _Open(run, now), "pending", "unanswered", now)
        else:
            condition.clearing_ms = None
            if condition.run != run:  # still unresponsive, now as a new run
                condition.run, condition.killed = run, False
                self._record(code, run, "raised", "run_changed", now)

    def _answered(self, code: str, run: dict, now: int) -> None:
        condition = self._open.get(code)
        if condition is None or (condition.killed and condition.run == run):
            return
        if not condition.raised:
            self._close(code, condition, "withdrawn", "answered", now)
        elif condition.clearing_ms is None:
            condition.clearing_ms = now

    def _killed(self, code: str, run: dict, now: int) -> None:
        condition = self._open.get(code)
        if condition is None:
            self._begin(code, _Open(run, now, raised=True, killed=True), "raised", "app_killed", now)
            return
        changed = condition.run != run or not condition.raised
        condition.run, condition.killed, condition.clearing_ms = run, True, None
        if changed:
            condition.raised = True
            self._record(code, run, "raised", "app_killed", now)

    def _renderer(self, run: dict, renderer: str, now: int) -> None:
        condition = self._open.get(SOFTWARE_RENDERER)
        if not software_renderer(renderer):
            if condition is not None:
                self._close(SOFTWARE_RENDERER, condition, "cleared", "gpu_renderer", now)
        elif condition is None:
            self._begin(SOFTWARE_RENDERER, _Open(run, now, raised=True), "raised",
                        "software_renderer", now)
        elif condition.run != run:  # still software, now as a new run
            condition.run = run
            self._record(SOFTWARE_RENDERER, run, "raised", "run_changed", now)

    def _advance(self, now: int) -> None:
        for code, condition in list(self._open.items()):
            fault = self._fault(code)
            if not condition.raised and now - condition.since_ms >= fault.raise_window_ms:
                condition.raised = True
                self._record(code, condition.run, "raised", "window_elapsed", now)
            elif (condition.raised and condition.clearing_ms is not None
                  and now - condition.clearing_ms >= fault.clear_hold_ms):
                self._close(code, condition, "cleared", "hold_elapsed", now)

    def _begin(self, code: str, condition: _Open, state: str, reason: str, now: int) -> None:
        self._fault(code)
        self._open[code] = condition
        self._record(code, condition.run, state, reason, now)

    def _close(self, code: str, condition: _Open, state: str, reason: str, now: int) -> None:
        del self._open[code]
        self._record(code, condition.run, state, reason, now)

    def _record(self, code: str, run: dict, state: str, reason: str, now: int) -> None:
        self.sequence += 1
        self._append(Transition(self.sequence, code, dict(run), state, reason, now))

    def _append(self, entry: Transition | Presented) -> None:
        if len(self._ring) == self._ring.maxlen:
            self.ring_dropped += 1
        self._ring.append(entry)


__all__ = ["APP_ABSENT", "APP_RESOURCE_EXHAUSTED", "APP_UNRESPONSIVE", "MAX_OUTPUTS",
           "RESOURCE_PRESSURE_PERCENT", "RING_CAPACITY", "SOFTWARE_RENDERER", "Condition",
           "DisplayOutput", "HealthJudge", "OutputVerdict", "Presented", "Transition", "Verdict",
           "descriptor_pressure", "display_outputs", "software_renderer"]
