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

Every other feed kind (channel, link and relink facts, `kill_withheld`) is recorded or ignored,
never a fault; `app_link_accepted` is kept for its `player_id`. A code outside the catalogue is
refused (`ValueError`). Construction refuses timing under which the card could miss the kill:
K > k·T + raise + D and K > S + raise + D (the K rule).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from appliance.feed import FeedEvent
from contracts.node_faults import Fault

APP_UNRESPONSIVE = "app_unresponsive"
RING_CAPACITY = 256
_RUN_KEYS = {"invocation_id": str, "pid": int, "start_ticks": int, "app_epoch": int}
_UNANSWERED = ("probe_unanswered", "probe_kill_due")


@dataclass(frozen=True, slots=True)
class Condition:
    code: str
    run: dict  # the AppRunKey document of the run that holds the condition
    state: Literal["pending", "raised"]
    age_ms: int  # since the condition opened (pending), on the judge's clock


@dataclass(frozen=True, slots=True)
class Verdict:
    sequence: int  # bumps on every transition
    conditions: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class Transition:
    sequence: int  # the verdict sequence this transition produced
    code: str
    run: dict
    state: Literal["pending", "raised", "cleared", "withdrawn"]
    reason: str  # unanswered | window_elapsed | app_killed | run_changed | answered | hold_elapsed | feed_gap
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


def _positive(*values: object) -> bool:
    return all(type(value) is int and value > 0 for value in values)


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
        self._ring: deque[Transition] = deque(maxlen=RING_CAPACITY)
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
        self._advance(now)

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
            for code, condition in sorted(self._open.items())))

    def transitions(self) -> tuple[Transition, ...]:
        """The bounded transition ring, oldest first (`ring_dropped` counts what fell off)."""
        return tuple(self._ring)

    # -- rules --------------------------------------------------------------------------

    def _tick(self, now_ms: int) -> int:
        if type(now_ms) is not int:
            raise ValueError("judge_clock")
        self._now = max(self._now, now_ms)  # one clock, never read backwards
        return self._now

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
        if len(self._ring) == self._ring.maxlen:
            self.ring_dropped += 1
        self._ring.append(Transition(self.sequence, code, dict(run), state, reason, now))


__all__ = ["APP_UNRESPONSIVE", "RING_CAPACITY", "Condition", "HealthJudge", "Transition", "Verdict"]
