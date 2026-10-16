"""What a Home Assistant message says, as a room-light state or reading: the one home of Home
Assistant's payload shapes and scales. Pure.

A light's payload: strict JSON (contracts.strict_json) of at most MESSAGE_BYTES, an object with
"state" ("on", "off", "unavailable" or "unknown") and optional "brightness" (an int 0..255) and
"rgb" ([r, g, b], ints 0..255); other keys are ignored (Home Assistant may add attributes). An
illuminance payload: {"lux": <number 0..LUX_MAX>} or {"state": "unavailable" | "unknown"}.
"unknown" is read as unavailable.
"""
from __future__ import annotations

import math
from typing import Final

from central.infra.home_assistant.topics import MESSAGE_BYTES
from central.room_light.model import ReportRefusal
from contracts.room_light import LUX_MAX, ColourDimmer, Dimmer, Measured, Rgb, Unavailable
from contracts.strict_json import loads_object

LEVEL_MAX: Final = 255      # Home Assistant's brightness and each rgb channel: 0..LEVEL_MAX


class MessageRefused(ValueError):
    """A payload this module cannot read, and why (`ReportRefusal.UNREADABLE` for a shape it does
    not know, `OUT_OF_RANGE` for a value outside its range)."""

    def __init__(self, reason: ReportRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


def light_state(raw: bytes) -> Dimmer | ColourDimmer | Unavailable:
    """The state a light's payload reports: Unavailable for unavailable or unknown; on or off with
    `brightness_share` of its brightness (none sent: 1.0 on, 0.0 off); a ColourDimmer when rgb was
    sent (`rgb_drive`), else a Dimmer. MessageRefused otherwise."""
    value = _message(raw)
    state = value.get("state")
    if state not in ("on", "off", "unavailable", "unknown"):
        raise MessageRefused(ReportRefusal.UNREADABLE)
    if state in ("unavailable", "unknown"):
        return Unavailable()
    brightness, rgb = value.get("brightness"), value.get("rgb")
    if brightness is not None and not _level(brightness):
        raise MessageRefused(ReportRefusal.OUT_OF_RANGE)
    if rgb is not None and not (isinstance(rgb, list) and len(rgb) == 3 and all(_level(each) for each in rgb)):
        raise MessageRefused(ReportRefusal.OUT_OF_RANGE)
    on = state == "on"
    share = (1.0 if on else 0.0) if brightness is None else brightness_share(brightness)
    if rgb is None:
        return Dimmer(on=on, brightness=share)
    return ColourDimmer(on=on, brightness=share, rgb=rgb_drive((rgb[0], rgb[1], rgb[2])))


def illuminance(raw: bytes) -> Measured | Unavailable:
    """The reading an illuminance payload reports. MessageRefused otherwise."""
    value = _message(raw)
    if "lux" in value:
        lux = value["lux"]
        if type(lux) not in (int, float):
            raise MessageRefused(ReportRefusal.UNREADABLE)
        if not math.isfinite(lux) or not 0 <= lux <= LUX_MAX:
            raise MessageRefused(ReportRefusal.OUT_OF_RANGE)
        return Measured(lux=float(lux))
    if value.get("state") in ("unavailable", "unknown"):
        return Unavailable()
    raise MessageRefused(ReportRefusal.UNREADABLE)


def brightness_share(level: int) -> float:
    """Home Assistant's brightness level as the share of the light's full output: level / LEVEL_MAX
    (read as linear output, as the delivery shipped; see the run's notes on whether it is
    perceptual). Pure."""
    return level / LEVEL_MAX


def rgb_drive(rgb: tuple[int, int, int]) -> Rgb:
    """Home Assistant's rgb_color as each channel's drive, 0..1: each / LEVEL_MAX (as shipped). Pure."""
    return (rgb[0] / LEVEL_MAX, rgb[1] / LEVEL_MAX, rgb[2] / LEVEL_MAX)


def _message(raw: bytes) -> dict[str, object]:
    """The payload's JSON object: strict JSON of at most MESSAGE_BYTES; MessageRefused(UNREADABLE)."""
    value = loads_object(raw, max_bytes=MESSAGE_BYTES)
    if value is None:
        raise MessageRefused(ReportRefusal.UNREADABLE)
    return value


def _level(value: object) -> bool:
    return type(value) is int and 0 <= value <= LEVEL_MAX


__all__ = ["LEVEL_MAX", "MessageRefused", "brightness_share", "illuminance", "light_state", "rgb_drive"]
