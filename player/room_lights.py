"""The Player's room lights (decision 0028): the room-lights document Central writes on the hub's WALL
stream, read from the Node's WALL mirror through nodeapi's read-only `WallReader`.

It never rides the Player's control State (design rule 2): a light switching ten times redraws the
Frames ten times and moves no configuration, plan, executor revision, delivery, Readiness or Commit.
The latest good document stays: a value the codec refuses is logged and dropped, and one equal to
the held one is not drawn again (WALL is sticky and its mirror replays the last value at every
connect). Whatever WALL delivers last wins, `change` included: WALL keeps one value per key, so an
older value cannot arrive after a newer one, and a restored Central whose `change` restarted lower
is still followed. Until the first document arrives every Frame light adds none (rule 2; 0028's
costs). The bus away never blocks or stops the Player: the reader connects forever on its own
thread, and the Node's mirror keeps the last document while Central is away.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from contracts.room_light import ROOM_LIGHTS_KEY, RoomLightsDocument, decode_room_lights
from nodeapi.node import WallReader

log = logging.getLogger(__name__)

READER_COMPONENT = "player"


class RoomLights:
    """The Player's latest room-lights document: fed on the WallReader's thread, read on the GTK
    main loop; never touches the executor, the plan or the control State."""

    def __init__(self, changed: Callable[[RoomLightsDocument], None]) -> None:
        """`changed` is called with each newer document, on the offering thread."""
        self._changed = changed
        self._lock = threading.Lock()
        self._held: RoomLightsDocument | None = None

    def offer(self, key: str, value: bytes) -> None:
        """WallView callback: ROOM_LIGHTS_KEY only; a value decode_room_lights refuses is logged and
        dropped (last good stays); one equal to the held one is not drawn again; otherwise held and
        `changed` called."""
        if key != ROOM_LIGHTS_KEY:
            return
        try:
            document = decode_room_lights(value)
        except ValueError:
            log.warning("player: a room-lights document the codec refuses is dropped; the last stays")
            return
        with self._lock:
            if document == self._held:
                return
            self._held = document
        self._changed(document)

    def current(self) -> RoomLightsDocument | None:
        """The held document; None before the first."""
        with self._lock:
            return self._held


def follow_room_lights(url: str, changed: Callable[[RoomLightsDocument], None]) -> tuple[WallReader, RoomLights]:
    """The composition: WallReader("player", url=url), its wall's on_change -> `RoomLights.offer`;
    started (it returns at once)."""
    lights = RoomLights(changed)
    reader = WallReader(READER_COMPONENT, url=url)
    reader.wall.on_change(lights.offer)
    reader.start()
    return reader, lights


__all__ = ["READER_COMPONENT", "RoomLights", "follow_room_lights"]
