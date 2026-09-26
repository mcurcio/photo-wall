"""The boot's clock record. Root stages write it (Project 1: the initramfs). Later stages read
it (Project 2: provisioning skips a second step, and the Player shows the state beside a `time`
failure). An unknown schema or state, or anything unparsable, returns None, which readers treat
as "not known to be synced". A writer that only adds fields keeps schema 1. Stdlib only, so it
runs in the initramfs."""

import json
import math
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Final, Literal

from contracts.strict_json import loads_object

CLOCK_RECORD_PATH: Final = "/run/photo-wall-clock.json"
SCHEMA: Final = 1
MAX_RECORD_BYTES: Final = 4096
MAX_TRIED: Final = 8
MAX_TEXT: Final = 128           # source, writer and each `tried` entry


class ClockState(StrEnum):
    SYNCED = "synced"        # a valid SNTP answer; the clock agrees within the threshold
    AHEAD = "ahead"          # a valid answer says the clock is ahead, and stepping back would cross the floor
    UNSYNCED = "unsynced"    # no valid answer; the clock is at max(its own value, floor)


def _text(value: object, *, optional: bool = False) -> bool:
    if value is None:
        return optional
    return (isinstance(value, str) and 0 < len(value) <= MAX_TEXT and value.isascii()
            and value.isprintable() and " " not in value)


def _number(value: object, *, optional: bool = False) -> bool:
    if value is None:
        return optional
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class ClockRecord:
    """What this boot's one clock step did. Every field is checked at construction, so a record
    that exists is one a reader can parse back: a bad value is a ValueError."""

    state: ClockState
    floor: int                  # Unix seconds
    raised_to_floor: bool
    tier: Literal["dhcp", "pool"] | None
    source: str | None          # the server address that answered
    offset: float | None        # seconds, measured after any floor raise; applied only if `stepped`
    stepped: bool
    tried: tuple[str, ...]      # "tier:server:outcome", at most 8
    writer: str                 # "netboot" (Project 2 adds "provision")
    written_at: float           # wall clock after any step

    def __post_init__(self) -> None:
        if not (isinstance(self.state, ClockState)
                and type(self.floor) is int and self.floor >= 0
                and type(self.raised_to_floor) is bool and type(self.stepped) is bool
                and self.tier in (None, "dhcp", "pool")
                and _text(self.source, optional=True) and _number(self.offset, optional=True)
                and isinstance(self.tried, tuple) and len(self.tried) <= MAX_TRIED
                and all(_text(entry) for entry in self.tried)
                and _text(self.writer) and _number(self.written_at)):
            raise ValueError("not a valid clock record")

    def summary(self) -> str:
        """One console token run: 'clock=<state> floor=<date> tried=<a,b>'."""
        floor = time.strftime("%Y-%m-%d", time.gmtime(self.floor))
        return f"clock={self.state} floor={floor} tried={','.join(self.tried) or 'none'}"


def encode_clock_record(record: ClockRecord) -> bytes:
    fields = asdict(record)
    fields["tried"] = list(record.tried)
    return json.dumps({"schema": SCHEMA, **fields}, allow_nan=False,
                      separators=(",", ":")).encode("ascii")


def parse_clock_record(data: bytes) -> ClockRecord | None:
    document = loads_object(data, max_bytes=MAX_RECORD_BYTES)
    if document is None or document.get("schema") != SCHEMA or type(document["schema"]) is not int:
        return None
    try:
        state = ClockState(document["state"])
        tried = document["tried"]
        if not isinstance(tried, list):
            return None
        return ClockRecord(
            state=state, floor=document["floor"], raised_to_floor=document["raised_to_floor"],
            tier=document["tier"], source=document["source"], offset=document["offset"],
            stepped=document["stepped"], tried=tuple(tried), writer=document["writer"],
            written_at=document["written_at"])
    except (KeyError, ValueError, TypeError):
        return None
