"""The broker's probe socket on a Pi.

The probe channel (below) is the Player's one exchange with the broker: progress probes and the
facts the health judge reads. Central reads none of it (decision 0034, S4c).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from contracts.node_protocol import digest
from contracts.strict_json import loads_object

MAX_NODE_LINK_BYTES = 8192
# The broker's probe socket, the one home of its path: the broker binds it, and the Player's unit
# sees its directory at PLAYER_PROBE_SOCKET's (appliance/apps/process_linux.py binds one onto the
# other), where the Player connects.
PROBE_SOCKET: Final = "/run/photo-wall-app-proof/app-link.sock"
PLAYER_PROBE_SOCKET: Final = "/run/photo-wall-client/app-link.sock"


def _json(value: dict) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_NODE_LINK_BYTES:
        raise ValueError("node_link_too_large")
    return raw


NODE_APP_LINK_RESULTS = ("accepted", "recorded", "refused")


def encode_node_app_link_result(status: str) -> bytes:
    """The broker's last packet of an app-link exchange.

    `accepted`: the broker holds the proof locally (Central delivery is its
    own concern); `recorded`: Central recorded it; `refused`: no proof.
    """
    if not isinstance(status, str) or status not in NODE_APP_LINK_RESULTS:
        raise ValueError("app_link_result_invalid")
    return _json({"schema": 2, "kind": "result", "status": status})


def parse_node_app_link_result(raw: bytes) -> str:
    value = loads_object(raw, max_bytes=MAX_NODE_LINK_BYTES)
    if (value is None or set(value) != {"schema", "kind", "status"}
            or type(value["schema"]) is not int or value["schema"] != 2 or value["kind"] != "result"
            or not isinstance(value["status"], str) or value["status"] not in NODE_APP_LINK_RESULTS):
        raise ValueError("app_link_result_invalid")
    return value["status"]


# Progress-probe channel. The Player opens it on the broker's probe socket and speaks
# first (`probe_open`); the broker then sends `probe`, and the Player answers each probe it got to on its control queue (`probe_answer`).
# Once per channel the Player also reports its GL renderer (`renderer`), a fact
# for the health judge. The Player also states, on change and at each channel
# open, which of its Frames are black when content is wanted (`frames_black`),
# another fact for the judge. Every packet is {"schema": 2, "kind": ...} and
# carries no other authority.

MAX_RENDERER = 128  # GL_RENDERER is a short driver name ("V3D 7.1.10.2", "llvmpipe (LLVM 19.1.7, 128 bits)")
MAX_FRAMES_BLACK: Final = 16
_OUTPUT_NAME = re.compile(r"[!-~]{1,64}")   # printable ASCII, no spaces


class FrameBlackCause(StrEnum):
    """Why a Frame is black while content is wanted (player/frame_black.py)."""

    NO_PLAN = "no_plan"
    NO_TIME = "no_time"
    NO_PHOTO = "no_photo"
    CANNOT_DRAW = "cannot_draw"
    NOT_STARTED = "not_started"
    NO_CONTENT = "no_content"
    NO_RENDERER = "no_renderer"     # the Player's renderer could not be made: it draws nothing


@dataclass(frozen=True, slots=True)
class NodeProbeV2:
    nonce: str

    def __post_init__(self) -> None:
        try:
            digest(self.nonce)
        except ValueError as exc:
            raise ValueError("probe_channel_message") from exc


@dataclass(frozen=True, slots=True)
class NodeRendererV2:
    """The Player's GL renderer string (`GL_RENDERER` of its own GL context)."""

    renderer: str

    def __post_init__(self) -> None:
        if (not isinstance(self.renderer, str) or not 0 < len(self.renderer) <= MAX_RENDERER
                or not self.renderer.isprintable()):
            raise ValueError("probe_channel_message")


@dataclass(frozen=True, slots=True)
class FrameBlack:
    """One Output the Player states black, and why."""

    output_id: str
    cause: FrameBlackCause

    def __post_init__(self) -> None:
        if (not isinstance(self.output_id, str) or not _OUTPUT_NAME.fullmatch(self.output_id)
                or not isinstance(self.cause, FrameBlackCause)):
            raise ValueError("probe_channel_message")


@dataclass(frozen=True, slots=True)
class NodeFramesBlackV2:
    """The Player's whole statement of its black Frames: unique Outputs, sorted, at most
    MAX_FRAMES_BLACK. Empty means nothing is black, never "cannot tell"."""

    frames: tuple[FrameBlack, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.frames, tuple) or len(self.frames) > MAX_FRAMES_BLACK
                or not all(isinstance(frame, FrameBlack) for frame in self.frames)):
            raise ValueError("probe_channel_message")
        outputs = [frame.output_id for frame in self.frames]
        if outputs != sorted(set(outputs)):
            raise ValueError("probe_channel_message")


class NodeFramesBlackMalformed(ValueError):
    """A `frames_black` packet that does not parse. Its own type because a bad report is not a bad
    channel: the broker drops the report and keeps probing."""


def _probe_packet(raw: bytes, *, kind: str, fields: frozenset[str]) -> dict:
    value = loads_object(raw, max_bytes=MAX_NODE_LINK_BYTES)
    if (value is None or set(value) != {"schema", "kind", *fields}
            or type(value["schema"]) is not int or value["schema"] != 2 or value["kind"] != kind):
        raise ValueError("probe_channel_message")
    return value


def encode_node_probe_open() -> bytes:
    return _json({"schema": 2, "kind": "probe_open"})


def parse_node_probe_open(raw: bytes) -> None:
    _probe_packet(raw, kind="probe_open", fields=frozenset())


def encode_node_probe(nonce: str) -> bytes:
    return _json({"schema": 2, "kind": "probe", "nonce": NodeProbeV2(nonce).nonce})


def encode_node_probe_answer(nonce: str) -> bytes:
    return _json({"schema": 2, "kind": "probe_answer", "nonce": NodeProbeV2(nonce).nonce})


def parse_node_probe_answer(raw: bytes) -> str:
    return NodeProbeV2(_probe_packet(raw, kind="probe_answer", fields=frozenset({"nonce"}))["nonce"]).nonce


def encode_node_renderer(renderer: str) -> bytes:
    return _json({"schema": 2, "kind": "renderer", "renderer": NodeRendererV2(renderer).renderer})


def encode_node_frames_black(frames: tuple[FrameBlack, ...]) -> bytes:
    return _json({"schema": 2, "kind": "frames_black",
                  "frames": [{"output": frame.output_id, "cause": frame.cause.value}
                             for frame in NodeFramesBlackV2(frames).frames]})


def _parse_frames_black(raw: bytes) -> NodeFramesBlackV2:
    try:
        entries = _probe_packet(raw, kind="frames_black", fields=frozenset({"frames"}))["frames"]
        if not isinstance(entries, list):
            raise ValueError("probe_channel_message")
        frames = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"output", "cause"}:
                raise ValueError("probe_channel_message")
            frames.append(FrameBlack(entry["output"], FrameBlackCause(entry["cause"])))
        return NodeFramesBlackV2(tuple(frames))
    except ValueError as error:     # FrameBlackCause's own too: an unknown cause
        raise NodeFramesBlackMalformed("probe_channel_message") from error


def parse_node_probe_reply(raw: bytes) -> NodeProbeV2 | NodeRendererV2 | NodeFramesBlackV2:
    """A Player → broker packet on an open probe channel: an answer, the renderer or the Frames
    black report (a malformed report raises `NodeFramesBlackMalformed`)."""
    value = loads_object(raw, max_bytes=MAX_NODE_LINK_BYTES)
    kind = value.get("kind") if value is not None else None
    if kind == "renderer":
        return NodeRendererV2(_probe_packet(raw, kind="renderer", fields=frozenset({"renderer"}))["renderer"])
    if kind == "frames_black":
        return _parse_frames_black(raw)
    return NodeProbeV2(parse_node_probe_answer(raw))


def parse_node_probe_channel_message(raw: bytes) -> NodeProbeV2:
    """A broker → Player packet on an open probe channel: a probe, the only one."""
    return NodeProbeV2(_probe_packet(raw, kind="probe", fields=frozenset({"nonce"}))["nonce"])
