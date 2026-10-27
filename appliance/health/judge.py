"""The health judge: node facts in, one sequenced verdict out. Pure (no I/O, no clock of its own).

The caller passes every fact with the judge's own observation time (`now_ms`, one node clock);
facts carry no time the judge compares. Nothing kills or restarts a Player (issue #125), so
the run causes are pinned: once raised, `app_unresponsive` and `app_absent` stay on the card
until the broker reports another run started (`app_started`, reason `started`); a person
recovers the run. Rules for `app_unresponsive`:

- `probe_unanswered` for a run opens the condition **pending**;
- pending becomes **raised** once it has stayed unanswered for the catalogue's raise window;
  a `probe_answered` while pending withdraws it (it was never shown);
- a `probe_answered` while raised changes nothing: a frozen run that answers again keeps its
  card until a new run starts;
- a feed gap (`forget`) withdraws a pending condition but keeps a raised one: the wall is not
  untinted on evidence the judge did not see.

`app_absent` (display-affecting) follows the broker's run facts (`appliance.kernel.app_facts`):
`app_exited` opens it pending for that run (a new pending window if it was open for another
run), raised after the catalogue's raise window; `app_started` for any other run withdraws a
pending one (Display's purposeful Weston restart inside the window is never shown) or clears a
raised one at once. Whether the new run answers is `app_unresponsive`'s question (E-1B-16).

`app_resource_exhausted` (degraded) follows `app_descriptors` for the run that holds it, or for
any run while none is open: a run holding at least RESOURCE_PRESSURE_PERCENT of its soft
open-file limit raises it at once and stops a running clear hold; a sample below that starts the
hold, and it clears (`descriptor_relief`) once the catalogue's clear hold has passed with no
sample under pressure, or when that run exits.

`app_descriptor_limit` (display-affecting) is the same fact at the limit: a run within
DESCRIPTOR_LIMIT_HEADROOM of its soft limit (`descriptor_at_limit`) can open nothing more, so it
raises at once for that run and, like a run cause, stays until another run starts (issue #123).
Pressure alone (`app_resource_exhausted`) never tints.

`nothing_to_show` (display-affecting, issue #123) is per Output, in its own map keyed by the
Output's name: a connected Output whose power in force is not off (Display's snapshot `power`:
Night off and Resting are power off) and that no run holds (nothing admitted, or the shell's
`holding` false: no picture plane and no drawing of Display's own) opens it pending; it is raised
after the catalogue's window and withdrawn (pending) or cleared (raised) once the Output is held
again, powered off or gone. Its transitions name the Output as their `run` (`{"output": name}`).
Authored black is the Player's black layer, a picture plane, so it holds; so is a Frame with
nothing kept and nothing playing, drawn as the Player's dark plane (player/planes.py `compose`).

`software_renderer` (degraded, never display-affecting) follows the run's `app_renderer` fact:
a software rasterizer (llvmpipe, softpipe, swrast) raises it at once for that run; a GPU
renderer reported by any later run clears it. A feed gap keeps it, like any raised condition.

Every other feed kind (channel, link and relink facts) is recorded or ignored, never a fault;
`app_link_accepted` is kept for its `player_id`. A code outside the catalogue is refused
(`ValueError`).

Per Output (B10b): Display's `outputs` snapshot (every display feed read carries one) names the
connected Outputs and each one's admitted app run; display events only refine it until the next
snapshot (an invalidation drops that Output's admission). Each connected Output's underlay is
`slate` (nothing admitted), `held` (the admitted run is the raised unresponsive run) or `live`;
its codes are the raised codes (node-wide in M1: one app drives every Output) and its own
`nothing_to_show`. The underlay is taken from `admitted` only, never from the snapshot's `fault`
(E-B10a-7). Its card is the first display-affecting raised code in CARD_PRECEDENCE, with its age.
The projection to Display's `OverlayInstruction` per connected Output: tint on iff it has a card;
line 1 `"{household line} · since {HH:MM}"`, the time the condition's evidence began on
the Pi's own clock (`evidence_wall_s`, `card_time`), line 2
`"{code} · Player {player_id} · Output {output}"`; the serial (one judge-wide counter) bumps
whenever an Output's tint or lines change. A
`PresentedReport` for a serial the judge projected is kept in the ring as `presented`.
"""

from __future__ import annotations

import datetime
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from appliance.display_host.overlay.instruction import (
    MAX_TEXT,
    OverlayInstruction,
    PresentedReport,
)
from appliance.feed import FeedEvent
from appliance.kernel.app_facts import APP_DESCRIPTORS, APP_EXITED, APP_STARTED
from contracts.node_faults import Fault

APP_UNRESPONSIVE = "app_unresponsive"
SOFTWARE_RENDERER = "software_renderer"
APP_ABSENT = "app_absent"  # 1b P1b: no app process (the broker's app_exited .. app_started)
APP_RESOURCE_EXHAUSTED = "app_resource_exhausted"  # 1b P1b: the run near its open-file limit
APP_DESCRIPTOR_LIMIT = "app_descriptor_limit"  # #123: the run at its open-file limit
NOTHING_TO_SHOW = "nothing_to_show"  # #123: an Output on that no run holds, per Output
# A run within this many descriptors of its soft limit is at it (#123, a placeholder of r2's): it
# can open nothing a photo needs, so its Frame shows nothing new.
DESCRIPTOR_LIMIT_HEADROOM = 16
# Which raised display-affecting code an Output's card shows: the first of these it has.
CARD_PRECEDENCE: Final = (APP_ABSENT, APP_UNRESPONSIVE, APP_DESCRIPTOR_LIMIT, NOTHING_TO_SHOW)
# A run holding at least this share of its soft open-file limit raises app_resource_exhausted
# (1b P1b). At the leak root-caused in 1b (about 240 an hour against 1,024), 80 % left ~50 minutes.
RESOURCE_PRESSURE_PERCENT = 80
# A raised condition whose clear hold elapsed clears for this reason (default `hold_elapsed`).
_RELIEF = {APP_RESOURCE_EXHAUSTED: "descriptor_relief"}
# Mesa's CPU rasterizers, as GL_RENDERER names them (zink over lavapipe reports "llvmpipe" too).
_SOFTWARE_RENDERERS = ("llvmpipe", "softpipe", "software rasterizer", "swrast")
RING_CAPACITY = 256
_RUN_KEYS = {"invocation_id": str, "pid": int, "start_ticks": int, "app_epoch": int}
_UNANSWERED = ("probe_unanswered",)
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
    card: str | None  # the first display-affecting raised code by CARD_PRECEDENCE; None: no tint
    card_age_ms: int | None  # now minus the card's condition's opening, on the judge's clock


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
    holding: bool  # the shell's hold: a picture plane or Display's own drawing keeps the Output
    power: Literal["on", "off"] | None  # the power in force (None: no report or none in force)


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
    reason: str  # unanswered | window_elapsed | run_changed | answered | hold_elapsed | feed_gap
    # | software_renderer | gpu_renderer | app_exited | started | descriptor_pressure
    # | descriptor_relief | descriptor_limit | not_held | held | powered_off | disconnected
    at_ms: int


@dataclass(slots=True)
class _Open:
    run: dict
    since_ms: int
    raised: bool = False
    clearing_ms: int | None = None  # the clear hold started here


def _run(value: object) -> dict | None:
    """A well-formed AppRunKey document, or None (a malformed fact is ignored, never raised on)."""
    if not isinstance(value, dict) or set(value) != set(_RUN_KEYS):
        return None
    for key, kind in _RUN_KEYS.items():
        if type(value[key]) is not kind:
            return None
    return dict(value)


def _card_order(codes: tuple[str, ...]) -> tuple[str, ...]:
    """`codes` in CARD_PRECEDENCE, then any other code by name."""
    return (tuple(code for code in CARD_PRECEDENCE if code in codes)
            + tuple(code for code in codes if code not in CARD_PRECEDENCE))


def software_renderer(renderer: str) -> bool:
    """True iff `renderer` (a GL_RENDERER string) names a CPU rasterizer, not a GPU."""
    lowered = renderer.lower()
    return any(name in lowered for name in _SOFTWARE_RENDERERS)


def descriptor_pressure(open_descriptors: int, soft_limit: int) -> bool:
    """PURE (1b P1b). True iff `open_descriptors` is at least RESOURCE_PRESSURE_PERCENT of
    `soft_limit`, in integer arithmetic (`open * 100 >= percent * limit`). Both are the broker's
    `app_descriptors` fact; a non-int, a negative count or a limit below 1 is ValueError
    ("descriptor_fact") and the judge ignores that fact, as it ignores any malformed one."""
    if (type(open_descriptors) is not int or type(soft_limit) is not int
            or open_descriptors < 0 or soft_limit < 1):
        raise ValueError("descriptor_fact")
    return open_descriptors * 100 >= RESOURCE_PRESSURE_PERCENT * soft_limit


def descriptor_at_limit(open_descriptors: int, soft_limit: int) -> bool:
    """PURE (#123). True iff `open_descriptors` is within DESCRIPTOR_LIMIT_HEADROOM of
    `soft_limit` (`open + headroom >= limit`). The same facts and refusals as
    `descriptor_pressure` (ValueError("descriptor_fact"))."""
    descriptor_pressure(open_descriptors, soft_limit)
    return open_descriptors + DESCRIPTOR_LIMIT_HEADROOM >= soft_limit


def evidence_wall_s(since_ms: int, now_ms: int, wall_ms: int) -> int:
    """PURE. The Pi's wall-clock second at which evidence that began at `since_ms` on the judge's
    boot clock began, given that the boot clock reads `now_ms` while the wall clock reads
    `wall_ms`: (wall_ms - (now_ms - since_ms)) // 1000. Only the boot clock's own interval is
    used; the wall clock is read once, now."""
    return (wall_ms - (now_ms - since_ms)) // 1000


def card_time(wall_s: int, zone: datetime.tzinfo) -> str:
    """PURE. `wall_s` (seconds since the epoch) as the card shows it in `zone`: "HH:MM"."""
    return datetime.datetime.fromtimestamp(wall_s, zone).strftime("%H:%M")


def _positive(*values: object) -> bool:
    return all(type(value) is int and value > 0 for value in values)


def display_outputs(snapshot: object) -> tuple[DisplayOutput, ...]:
    """Display's `outputs` snapshot, checked: `ValueError("display_snapshot")` if malformed (an
    entry without `holding` or `power` included)."""
    if not isinstance(snapshot, list) or len(snapshot) > MAX_OUTPUTS:
        raise ValueError("display_snapshot")
    outputs: dict[str, DisplayOutput] = {}
    for entry in snapshot:
        if not isinstance(entry, dict) or "holding" not in entry or "power" not in entry:
            raise ValueError("display_snapshot")
        name, connected, admitted = (entry.get("output_id"), entry.get("connected"),
                                     entry.get("admitted"))
        holding, power = entry["holding"], entry["power"]
        if (not (isinstance(name, str) and name and type(connected) is bool) or name in outputs
                or type(holding) is not bool or power not in ("on", "off", None)):
            raise ValueError("display_snapshot")
        run = None
        if admitted is not None:
            run = _run({key: admitted.get(key) for key in _RUN_KEYS}
                       if isinstance(admitted, dict) else None)
            if run is None:
                raise ValueError("display_snapshot")
        outputs[name] = DisplayOutput(name, connected, run, holding, power)
    return tuple(outputs.values())


class HealthJudge:
    def __init__(self, *, period_ms: int, miss_limit: int, startup_ms: int,
                 pulse_deadline_ms: int, catalogue: Mapping[str, Fault]):
        if not _positive(period_ms, miss_limit, startup_ms, pulse_deadline_ms):
            raise ValueError("judge_timing")
        if not all(isinstance(fault, Fault) and fault.code == code
                   for code, fault in catalogue.items()):
            raise ValueError("fault_catalogue")
        self.catalogue = catalogue
        self._fault(APP_UNRESPONSIVE)
        self._fault(NOTHING_TO_SHOW)  # judged every turn (`_advance`)
        self.sequence = 0
        self.player: tuple[dict, str] | None = None  # (run, player_id) from app_link_accepted
        self._open: dict[str, _Open] = {}
        self._nothing: dict[str, _Open] = {}  # nothing_to_show, by Output name
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
            elif event.kind == "app_link_accepted" and isinstance(value.get("player_id"), str):
                self.player = (run, value["player_id"])
            elif event.kind == "app_renderer" and isinstance(value.get("renderer"), str):
                self._renderer(run, value["renderer"], now)
            elif event.kind == APP_EXITED:
                self._exited(run, now)
            elif event.kind == APP_STARTED:
                self._started(run, now)
            elif event.kind == APP_DESCRIPTORS:
                self._descriptors(run, value.get("open"), value.get("soft_limit"), now)
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
                                       name: DisplayOutput(name, known.connected, None, False,
                                                           known.power)})

    def forget(self, now_ms: int) -> None:
        """The publisher's feed had a gap (or a new incarnation): drop what the missed facts
        may have changed. A raised condition stays raised: a run cause until another run
        starts, a degraded one until fresh evidence clears it."""
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

    def instructions(self, now_ms: int, *, wall_ms: int,
                     zone: datetime.tzinfo = datetime.timezone.utc) -> tuple[OverlayInstruction, ...]:
        """Display's overlay instruction for each connected Output of the latest snapshot; an
        Output keeps its serial while its tint and lines are unchanged. `wall_ms` is the Pi's
        wall clock read with `now_ms` and `zone` its local zone: they place the card's time."""
        projected: dict[str, OverlayInstruction] = {}
        verdict = self.verdict(now_ms)
        for output in verdict.outputs:
            if len(output.output) > MAX_TEXT:
                continue  # a name the health layer protocol cannot carry
            tint, lines = self._card(output, self._now, wall_ms, zone)
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
        before = self._shown()
        self._outputs = outputs()
        self._held(now)
        if self._shown() != before:
            self.sequence += 1

    def _shown(self) -> tuple:
        """The Output verdicts without their card's age (which moves with the clock)."""
        return tuple((verdict.output, verdict.underlay, verdict.codes, verdict.card)
                     for verdict in self._output_verdicts())

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
            shown = dict(raised)
            nothing = self._nothing.get(output.output)
            if nothing is not None and nothing.raised:
                shown[NOTHING_TO_SHOW] = nothing
            codes = tuple(sorted(shown))
            card = next((code for code in _card_order(codes)
                         if self._fault(code).display_affecting), None)
            verdicts.append(OutputVerdict(
                output.output, underlay, codes, card,
                None if card is None else self._now - shown[card].since_ms))
        return tuple(verdicts)

    def _card(self, output: OutputVerdict, now: int, wall_ms: int,
              zone: datetime.tzinfo) -> tuple[bool, tuple[str, str]]:
        if output.card is None or output.card_age_ms is None:
            return False, ("", "")
        fault = self._fault(output.card)
        since = card_time(evidence_wall_s(now - output.card_age_ms, now, wall_ms), zone)
        player = "" if self.player is None else f" · Player {self.player[1]}"
        return True, (f"{fault.household_line} · since {since}"[:MAX_TEXT],
                      f"{output.card}{player} · Output {output.output}"[:MAX_TEXT])

    def _held(self, now: int) -> None:
        """Open, withdraw or clear each Output's `nothing_to_show` from the latest Output set."""
        for name, output in self._outputs.items():
            if (name not in self._nothing and output.connected and output.power != "off"
                    and (output.admitted is None or not output.holding)):
                self._nothing[name] = _Open({"output": name}, now)
                self._record(NOTHING_TO_SHOW, {"output": name}, "pending", "not_held", now)
        for name, condition in list(self._nothing.items()):
            output = self._outputs.get(name)
            if output is None or not output.connected:
                reason = "disconnected"
            elif output.power == "off":
                reason = "powered_off"
            elif output.admitted is not None and output.holding:
                reason = "held"
            else:
                continue
            del self._nothing[name]
            self._record(NOTHING_TO_SHOW, condition.run,
                         "cleared" if condition.raised else "withdrawn", reason, now)

    def _unanswered(self, code: str, run: dict, now: int) -> None:
        condition = self._open.get(code)
        if condition is None:
            self._begin(code, _Open(run, now), "pending", "unanswered", now)
        elif not condition.raised:
            if condition.run != run:  # an older run's window does not carry over
                self._close(code, condition, "withdrawn", "run_changed", now)
                self._begin(code, _Open(run, now), "pending", "unanswered", now)
        else:
            if condition.run != run:  # still unresponsive, now as a new run
                condition.run = run
                self._record(code, run, "raised", "run_changed", now)

    def _answered(self, code: str, run: dict, now: int) -> None:
        """An answer withdraws only a pending condition of its run; a raised one is pinned
        until another run starts (`_started`)."""
        condition = self._open.get(code)
        if condition is not None and not condition.raised and condition.run == run:
            self._close(code, condition, "withdrawn", "answered", now)

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

    def _exited(self, run: dict, now: int) -> None:
        condition = self._open.get(APP_ABSENT)
        if condition is None:
            self._begin(APP_ABSENT, _Open(run, now), "pending", "app_exited", now)
        elif condition.run != run:
            if condition.raised:  # still absent, now as a later run
                condition.run, condition.clearing_ms = run, None
                self._record(APP_ABSENT, run, "raised", "run_changed", now)
            else:  # an older run's window does not carry over
                self._close(APP_ABSENT, condition, "withdrawn", "run_changed", now)
                self._begin(APP_ABSENT, _Open(run, now), "pending", "app_exited", now)
        exhausted = self._open.get(APP_RESOURCE_EXHAUSTED)
        if exhausted is not None and exhausted.run == run:
            self._close(APP_RESOURCE_EXHAUSTED, exhausted, "cleared", "run_changed", now)

    def _started(self, run: dict, now: int) -> None:
        """Another run started: the only way a run cause (`app_absent`, `app_unresponsive`,
        `app_descriptor_limit`) held by an earlier run clears once raised."""
        for code in (APP_ABSENT, APP_UNRESPONSIVE, APP_DESCRIPTOR_LIMIT):
            condition = self._open.get(code)
            if condition is not None and condition.run != run:
                self._close(code, condition, "cleared" if condition.raised else "withdrawn",
                            "started", now)

    def _descriptors(self, run: dict, open_descriptors: object, soft_limit: object,
                     now: int) -> None:
        try:
            pressure = descriptor_pressure(open_descriptors, soft_limit)
            limit = descriptor_at_limit(open_descriptors, soft_limit)
        except ValueError:
            return
        if limit:
            pinned = self._open.get(APP_DESCRIPTOR_LIMIT)
            if pinned is None:
                self._begin(APP_DESCRIPTOR_LIMIT, _Open(run, now, raised=True), "raised",
                            "descriptor_limit", now)
            elif pinned.run != run:  # still at the limit, now as a later run
                pinned.run = run
                self._record(APP_DESCRIPTOR_LIMIT, run, "raised", "run_changed", now)
        condition = self._open.get(APP_RESOURCE_EXHAUSTED)
        if condition is None:
            if pressure:
                self._begin(APP_RESOURCE_EXHAUSTED, _Open(run, now, raised=True), "raised",
                            "descriptor_pressure", now)
        elif condition.run == run:
            if pressure:
                condition.clearing_ms = None
            elif condition.clearing_ms is None:
                condition.clearing_ms = now

    def _advance(self, now: int) -> None:
        window = self._fault(NOTHING_TO_SHOW).raise_window_ms
        for condition in self._nothing.values():
            if not condition.raised and now - condition.since_ms >= window:
                condition.raised = True
                self._record(NOTHING_TO_SHOW, condition.run, "raised", "window_elapsed", now)
        for code, condition in list(self._open.items()):
            fault = self._fault(code)
            if not condition.raised and now - condition.since_ms >= fault.raise_window_ms:
                condition.raised = True
                self._record(code, condition.run, "raised", "window_elapsed", now)
            elif (condition.raised and condition.clearing_ms is not None
                  and now - condition.clearing_ms >= fault.clear_hold_ms):
                self._close(code, condition, "cleared", _RELIEF.get(code, "hold_elapsed"), now)

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


__all__ = ["APP_ABSENT", "APP_DESCRIPTOR_LIMIT", "APP_RESOURCE_EXHAUSTED", "APP_UNRESPONSIVE",
           "CARD_PRECEDENCE", "DESCRIPTOR_LIMIT_HEADROOM", "MAX_OUTPUTS", "NOTHING_TO_SHOW",
           "RESOURCE_PRESSURE_PERCENT", "RING_CAPACITY", "SOFTWARE_RENDERER", "Condition",
           "DisplayOutput", "HealthJudge", "OutputVerdict", "Presented", "Transition", "Verdict",
           "card_time", "descriptor_at_limit", "descriptor_pressure", "display_outputs",
           "evidence_wall_s", "software_renderer"]
