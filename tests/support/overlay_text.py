"""A stand-in for the overlay's font measure (overlay/render.py `TextMeasure`) where cairo is not
installed: every character a fixed share of the size wide, a little wider than DejaVu Sans's
widest common glyphs, so a layout that fits this fits the real font. Real ink is proven in the
display harness (tests/native_display_smoke.py `card_ink_stays_in_the_explain_area`)."""

from __future__ import annotations

ADVANCE = 0.7   # of the font size, per character


def fake_text_width(text: str, size: float, face: str) -> float:
    return ADVANCE * size * len(text)
