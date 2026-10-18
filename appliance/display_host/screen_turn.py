"""The Pi's screen turns: each Output's screen hangs as its Output document says (render redesign S3,
run ledger .claude/runs/render-planes/s3-page.md, bead B1; owner, gate answer Q3, 2026-10-17: the
turn belongs to the spot on the wall, so Central sends it in the Output document,
contracts/node_output.py `OutputDocument.turn`).

Pure core, effects at the edges: `turn_due` is pure; `ScreenTurns` carries it out through one port,
`ScreenShell` (the shell's `turn` control op; appliance/display_host/weston.py `WestonBackend.turn`),
on its own worker thread, so neither the bus's document callback nor the Weston dispatch loop ever
waits on the shell. It hears of Outputs as the power and picture controllers do: runner.py
`PowerObservations`, from the Output reports (a connection or an identity that changed).

Rules this module owns (one home each):
- **What is wanted:** the newest Output document's `turn` for that Output; no document, nothing
  wanted (the screen is left as the shell has it).
- **When to act** (`turn_due`): on a trigger: a document whose turn differs from the last one the
  shell confirmed for that Output, and the Output appearing connected (at start, documents arrive
  before the shell has announced any Output, so the first ask is refused; after a hot-plug the
  shell has kept the turn, and asking again for the transform it already has changes nothing: the
  shell remodes only on a change).
- **Until confirmed** (render planes end review): a refused or unanswered turn of an Output
  reported connected is asked again after RETRY_FIRST seconds, doubling to RETRY_MOST, until the
  shell confirms it, the Output goes, or a newer trigger asks; documents are sent only when they
  change, so waiting for the next trigger could leave a screen unturned for good (Central sizes
  pictures by the turn it sent, the Player places by Weston's).
- **Newest wins:** a document that arrives while the worker asks the shell replaces the one
  waiting.

Rules it does NOT own: the turn's wire value (contracts/display_turn.py `wayland_transform`), what
a turn does to the Output (the shell: a mode change, the app's grant invalidated and the Output
back under a new key, which Central's surface decisions grant again), the Player drawing its
pictures turned (player/plane_screen.py reads the turn from Weston).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Final, Protocol

from contracts.display_turn import DisplayTurn
from contracts.node_output import OutputDocument

log = logging.getLogger(__name__)

TURN_SECONDS: Final = 2.0      # one `turn` op's answer from the shell
RETRY_FIRST: Final = 1.0       # seconds before a refused turn is asked again (module rule, Until confirmed)
RETRY_MOST: Final = 30.0       # the longest wait between two asks of a refused turn


class ScreenShell(Protocol):
    """The shell's `turn` op, as appliance/display_host/weston.py `WestonBackend` carries it."""

    def turn(self, output_id: str, turn: DisplayTurn, *, timeout: float) -> bool:
        """Whether the shell confirmed `output_id`'s screen turned `turn` (False: no such Output, a
        refusal or no answer within `timeout`). Never raises for a turn in TURNS."""
        ...


def turn_due(wanted: DisplayTurn | None, confirmed: DisplayTurn | None, *, appeared: bool) -> bool:
    """Whether the worker asks the shell now (module rule, When to act): `wanted` is the newest
    document's turn (None: no document yet), `confirmed` the turn the shell last confirmed for this
    Output (None: none since it last appeared), `appeared` whether this trigger is the Output
    appearing connected. True when `wanted` is not None and (`appeared` or `wanted != confirmed`).
    Pure."""
    return wanted is not None and (appeared or wanted != confirmed)


class ScreenTurns:
    """Carries each Output's wanted turn to the shell (module rules), on its own worker thread.

    One lock guards the wanted, confirmed and waiting turns and the retries; the worker holds it
    never while it asks the shell. Each Output's appearance count (`_seen`) marks an answer that
    lands after the Output went away or came back, which then confirms nothing."""

    def __init__(self, shell: ScreenShell, *, clock=time.monotonic) -> None:
        self._shell, self._clock = shell, clock
        self._connected: set[str] = set()                # Outputs whose last report said connected
        self._retry_at: dict[str, float] = {}           # Output -> when its refused turn is asked again
        self._backoff: dict[str, float] = {}            # Output -> the last wait before that ask
        self._changed = threading.Condition()
        self._wanted: dict[str, DisplayTurn] = {}
        self._confirmed: dict[str, DisplayTurn] = {}
        self._waiting: dict[str, DisplayTurn] = {}       # one ask per Output, the newest; in order
        self._seen: dict[str, int] = {}
        self._stopping = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the worker thread (once)."""
        with self._changed:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="screen-turns", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker and join it within `timeout`."""
        with self._changed:
            self._stopping = True
            self._changed.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)

    def document(self, output_id: str, document: OutputDocument) -> None:
        """The newest Output document for `output_id` (runner.py `document_feed`, on the bus's
        thread): its turn is wanted from now on, and asked for when `turn_due` says. Never blocks."""
        with self._changed:
            self._wanted[output_id] = document.turn
            self._ask(output_id, appeared=False)

    def output(self, output_id: str, *, connected: bool) -> None:
        """`output_id`'s connection as its Output report says (runner.py `PowerObservations`, on the
        dispatch thread): appearing connected forgets the confirmed turn and asks for the wanted one
        (`turn_due` with `appeared`); disconnected forgets the confirmed turn. Never blocks."""
        with self._changed:
            self._seen[output_id] = self._seen.get(output_id, 0) + 1
            self._retry_at.pop(output_id, None)
            self._backoff.pop(output_id, None)
            if connected:
                self._connected.add(output_id)
                self._ask(output_id, appeared=True)
            else:
                self._connected.discard(output_id)
                self._waiting.pop(output_id, None)
            self._confirmed.pop(output_id, None)

    def confirmed(self, output_id: str) -> DisplayTurn | None:
        """The turn the shell last confirmed for `output_id` since it last appeared; None before
        any."""
        with self._changed:
            return self._confirmed.get(output_id)

    def _ask(self, output_id: str, *, appeared: bool) -> None:
        """Queue the wanted turn when `turn_due` says (newest wins: it replaces the waiting one and
        goes last), or drop the waiting one when the newest needs no ask. Under the lock."""
        wanted = self._wanted.get(output_id)
        self._waiting.pop(output_id, None)
        if wanted is not None and turn_due(wanted, self._confirmed.get(output_id), appeared=appeared):
            self._waiting[output_id] = wanted
            self._changed.notify_all()

    def _retry_due(self) -> float | None:
        """Queue every refused turn whose retry is due (module rule, Until confirmed); the seconds
        until the next one, None when none waits. Under the lock."""
        now = self._clock()
        for output_id, at in list(self._retry_at.items()):
            if at <= now:
                del self._retry_at[output_id]
                wanted = self._wanted.get(output_id)
                if (output_id in self._connected and wanted is not None and output_id not in self._waiting
                        and wanted != self._confirmed.get(output_id)):
                    self._waiting[output_id] = wanted
        return min((at - now for at in self._retry_at.values()), default=None)

    def _refused(self, output_id: str, turn: DisplayTurn) -> None:
        """A refused or unanswered ask: asked again later while the Output is connected and the turn
        still wanted (module rule, Until confirmed). Under the lock."""
        if output_id not in self._connected or self._wanted.get(output_id) != turn:
            return
        wait = min(RETRY_MOST, 2 * self._backoff[output_id]) if output_id in self._backoff else RETRY_FIRST
        self._backoff[output_id] = wait
        self._retry_at[output_id] = self._clock() + wait
        log.warning("the shell did not turn %s to %s; asked again in %.0f s", output_id, turn, wait)

    def _run(self) -> None:
        while True:
            with self._changed:
                while not self._stopping and not self._waiting:
                    wait = self._retry_due()
                    if self._waiting:
                        break
                    self._changed.wait(wait)
                if self._stopping:
                    return
                output_id = next(iter(self._waiting))
                turn = self._waiting.pop(output_id)
                seen = self._seen.get(output_id, 0)
            try:
                done = self._shell.turn(output_id, turn, timeout=TURN_SECONDS)
            except Exception:       # the port never raises for a turn in TURNS; the worker lives on
                log.exception("screen turn of %s to %s", output_id, turn)
                done = False
            with self._changed:
                if done and self._seen.get(output_id, 0) == seen:
                    self._confirmed[output_id] = turn
                    self._retry_at.pop(output_id, None)
                    self._backoff.pop(output_id, None)
                elif not done:
                    self._refused(output_id, turn)


__all__ = ["RETRY_FIRST", "RETRY_MOST", "TURN_SECONDS", "ScreenShell", "ScreenTurns", "turn_due"]
