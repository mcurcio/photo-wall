"""The Display picture's stored columns (decision 0022, the picture split; migrations 092-094, 099):
the one home of how a DisplayPicture is laid out in a displays row, a picture session's draft and a
picture history entry (migration 096), so central/infra/display_store.py and
central/infra/picture_history_store.py read and write it alike without importing each other.

Room light (decision 0028, migration 099) removed the print look's sliders and the Display's LUT and
added the Display's light (`matched_lux`, `panel_black`). A history entry written before 099 has no
light: `entry_picture` says so.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from central.displays.picture import DisplayPicture
from contracts.node_picture import (
    PICTURE_LIST,
    UPGRADE_DISPLAY_LIGHT,
    DisplayLight,
    SignalRange,
    SoftwarePicture,
)

# A displays row `d` gives these; `stored_picture` reads them, display_store `write_picture` writes
# them. A session's draft and a history entry's picture are stored under the same names.
PICTURE_FIELDS: Final[tuple[str, ...]] = (
    "hardware_picture", "software_contrast_percent", "colour_temperature_kelvin", "gamma_tenths", "signal_range",
    "matched_lux", "panel_black")
PICTURE_COLUMNS: Final[str] = ", ".join("d." + field for field in PICTURE_FIELDS)


def stored_picture(row: Mapping[str, Any]) -> DisplayPicture:
    """The DisplayPicture of a mapping with `PICTURE_FIELDS` (a displays row, or a session's draft)."""
    picture, recorded = entry_picture(row)
    if not recorded:
        raise ValueError("picture_light_missing")
    return picture


def entry_picture(row: Mapping[str, Any]) -> tuple[DisplayPicture, bool]:
    """(the DisplayPicture of a mapping with `PICTURE_FIELDS`, whether it records the Display's light):
    a history entry from before migration 099 has none, and reads with UPGRADE_DISPLAY_LIGHT as a
    stand-in (never a value it recorded)."""
    hardware = {code: int(value) for code, value in row["hardware_picture"].items()}
    recorded = row.get("matched_lux") is not None and row.get("panel_black") is not None
    light = (DisplayLight(matched_lux=float(row["matched_lux"]), black=float(row["panel_black"])) if recorded
             else UPGRADE_DISPLAY_LIGHT)
    return DisplayPicture({code: hardware[code] for code in PICTURE_LIST if code in hardware},
                          SoftwarePicture(contrast_percent=row["software_contrast_percent"],
                                          colour_temperature_kelvin=row["colour_temperature_kelvin"],
                                          gamma_tenths=row["gamma_tenths"]),
                          SignalRange(row["signal_range"]), light), recorded


def picture_fields(picture: DisplayPicture) -> dict[str, Any]:
    """`picture` under the stored columns' names (`PICTURE_FIELDS`)."""
    software = picture.software
    return {"hardware_picture": dict(picture.hardware),
            "software_contrast_percent": software.contrast_percent,
            "colour_temperature_kelvin": software.colour_temperature_kelvin, "gamma_tenths": software.gamma_tenths,
            "signal_range": picture.signal_range.value,
            "matched_lux": picture.light.matched_lux, "panel_black": picture.light.black}


__all__ = ["PICTURE_COLUMNS", "PICTURE_FIELDS", "entry_picture", "picture_fields", "stored_picture"]
