"""The Display picture's stored columns (decision 0022, the picture split; migrations 092-094): the
one home of how a DisplayPicture is laid out in a displays row, a picture session's draft and a
picture history entry (migration 096), so central/infra/display_store.py and
central/infra/picture_history_store.py read and write it alike without importing each other.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from central.displays.lut import LUT_RECIPE_ID
from central.displays.picture import DisplayPicture
from contracts.lut import LutRef
from contracts.node_picture import PICTURE_LIST, SignalRange, SoftwarePicture

# A displays row `d` gives these; `stored_picture` reads them, display_store `write_picture` writes
# them. A session's draft and a history entry's picture are stored under the same names.
PICTURE_FIELDS: Final[tuple[str, ...]] = (
    "hardware_picture", "software_brightness_percent", "software_contrast_percent", "colour_temperature_kelvin",
    "gamma_tenths", "software_saturation_percent", "software_highlight_rolloff_percent",
    "software_black_floor_tenths", "software_lut_profile", "software_lut_amount_percent", "signal_range")
PICTURE_COLUMNS: Final[str] = ", ".join("d." + field for field in PICTURE_FIELDS)


def stored_picture(row: Mapping[str, Any]) -> DisplayPicture:
    """The DisplayPicture of a mapping with `PICTURE_FIELDS` (a displays row, or a session's draft).
    The LUT is stored as its paper profile's digest alone; its recipe is never stored, so it is
    always the current `LUT_RECIPE_ID`."""
    hardware = {code: int(value) for code, value in row["hardware_picture"].items()}
    lut = row["software_lut_profile"]
    return DisplayPicture({code: hardware[code] for code in PICTURE_LIST if code in hardware},
                          SoftwarePicture(row["software_brightness_percent"], row["software_contrast_percent"],
                                          row["colour_temperature_kelvin"], row["gamma_tenths"],
                                          row["software_saturation_percent"], row["software_highlight_rolloff_percent"],
                                          row["software_black_floor_tenths"],
                                          None if lut is None else LutRef(lut, LUT_RECIPE_ID),
                                          row["software_lut_amount_percent"]),
                          SignalRange(row["signal_range"]))


def picture_fields(picture: DisplayPicture) -> dict[str, Any]:
    """`picture` under the stored columns' names (`PICTURE_FIELDS`); the LUT as its profile's digest."""
    software = picture.software
    return {"hardware_picture": dict(picture.hardware),
            "software_brightness_percent": software.brightness_percent,
            "software_contrast_percent": software.contrast_percent,
            "colour_temperature_kelvin": software.colour_temperature_kelvin, "gamma_tenths": software.gamma_tenths,
            "software_saturation_percent": software.saturation_percent,
            "software_highlight_rolloff_percent": software.highlight_rolloff_percent,
            "software_black_floor_tenths": software.black_floor_tenths,
            "software_lut_profile": None if software.lut is None else software.lut.profile_sha256,
            "software_lut_amount_percent": software.lut_amount_percent,
            "signal_range": picture.signal_range.value}


__all__ = ["PICTURE_COLUMNS", "PICTURE_FIELDS", "picture_fields", "stored_picture"]
