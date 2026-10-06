"""What each Output's health layer should show, from the judge's overlay instructions. PURE.

Stdlib and `.instruction` only (no Wayland, no sockets, no clock of its own), so the rules are
unit-tested anywhere; `client.HealthLayer` draws what `HealthOutput.page` returns and sends what
`HealthOutput.presented` returns.

Rules:
- An instruction with **any** serial is taken (the judge's counter restarts at 1 per judge
  process); the Output is dirty iff (serial, tint, lines) differs from what was last drawn.
- `reconnected()` (the judge link closed) forgets the connection's instruction and what was drawn
  and reported, so the first instruction on the next connection repaints and is reported; the
  screen keeps its last drawing until then.
- **Stale**: no instruction within V (`INSTRUCTION_STALE_MS`) of `started_ms` or of the last
  instruction -> tint on with `UNAVAILABLE_LINES` and serial None. A stale page is never reported.
- `presented(serial)` reports a serial drawn on this connection, once; `discarded` reports nothing.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass

from .instruction import (
    INSTRUCTION_STALE_MS,
    UNAVAILABLE_LINES,
    OverlayInstruction,
    PresentedReport,
)

# The judge's socket (appliance.health.runner.HEALTH_SOCKET; Display may not import Health, so a
# test pins the two equal). The environment variable exists for the display harness's fake judge.
HEALTH_SOCKET_ENV = "PHOTO_WALL_HEALTH_SOCKET"
DEFAULT_HEALTH_SOCKET = "/run/photo-wall-health/health.sock"
JUDGE_UIDS = frozenset({0, 10006})   # root, pw-health: the only peers the client takes a card from
RECONNECT_MS = 1000
DRAWN_SERIALS = 8                    # serials drawn on a connection that may still be presented


def health_socket_path(environ: dict[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get(HEALTH_SOCKET_ENV) or DEFAULT_HEALTH_SOCKET


@dataclass(frozen=True, slots=True)
class HealthPage:
    """One drawing of an Output's health layer. `serial` None = the stale (unavailable) page."""
    serial: int | None
    tint: bool
    lines: tuple[str, str]


STALE_PAGE = HealthPage(None, True, UNAVAILABLE_LINES)


class HealthOutput:
    """One Output's health state on the overlay client's own clock (monotonic ms)."""

    def __init__(self, name: str, started_ms: int, *, stale_ms: int = INSTRUCTION_STALE_MS):
        self.name, self.stale_ms = name, stale_ms
        self._since = started_ms                  # started, or the last instruction
        self._instruction: OverlayInstruction | None = None
        self._drawn: HealthPage | None = None
        self._drawn_serials: deque[int] = deque(maxlen=DRAWN_SERIALS)
        self._reported: set[int] = set()

    def instruction(self, instruction: OverlayInstruction, now_ms: int) -> None:
        """Take the judge's instruction for this Output (any serial); refreshes staleness."""
        if instruction.output != self.name:
            raise ValueError("health_output")
        self._instruction = instruction
        self._since = max(self._since, now_ms)

    def reconnected(self) -> None:
        """The judge link closed: what was drawn and reported belongs to the old connection."""
        self._instruction = None
        self._drawn = None
        self._drawn_serials.clear()
        self._reported.clear()

    def repaint(self) -> None:
        """Draw the current page again (the Output's size changed); nothing is re-reported."""
        self._drawn = None

    def stale(self, now_ms: int) -> bool:
        return now_ms - self._since >= self.stale_ms

    def deadline(self, now_ms: int) -> int | None:
        """When this Output turns stale, or None if it already is."""
        return None if self.stale(now_ms) else self._since + self.stale_ms

    def current(self, now_ms: int) -> HealthPage | None:
        """The page this Output should show; None = no instruction on this connection yet."""
        if self.stale(now_ms):
            return STALE_PAGE
        instruction = self._instruction
        if instruction is None:
            return None
        return HealthPage(instruction.serial, instruction.tint, instruction.lines)

    def page(self, now_ms: int) -> HealthPage | None:
        """The page to draw now, or None (nothing new to draw)."""
        page = self.current(now_ms)
        return None if page is None or page == self._drawn else page

    def drawn(self, page: HealthPage) -> None:
        """`page` was committed (or, refused for its size, replaced by the shell's fallback)."""
        self._drawn = page
        if page.serial is not None and page.serial not in self._drawn_serials:
            self._drawn_serials.append(page.serial)

    def presented(self, serial: int | None) -> PresentedReport | None:
        """The commit of `serial` was presented: a report once per serial per connection, only
        for a serial drawn on this connection; None for the stale page."""
        if serial is None or serial not in self._drawn_serials or serial in self._reported:
            return None
        self._reported.add(serial)
        return PresentedReport(self.name, serial)

    def discarded(self, serial: int | None) -> None:
        """The commit of `serial` was never shown: nothing to report (a later presentation of the
        same serial still reports it)."""
        return None


class HealthBoard:
    """Every Output's `HealthOutput`. Instructions for an Output not configured yet are kept
    (at most `limit` names, newest per name) and applied on its configure."""

    def __init__(self, *, limit: int, stale_ms: int = INSTRUCTION_STALE_MS):
        self.limit, self.stale_ms = limit, stale_ms
        self.outputs: dict[str, HealthOutput] = {}
        self._pending: dict[str, tuple[OverlayInstruction, int]] = {}

    def configure(self, name: str, now_ms: int) -> HealthOutput:
        output = self.outputs.get(name)
        if output is None:
            output = self.outputs[name] = HealthOutput(name, now_ms, stale_ms=self.stale_ms)
            pending = self._pending.pop(name, None)
            if pending is not None:
                output.instruction(*pending)
        return output

    def instruction(self, instruction: OverlayInstruction, now_ms: int) -> HealthOutput | None:
        """The Output the instruction applies to, or None (kept, or dropped over the limit)."""
        output = self.outputs.get(instruction.output)
        if output is not None:
            output.instruction(instruction, now_ms)
            return output
        if instruction.output in self._pending or len(self._pending) < self.limit:
            self._pending[instruction.output] = (instruction, now_ms)
        return None

    def reconnected(self) -> None:
        self._pending.clear()
        for output in self.outputs.values():
            output.reconnected()

    def deadline(self, now_ms: int) -> int | None:
        """The earliest staleness deadline still ahead, or None."""
        deadlines = [d for d in (o.deadline(now_ms) for o in self.outputs.values()) if d is not None]
        return min(deadlines, default=None)
