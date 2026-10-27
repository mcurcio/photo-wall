"""State keys on a Node's lines that any client reads (0017 C4, C11: documents and state are readable
by any client): Central through its link, Home Assistant through the Pi's read-only door
(contracts/node_link.py NODE_DOOR_PORT). Contracts and pydantic only: the Pi runs it.

A key's subject is nodeapi.buffers `state_prefix(line) + key`, in the line's state bucket
`state_stream(line)`; a value is canonical JSON (contracts.wire_codec) and decodes strictly.

- `birth` (every component's state bucket; nodeapi.node NodeSession writes it at every attach):
  `Birth`, who the component is and which schema majors its release reads.
- `base` (the host line, HostCore): `HostBase`, the base this boot runs. A release's base carries
  the release's own tag (scripts/node_release_artifacts.py: the base is tagged with the release), so
  `base_tag` is the release this Pi booted: Home Assistant's "installed" version of a Player.
- The Player's own line (0017 E7/E8, pulled into #106; the Player declares it):
  `frames`, `FrameReports`: per Frame it draws, the binding generation it draws, its Output,
  whether it is showing and the light on the Frame at its centre as last drawn;
  `room-lights-drawn`, contracts/room_light.py `RoomLightsDrawn`: what the Player last drew its
  Frames with, repeated at least every ROOM_LIGHTS_DRAWN_REPEAT_SECONDS so Central judges it by age;
  `impeded`, contracts/node_impeded.py `ImpededFrames`: which of its Frames Central's doubt blacks now
  (its own module, so the Pi's health judge can never import it; a new key under major 1).

A Frame report counts for a Frame only while its `generation` equals Central's binding generation
for that Frame (contracts/central_api.py `DrivenBy.generation`); a reader holding another
generation treats the report as not said.
"""
from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field

from contracts.identifiers import TargetIdentifier
from contracts.room_light import Lux
from contracts.wire_codec import encode
from contracts.wire_model import Model, decoded

BIRTH_KEY: Final = "birth"                 # every state bucket: the component's birth
BIRTH_BYTES: Final = 4096                  # its largest value (nodeapi.buffers RESERVED_STATE_BYTES)
HOST_LINE: Final = "host"
HOST_BASE_KEY: Final = "base"              # the host line's state key of HostBase
HOST_BASE_BYTES: Final = 256
PLAYER_LINE: Final = "player"              # the Player's line (contracts.node_link.STORE_LINES)
PLAYER_COMPONENT: Final = "player"         # the component writing it, and its birth's name
PLAYER_SCHEMA_MAJOR: Final = 1             # the major of the Player line's keys below (its birth names it)
FRAMES_KEY: Final = "frames"               # the Player line's state key of FrameReports
FRAMES_PER_PLAYER_MAX: Final = 16
FRAME_REPORTS_BYTES: Final = 4096
ROOM_LIGHTS_DRAWN_KEY: Final = "room-lights-drawn"   # the Player line's state key of RoomLightsDrawn

OUTPUT_ID_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}"   # contracts.node_protocol `token`
OutputId = Annotated[str, Field(pattern=rf"^{OUTPUT_ID_PATTERN}$", title="OutputId")]


class Birth(Model):
    """A component's birth (nodeapi.node NodeSession): its name, its release's semantic version and
    digest, its line's pipe, the schema major per vocabulary it writes, and its slice's digest."""
    component: Annotated[str, Field(pattern=r"^[a-z]+$")]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    pipe: Literal["fleet", "show"]
    release_digest: Annotated[str, Field(min_length=1, max_length=128)]
    schema_majors: dict[str, Annotated[int, Field(ge=1)]]
    slice_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class HostBase(Model):
    """The base this boot runs (HostCore, the host line's `base` key): its tag, the release's tag."""
    base_tag: Annotated[str, Field(min_length=1, max_length=128)]


class FrameReport(Model):
    """One Frame as its Player draws it: the binding `generation` it draws (it counts only while it
    equals Central's), the HDMI port, whether its picture is on screen, and the light on the Frame at
    its centre as last drawn (None: no room-lights document drawn since the Player started)."""
    frame_id: TargetIdentifier
    generation: int = Field(ge=1)
    output_id: OutputId
    showing: bool
    light_lux: Lux | None


class FrameReports(Model):
    """Every Frame the Player draws now (level state: a Frame it no longer draws is absent)."""
    frames: tuple[FrameReport, ...] = Field(max_length=FRAMES_PER_PLAYER_MAX)


def encode_birth(birth: Birth) -> bytes:
    """Canonical JSON of at most BIRTH_BYTES."""
    return encode(birth.model_dump(mode="json"), BIRTH_BYTES, "birth_too_large")


def decode_birth(raw: bytes) -> Birth:
    """The birth `raw` encodes; ValueError("birth") for anything else."""
    return decoded(Birth, raw, BIRTH_BYTES, "birth")


def encode_host_base(base: HostBase) -> bytes:
    """Canonical JSON of at most HOST_BASE_BYTES."""
    return encode(base.model_dump(mode="json"), HOST_BASE_BYTES, "host_base_too_large")


def decode_host_base(raw: bytes) -> HostBase:
    """The base `raw` encodes; ValueError("host_base") for anything else."""
    return decoded(HostBase, raw, HOST_BASE_BYTES, "host_base")


def encode_frame_reports(reports: FrameReports) -> bytes:
    """Canonical JSON of at most FRAME_REPORTS_BYTES; ValueError("frame_reports_too_large") past it."""
    return encode(reports.model_dump(mode="json"), FRAME_REPORTS_BYTES, "frame_reports_too_large")


def decode_frame_reports(raw: bytes) -> FrameReports:
    """The reports `raw` encodes; ValueError("frame_reports") for anything else."""
    return decoded(FrameReports, raw, FRAME_REPORTS_BYTES, "frame_reports")


__all__ = [
    "BIRTH_BYTES", "BIRTH_KEY", "FRAMES_KEY", "FRAMES_PER_PLAYER_MAX", "FRAME_REPORTS_BYTES", "HOST_BASE_BYTES",
    "HOST_BASE_KEY", "HOST_LINE", "OUTPUT_ID_PATTERN", "PLAYER_COMPONENT", "PLAYER_LINE", "PLAYER_SCHEMA_MAJOR",
    "ROOM_LIGHTS_DRAWN_KEY", "Birth", "FrameReport", "FrameReports", "HostBase", "OutputId", "decode_birth",
    "decode_frame_reports", "decode_host_base", "encode_birth", "encode_frame_reports", "encode_host_base",
]
