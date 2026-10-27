"""The Player's `impeded` report: which of its Frames Central's doubt blacks now (the Frame shows
Central's error when it blacks the Frame; BUILD.md, owner 2026-10-27: "1&2&3 all have the same
answer: when the issue induces blackness on the frame"). Contracts and pydantic only: the Pi runs it.

Its own module, apart from contracts/node_state.py, so the Pi's health judge can be held unable to
import it (pyproject.toml, "Health never reads the Player's impeded report"): Central's error never
goes on the health line, and a Player crash never reaches the health judge through it.

- IMPEDED_KEY on the Player's line (contracts/node_state.py PLAYER_LINE, schema major
  PLAYER_SCHEMA_MAJOR, unchanged: a new key under major 1, so a reader that knows only the older keys
  ignores it): `ImpededFrames`, level state, restated whole at every attach and on every change. A
  Frame absent is not impeded. Written by the Player (player/impediment.py judges each Frame);
  read by Display Host's controller (only the admitted run's report counts; it composes Central's
  card, contracts/node_output.py `CentralCard`) and by Central (the Frame page's "Central's error").

A Frame is impeded when the light the Player drew at its centre is exactly 0 lx AND at least one
room light its lighting names carries a doubt in the room-lights document the Player drew with
(contracts/room_light.py `CentralWords`). Rules this module owns: the key, the shapes, their caps
and codecs. Rules it does NOT own: the judgement (player/impediment.py), what Central's words say
(central/room_light/policy.py), when the card shows (appliance/display_host/central_card.py).
"""
from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field

from contracts.frame_lighting import FRAME_LIGHTS_MAX
from contracts.identifiers import TargetIdentifier
from contracts.node_state import FRAMES_PER_PLAYER_MAX, OutputId
from contracts.room_light import CentralWords, RoomLightId
from contracts.wire_codec import encode
from contracts.wire_model import Model, decoded

IMPEDED_KEY: Final = "impeded"          # the Player line's state key of ImpededFrames
IMPEDED_BYTES: Final = 24576            # its largest encoding (FRAMES_PER_PLAYER_MAX Frames at their longest)


GRANT_ID_PATTERN: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"   # a UUID's text, lower case
GrantId = Annotated[str, Field(pattern=rf"^{GRANT_ID_PATTERN}$", title="GrantId")]


class Impeded(Model):
    """One Frame Central's doubt blacks now: the Frame, its HDMI port (today one Frame per Output,
    its one bound region), the shell grant it was drawn under (the frame protocol's `grant_frame`
    grant_id: Display Host's controller counts the entry only while that grant is the one admitted on
    the Output, so an ended or older run's report is never shown: identity, never time), the doubted
    room lights its lighting names (in id order, non-empty), and the first of those lights'
    CentralWords, verbatim."""
    frame_id: TargetIdentifier
    output_id: OutputId
    grant_id: GrantId
    lights: Annotated[tuple[RoomLightId, ...], Field(min_length=1, max_length=FRAME_LIGHTS_MAX)]
    words: CentralWords


class ImpededFrames(Model):
    """Every Frame of this Player that is impeded now (level state)."""
    frames: tuple[Impeded, ...] = Field(max_length=FRAMES_PER_PLAYER_MAX)


def encode_impeded(frames: ImpededFrames) -> bytes:
    """Canonical JSON of at most IMPEDED_BYTES; ValueError("impeded_too_large") past it (never for a
    valid value)."""
    return encode(frames.model_dump(mode="json"), IMPEDED_BYTES, "impeded_too_large")


def decode_impeded(raw: bytes) -> ImpededFrames:
    """The report `raw` encodes; ValueError("impeded") for anything else."""
    return decoded(ImpededFrames, raw, IMPEDED_BYTES, "impeded")


__all__ = ["GRANT_ID_PATTERN", "IMPEDED_BYTES", "IMPEDED_KEY", "GrantId", "Impeded", "ImpededFrames", "decode_impeded", "encode_impeded"]
