"""What to change, by stage (decision 0026; owner, 2026-10-13: "the calibration has two stages:
pre-software-adjust and post-software-adjust. The best possible display calibration is one that
doesnt require any software adjustments."; their order is "the users choice -- not something that
the software enforces"). Pure.

The stage is the entry's test pattern; nothing here remembers an earlier reading or enforces an
order.

- **Pre-software** (TARGET_PRE_SOFTWARE, the screen target drawn raw): the display's own controls,
  as directions and gap sizes only (PreSoftwareStep). White level: screen white's level over paper
  white's, a percent gap. White colour: the white's channel balance, as a colour temperature gap in
  kelvin (contracts/kelvin.py's white). Tone: the screen greys' shares against sRGB(signal), as a
  gamma gap. Black: the lowest near-black code apart from raw 0 should be 1 (PLUGE, as decision
  0024's Black level): raise the display's black (its Brightness) while it is above 1. Each gap
  within KEEP_SPREADS of its spread is KEEP. A NORMAL photo yields the two white steps only.
- **Post-software** (TARGET_POST_SOFTWARE, through Photo Wall's steps but the mat shadow): the
  residual, as a software picture. The display's response is taken from this reading (each grey's
  measured light against the chain's output for it now: player/geometry.py `final_pixel` over
  appliance/display_host/picture.py `operations_for` of the entry's software picture, with the
  entry's LUT table); the aim is screen white = paper white and the greys at sRGB(final_pixel(x;
  neutral brightness, contrast, colour temperature and gamma, the look as set)). Only those four
  move, each away from neutral only when that improves the fit by more than KEEP_SPREADS of the
  readings' spread (a penalty per step from neutral, so a display right on its own yields "no
  change"); saturation, the highlight rolloff, the black floor and the LUT (the print look) never
  move. The black is not judged here.
"""
from __future__ import annotations

from .model import Reading, Suggestion


def suggest(reading: Reading, lut: bytes | None) -> Suggestion:
    """The suggestion for `reading` by its stage. `lut`: the table (contracts/lut.py LUT_BYTES) of
    the LUT the entry's software picture names, None when it names none or it could not be read;
    post-software with a LUT named and `lut` None is SuggestionRefused LUT_UNREAD (never a
    prediction without the look). Pure."""
    ...


__all__ = ["suggest"]
