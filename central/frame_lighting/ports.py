"""What Frame lighting's writers, and the house's, depend on: two ports, each called inside the
writer's own transaction. central/infra/frame_lighting_store.py implements them over PostgreSQL
(`PgFrameLighting`, `PgHouseLock`), both on the one lock."""
from __future__ import annotations

from typing import Protocol

from central.frame_lighting.model import FramesNaming
from central.kernel.transactions import Transaction
from contracts.frame_lighting import FrameLighting
from contracts.node_picture import DisplayLight


class FrameLightingCheck(Protocol):
    """The check every writer of a Frame's lighting goes through (Position's trial begin and edit,
    Restore, the Registry's new Frame), under the house's lock shared (HouseLock), so no removal
    commits between a check and its write, and two checks never wait for each other."""

    def fitted_within(self, tx: Transaction, lighting: FrameLighting) -> FrameLighting:
        """`lighting` checked against the house now (`policy.check`) and stamped with today's
        recipe (`policy.stamped`), under the lock shared; FrameLightingRefused(code) when it does not
        fit."""
        ...

    def new_frame_within(self, tx: Transaction, display: DisplayLight) -> FrameLighting:
        """The lighting a new Frame starts with (`policy.new_frame_lighting` over the house now),
        under the lock shared."""
        ...


class HouseLock(Protocol):
    """The house's one lock, and the one "which Frames name it" query, for every writer or remover of
    what a Frame's lighting names (House › Lights' room lights and Sensors, the words their reporters
    send, the paper profiles list): a change takes the lock exclusive, first; a Frame-lighting check
    shares it."""

    def changing_within(self, tx: Transaction) -> None:
        """Take the lock exclusive for `tx`: the first step of every change to the house (a room
        light, a Sensor, a reporter's word, a paper profile)."""
        ...

    def naming_within(self, tx: Transaction) -> FramesNaming:
        """Which Frames' saved lighting or open trial names each room light and paper profile, under
        the lock shared (a remover has taken it exclusive first, `changing_within`)."""
        ...


__all__ = ["FrameLightingCheck", "HouseLock"]
