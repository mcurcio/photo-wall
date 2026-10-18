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
- **When to act** (`turn_due`): once per trigger, never in a loop: a document whose turn differs
  from the last one the shell confirmed for that Output, and the Output appearing connected (at
  start, documents arrive before the shell has announced any Output, so the first ask is refused;
  after a hot-plug the shell has kept the turn, and asking again for the transform it already has
  changes nothing: the shell remodes only on a change). A refused or unanswered turn is tried
  again only at the next trigger.
- **Newest wins:** a document that arrives while the worker asks the shell replaces the one
  waiting.

Rules it does NOT own: the turn's wire value (contracts/display_turn.py `wayland_transform`), what
a turn does to the Output (the shell: a mode change, the app's grant invalidated and the Output
back under a new key, which Central's surface decisions grant again), the Player drawing its
pictures turned (player/plane_screen.py reads the turn from Weston).
"""
from __future__ import annotations

from typing import Final, Protocol

from contracts.display_turn import DisplayTurn
from contracts.node_output import OutputDocument

TURN_SECONDS: Final = 2.0      # one `turn` op's answer from the shell


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
    raise NotImplementedError("render-planes S3 bead B1")


class ScreenTurns:
    """Carries each Output's wanted turn to the shell (module rules), on its own worker thread."""

    def __init__(self, shell: ScreenShell) -> None:
        raise NotImplementedError("render-planes S3 bead B1")

    def start(self) -> None:
        """Start the worker thread (once)."""
        raise NotImplementedError("render-planes S3 bead B1")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker and join it within `timeout`."""
        raise NotImplementedError("render-planes S3 bead B1")

    def document(self, output_id: str, document: OutputDocument) -> None:
        """The newest Output document for `output_id` (runner.py `document_feed`, on the bus's
        thread): its turn is wanted from now on, and asked for when `turn_due` says. Never blocks."""
        raise NotImplementedError("render-planes S3 bead B1")

    def output(self, output_id: str, *, connected: bool) -> None:
        """`output_id`'s connection as its Output report says (runner.py `PowerObservations`, on the
        dispatch thread): appearing connected forgets the confirmed turn and asks for the wanted one
        (`turn_due` with `appeared`); disconnected forgets the confirmed turn. Never blocks."""
        raise NotImplementedError("render-planes S3 bead B1")

    def confirmed(self, output_id: str) -> DisplayTurn | None:
        """The turn the shell last confirmed for `output_id` since it last appeared; None before
        any."""
        raise NotImplementedError("render-planes S3 bead B1")


__all__ = ["TURN_SECONDS", "ScreenShell", "ScreenTurns", "turn_due"]
