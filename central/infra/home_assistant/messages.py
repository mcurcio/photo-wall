"""What a Home Assistant state says, as a room-light report or reading: the one home of Home
Assistant's scales on Central's side (brightness and rgb 0..LEVEL_MAX) and of the stated kelvin's
range check (contracts/kelvin.py KELVIN_STATED_MIN..KELVIN_STATED_MAX). Pure.

The state's shape is the contract's (contracts/home_assistant.py, decoded strictly before it gets
here). Which colour a light states is Galleria's to say (by its colour mode); this module only
converts it. A colour outside the stated range is refused on its own: the light's on/off and
brightness are still recorded (no colour value ever costs a light its brightness). Nothing here
bounds a colour to what a screen draws: the Pi does.
"""
from __future__ import annotations

from typing import Final

from pydantic import ValidationError

from central.room_light.model import ColourReading, LightReport, LightStated, ReportRefusal
from contracts import home_assistant
from contracts.home_assistant import (
    HomeAssistantColour,
    HomeAssistantColourTemperature,
    HomeAssistantIlluminance,
    HomeAssistantKelvin,
    HomeAssistantLight,
    HomeAssistantUnavailable,
)
from contracts.room_light import (
    Dimmer,
    KelvinColour,
    LightColour,
    Measured,
    Reading,
    Rgb,
    RgbColour,
    Unavailable,
)

LEVEL_MAX: Final = home_assistant.LEVEL_MAX   # Home Assistant's brightness and each rgb channel: 0..LEVEL_MAX


class MessageRefused(ValueError):
    """A state (or its colour) this module cannot read as a room-light fact, and why (`OUT_OF_RANGE`
    for a value outside the room-light range)."""

    def __init__(self, reason: ReportRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


def light_report(state: HomeAssistantLight | HomeAssistantUnavailable) -> tuple[LightReport, MessageRefused | None]:
    """The report a light's state makes: Unavailable for unavailable; else LightStated, on or off
    with `brightness_share` of its brightness (none sent: 1.0 on, 0.0 off) and its colour
    (`light_colour`; None when it sent none). A colour `light_colour` refuses is left out of the
    report and its refusal returned beside it, so the brightness is still recorded. Pure."""
    if isinstance(state, HomeAssistantUnavailable):
        return Unavailable(), None
    share = (1.0 if state.on else 0.0) if state.brightness is None else brightness_share(state.brightness)
    colour: LightColour | None = None
    refused: MessageRefused | None = None
    if state.colour is not None:
        try:
            colour = light_colour(state.colour)
        except MessageRefused as error:
            refused = error
    return LightStated(state=Dimmer(on=state.on, brightness=share), colour=colour), refused


def light_colour(colour: HomeAssistantColour) -> LightColour:
    """A light's stated colour: its kelvin as is (MessageRefused(OUT_OF_RANGE) outside the stated
    range), or its rgb as shares (`rgb_shares`; MessageRefused(OUT_OF_RANGE) for all channels 0). Pure."""
    try:
        if isinstance(colour, HomeAssistantKelvin):
            return KelvinColour(kelvin=colour.kelvin)
        return RgbColour(rgb=rgb_shares(colour.rgb))
    except ValidationError:
        raise MessageRefused(ReportRefusal.OUT_OF_RANGE) from None


def reading(state: HomeAssistantIlluminance | HomeAssistantUnavailable) -> Reading:
    """The reading an illuminance state says: Unavailable for unavailable or no number;
    MessageRefused(OUT_OF_RANGE) past the room-light range (contracts/room_light.py LUX_MAX). Pure."""
    if isinstance(state, HomeAssistantUnavailable) or state.lux is None:
        return Unavailable()
    try:   # the reading's range is Lux's own (contracts/room_light.py)
        return Measured(lux=state.lux)
    except ValidationError:
        raise MessageRefused(ReportRefusal.OUT_OF_RANGE) from None


def colour_reading(state: HomeAssistantColourTemperature | HomeAssistantUnavailable) -> ColourReading:
    """The reading a colour-temperature state says: Unavailable for unavailable or no number; its
    kelvin as is; MessageRefused(OUT_OF_RANGE) outside the stated range (contracts/kelvin.py). Pure."""
    if isinstance(state, HomeAssistantUnavailable) or state.kelvin is None:
        return Unavailable()
    try:
        return KelvinColour(kelvin=state.kelvin)
    except ValidationError:
        raise MessageRefused(ReportRefusal.OUT_OF_RANGE) from None


def brightness_share(level: int) -> float:
    """Home Assistant's brightness level as the share of the light's full output: level / LEVEL_MAX,
    read as linear output (whether a bulb's level is perceptual instead is an open question in
    docs/decisions/0028-room-light.md). Pure."""
    return level / LEVEL_MAX


def rgb_shares(rgb: tuple[int, int, int]) -> Rgb:
    """Home Assistant's rgb_color as each channel's share, 0..1 (sRGB-encoded): each / LEVEL_MAX. Pure."""
    return (rgb[0] / LEVEL_MAX, rgb[1] / LEVEL_MAX, rgb[2] / LEVEL_MAX)


__all__ = ["LEVEL_MAX", "MessageRefused", "brightness_share", "colour_reading", "light_colour", "light_report",
           "reading", "rgb_shares"]
