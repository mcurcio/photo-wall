"""The Player's own line on its Node's bus (0017 E7/E8 pulled into #106, owner Q3 "yes",
2026-10-16; 0017 C4 amended: the Player declares its own line). The Player is a component on its
bus: it declares the `player` store line (contracts.node_link.STORE_LINES, declarer "player") with
the slice its release ships (`player_slice()`), writes `birth`, and holds three state keys anyone reads:
`frames` (contracts/node_state.py FrameReports: per Frame, the binding generation it draws, its
Output, whether it is showing, the light on the Frame at its centre as last drawn),
`room-lights-drawn` (RoomLightsDrawn, moved here from the HTTP control link; re-put at least every
ROOM_LIGHTS_DRAWN_REPEAT_SECONDS so Central judges it by age) and `impeded` (contracts/node_impeded.py
ImpededFrames: the Frames Central's doubt blacks now, judged by player/impediment.py; read by Display
Host's controller for Central's card and by Central for the Frame page's "Central's error").

The session never blocks the Player: it connects forever on its own thread; a put is held and
published when attached, and every attach re-puts what is held (a bus restart starts empty). Central
reads the line through its show-pipe link; Home Assistant reads it through the Pi's read-only door.
"""
from __future__ import annotations

from typing import Final

from contracts.node_impeded import IMPEDED_BYTES, IMPEDED_KEY, ImpededFrames, encode_impeded
from contracts.node_link import NODE_BUS_URL, STORE_LINES
from contracts.node_state import (
    FRAME_REPORTS_BYTES,
    FRAMES_KEY,
    PLAYER_COMPONENT,
    PLAYER_LINE,
    PLAYER_SCHEMA_MAJOR,
    ROOM_LIGHTS_DRAWN_KEY,
    FrameReports,
    encode_frame_reports,
)
from contracts.room_light import ROOM_LIGHTS_DRAWN_BYTES, RoomLightsDrawn
from contracts.wire_codec import encode
from nodeapi.buffers import KeyTable, Slice, event_buffer, state_bucket
from nodeapi.node import NodeSession, Release

PLAYER_VERSION: Final = "1.0.0"   # the Player component's API version (its birth's `version`)
PLAYER_STATE: Final = KeyTable({FRAMES_KEY: FRAME_REPORTS_BYTES, ROOM_LIGHTS_DRAWN_KEY: ROOM_LIGHTS_DRAWN_BYTES,
                                IMPEDED_KEY: IMPEDED_BYTES})


def player_slice() -> Slice:
    """The Player line's slice: a circular record buffer (method calls' records) and the state bucket
    of PLAYER_STATE, the record buffer taking every byte of the line the bucket leaves."""
    state = state_bucket(PLAYER_LINE, PLAYER_STATE)
    records = event_buffer(PLAYER_LINE, "record", STORE_LINES[PLAYER_LINE].max_bytes - state.max_bytes)
    return Slice(PLAYER_LINE, (records, state))


class PlayerLine:
    """The Player's session on its bus (nodeapi.node NodeSession("player", player_slice(), ...))."""

    def __init__(self, release_digest: str, *, url: str = NODE_BUS_URL) -> None:
        """Not started. Its birth is Release(PLAYER_VERSION, release_digest, {"player": PLAYER_SCHEMA_MAJOR});
        `release_digest` is the running app environment's sha256 ("unknown" when the Player cannot tell)."""
        self._session = NodeSession(PLAYER_COMPONENT, player_slice(),
                                    Release(PLAYER_VERSION, release_digest, {PLAYER_LINE: PLAYER_SCHEMA_MAJOR}),
                                    url=url)

    def start(self) -> None:
        """Connect forever on its own thread; returns at once."""
        self._session.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._session.stop(timeout)

    def put_frames(self, reports: FrameReports) -> None:
        """Hold `reports` as `frames` and publish it when attached; never blocks."""
        self._session.state.put(FRAMES_KEY, encode_frame_reports(reports))

    def put_drawn(self, drawn: RoomLightsDrawn) -> None:
        """Hold `drawn` as `room-lights-drawn` and publish it when attached, again even when equal
        (the repeat Central judges by age); never blocks."""
        self._session.state.put(ROOM_LIGHTS_DRAWN_KEY, encode(drawn.model_dump(mode="json"), ROOM_LIGHTS_DRAWN_BYTES,
                                                              "room_lights_drawn_too_large"))

    def put_impeded(self, frames: ImpededFrames) -> None:
        """Hold `frames` as `impeded` (whole: a Frame absent is not impeded) and publish it when
        attached; never blocks."""
        self._session.state.put(IMPEDED_KEY, encode_impeded(frames))


__all__ = ["PLAYER_STATE", "PLAYER_VERSION", "PlayerLine", "player_slice"]
