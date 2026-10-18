"""The sRGB transfer curve: the one home of the decode every side works its colour in (the Player's
final pass, the Pi's white balance, a room light's kelvin white). Stdlib only: the Pi runs it, and
contracts/kelvin.py and contracts/node_frame.py both import it without importing each other.
"""
from __future__ import annotations


def srgb_to_linear(value: float) -> float:
    """An sRGB-encoded channel (0..1, clamped) in linear light (IEC 61966-2-1). Pure."""
    value = min(1.0, max(0.0, value))
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


__all__ = ["srgb_to_linear"]
