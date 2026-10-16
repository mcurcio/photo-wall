"""What a Home Assistant state says, as a room-light state or reading: the one home of Home
Assistant's scales on Central's side (brightness and rgb 0..LEVEL_MAX). Pure.

The state's shape is the contract's (contracts/home_assistant.py, decoded strictly before it gets
here). Its colour temperature (`kelvin`) is not read: a room light's colour is the one its setup
configures (decision 0028).
"""
from __future__ import annotations

from typing import Final

from pydantic import ValidationError

from central.room_light.model import ReportRefusal
from contracts import home_assistant
from contracts.home_assistant import (
    HomeAssistantIlluminance,
    HomeAssistantLight,
    HomeAssistantUnavailable,
)
from contracts.room_light import (
    ColourDimmer,
    Dimmer,
    LightWord,
    Measured,
    Reading,
    Rgb,
    Unavailable,
)

LEVEL_MAX: Final = home_assistant.LEVEL_MAX   # Home Assistant's brightness and each rgb channel: 0..LEVEL_MAX


class MessageRefused(ValueError):
    """A state this module cannot read as a room-light fact, and why (`OUT_OF_RANGE` for a value
    outside the room-light range)."""

    def __init__(self, reason: ReportRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


def light_word(state: HomeAssistantLight | HomeAssistantUnavailable) -> LightWord:
    """The word a light's state says: Unavailable for unavailable; on or off with
    `brightness_share` of its brightness (none sent: 1.0 on, 0.0 off); a ColourDimmer when rgb was
    sent (`rgb_drive`), else a Dimmer. Kelvin is not read. Pure."""
    if isinstance(state, HomeAssistantUnavailable):
        return Unavailable()
    share = (1.0 if state.on else 0.0) if state.brightness is None else brightness_share(state.brightness)
    if state.rgb is None:
        return Dimmer(on=state.on, brightness=share)
    return ColourDimmer(on=state.on, brightness=share, rgb=rgb_drive(state.rgb))


def reading(state: HomeAssistantIlluminance | HomeAssistantUnavailable) -> Reading:
    """The reading an illuminance state says: Unavailable for unavailable or no number;
    MessageRefused(OUT_OF_RANGE) past the room-light range (contracts/room_light.py LUX_MAX). Pure."""
    if isinstance(state, HomeAssistantUnavailable) or state.lux is None:
        return Unavailable()
    try:   # the reading's range is Lux's own (contracts/room_light.py)
        return Measured(lux=state.lux)
    except ValidationError:
        raise MessageRefused(ReportRefusal.OUT_OF_RANGE) from None


def brightness_share(level: int) -> float:
    """Home Assistant's brightness level as the share of the light's full output: level / LEVEL_MAX,
    read as linear output (whether a bulb's level is perceptual instead is an open question in
    docs/decisions/0028-room-light.md). Pure."""
    return level / LEVEL_MAX


def rgb_drive(rgb: tuple[int, int, int]) -> Rgb:
    """Home Assistant's rgb_color as each channel's drive, 0..1: each / LEVEL_MAX. Pure."""
    return (rgb[0] / LEVEL_MAX, rgb[1] / LEVEL_MAX, rgb[2] / LEVEL_MAX)


__all__ = ["LEVEL_MAX", "MessageRefused", "brightness_share", "light_word", "reading", "rgb_drive"]
