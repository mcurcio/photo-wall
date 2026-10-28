"""The sRGB transfer curve: the one home of the decode every side works its colour in (the Player's
final pass, a bulb's rgb colour, the calibration target reader). Stdlib only: the Pi runs it. The
kelvin table (contracts/kelvin.py) needs no decode: Charity's rows are linear light already.
"""
from __future__ import annotations


def srgb_to_linear(value: float) -> float:
    """An sRGB-encoded channel (0..1, clamped) in linear light (IEC 61966-2-1). Pure."""
    value = min(1.0, max(0.0, value))
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


__all__ = ["srgb_to_linear"]
