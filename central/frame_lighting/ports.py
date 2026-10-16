"""What Frame lighting's writers depend on: one port, called inside the writer's own transaction.
central/infra/frame_lighting_store.py `PgFrameLighting` implements it over PostgreSQL."""
from __future__ import annotations

from typing import Protocol

from central.frame_lighting.model import FramesNaming
from central.kernel.transactions import Transaction
from contracts.frame_lighting import FrameLighting
from contracts.node_picture import DisplayLight


class FrameLightingCheck(Protocol):
    """Injected into every writer of a Frame's lighting (Position's trial begin and edit, Restore,
    the Registry's new Frame) and every remover of what a lighting names (House › Lights, the paper
    profiles list). Each method first takes the room-lights lock in `tx` (the row every room-light
    writer locks first), so no removal commits between a check and its write."""

    def fitted_within(self, tx: Transaction, lighting: FrameLighting) -> FrameLighting:
        """`lighting` checked against the house now (`policy.check`) and stamped with today's
        recipe (`policy.stamped`); FrameLightingRefused(code) when it does not fit."""
        ...

    def new_frame_within(self, tx: Transaction, display: DisplayLight) -> FrameLighting:
        """The lighting a new Frame starts with (`policy.new_frame_lighting` over the house now)."""
        ...

    def naming_within(self, tx: Transaction) -> FramesNaming:
        """Which Frames' saved lighting or open trial names each room light and paper profile: the
        one query a removal asks before it removes."""
        ...


__all__ = ["FrameLightingCheck"]
