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

As built (.claude/errata.md, calibration targets reader R2):
- The white colour's kelvin is the colour temperature whose white (`kelvin_white`, interpolated
  between the table's rows) has the channel balance of screen white over paper white, read as a
  Photo Wall colour temperature against KELVIN_WHITE: below it the screen is warmer than the paper
  (COOLER), above it bluer (WARMER). With the paper under 6500 K light it is the kelvin difference
  itself; under warmer light it is the same step in mireds, so larger in kelvin.
- The tone's gamma is the exponent q for which the greys' shares, encoded as sRGB, best follow
  signal ** q (least squares on the encoded values), as Photo Wall's own gamma acts: gamma 2.2 q,
  its gap 2.2 (q - 1). A display following sRGB has q = 1 exactly.
- The post-software display is k (decode(signal) ** p) per channel above its black, its one p
  fitted to the greys' shares and k to the white. A knob away from neutral costs KEEP_SPREADS
  squared in the fit's units (residuals over their spreads), plus a tie-break per step, so it moves
  only when it improves the fit by about KEEP_SPREADS spreads; the four are searched one at a time
  in their own steps from neutral. The aim's white is paper white times the chain's white with
  the four neutral and the look as set, so a look that tints or dims white is never undone.
- The LUT is "named" when it acts (`operations_for` gives the operations a LUT: one named with an
  amount above 0); a LUT at amount 0 needs no table.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Final

import numpy as np

from appliance.display_host.picture import operations_for
from central.displays.views import PictureEntryView
from contracts.calibration_target import NEAR_BLACK_STEPS
from contracts.kelvin import (
    KELVIN_TABLE_MAX,
    KELVIN_TABLE_MIN,
    KELVIN_TABLE_STEP,
    KELVIN_WHITE,
    kelvin_white,
)
from contracts.lut import LutRef
from contracts.node_frame import PictureOperations, srgb_to_linear
from contracts.node_output import OUTPUT_IDS
from contracts.node_picture import (
    GAMMA_TENTHS_MAX,
    GAMMA_TENTHS_MIN,
    GAMMA_TENTHS_NEUTRAL,
    KELVIN_MAX,
    KELVIN_MIN,
    KELVIN_NEUTRAL,
    KELVIN_STEP,
    SOFTWARE_PERCENT_MAX,
    SOFTWARE_PERCENT_MIN,
    SOFTWARE_PERCENT_NEUTRAL,
    SoftwarePicture,
)
from contracts.tuning import TestPattern
from player.geometry import encode_srgb, final_pixel

from .model import (
    KEEP_SPREADS,
    Direction,
    PhotoKind,
    PostSoftwareSuggestion,
    PreProperty,
    PreSoftwareStep,
    PreSoftwareSuggestion,
    Reading,
    Suggestion,
    SuggestionRefusal,
    SuggestionRefused,
    ToneStep,
    Unit,
)

LUMINANCE: Final = np.array([0.2126, 0.7152, 0.0722])      # Rec. 709 / sRGB Y of linear RGB
KELVIN_FINE: Final = 10                     # the kelvin fit's grid between the table's rows
_SMALLEST_SPREAD: Final = 1e-4              # a spread reported as 0 (two identical copies) is this
_TIE_BREAK: Final = 1e-6                    # per knob step from neutral: the nearest of equal fits
# The four knobs the residual moves (design rule 3: the look never moves), each over its own range.
KNOBS: Final[dict[str, tuple[int, int, int, int]]] = {   # name -> (neutral, min, max, step)
    "brightness_percent": (SOFTWARE_PERCENT_NEUTRAL, SOFTWARE_PERCENT_MIN, SOFTWARE_PERCENT_MAX, 1),
    "contrast_percent": (SOFTWARE_PERCENT_NEUTRAL, SOFTWARE_PERCENT_MIN, SOFTWARE_PERCENT_MAX, 1),
    "colour_temperature_kelvin": (KELVIN_NEUTRAL, KELVIN_MIN, KELVIN_MAX, KELVIN_STEP),
    "gamma_tenths": (GAMMA_TENTHS_NEUTRAL, GAMMA_TENTHS_MIN, GAMMA_TENTHS_MAX, 1),
}
_ROUNDS: Final = 6


def suggest(reading: Reading, lut: bytes | None) -> Suggestion:
    """The suggestion for `reading` by its stage. `lut`: the table (contracts/lut.py LUT_BYTES) of
    the LUT the entry's software picture names, None when it names none or it could not be read;
    post-software with a LUT named and `lut` None is SuggestionRefused LUT_UNREAD (never a
    prediction without the look). Pure."""
    if reading.stage is TestPattern.TARGET_PRE_SOFTWARE:
        return _pre(reading)
    return _post(reading, lut)


def software_of(entry: PictureEntryView) -> SoftwarePicture:
    """The entry's software picture as the Pi gets it (its LUT named by profile and recipe). Pure."""
    view = entry.picture.software
    lut = None
    if view.lut is not None:
        if entry.picture.lut_recipe_id is None:
            raise ValueError("entry_lut_recipe")
        lut = LutRef(view.lut.profile_sha256, entry.picture.lut_recipe_id)
    return SoftwarePicture(
        brightness_percent=view.brightness_percent, contrast_percent=view.contrast_percent,
        colour_temperature_kelvin=view.colour_temperature_kelvin, gamma_tenths=view.gamma_tenths,
        saturation_percent=view.saturation_percent, highlight_rolloff_percent=view.highlight_rolloff_percent,
        black_floor_tenths=view.black_floor_tenths, lut=lut, lut_amount_percent=view.lut_amount_percent)


def lut_acts(software: SoftwarePicture) -> LutRef | None:
    """The LUT the software picture's operations apply (named, amount above 0), or None. Pure."""
    return software.lut if _operations(software).lut is not None else None


def _operations(software: SoftwarePicture) -> PictureOperations:
    # The port and the change do not enter the pixel's arithmetic.
    return operations_for(OUTPUT_IDS[0], 1, software, None)


def _keep(gap: float, spread: float) -> bool:
    return abs(gap) <= KEEP_SPREADS * spread


# -- Pre-software ----------------------------------------------------------------------------------


def _pre(reading: Reading) -> PreSoftwareSuggestion:
    white = np.array(reading.white.value)
    level = 100 * (float(LUMINANCE @ white) - 1)
    steps = [PreSoftwareStep(PreProperty.WHITE_LEVEL,
                             Direction.KEEP if _keep(level, 100 * reading.white.spread)
                             else Direction.LOWER if level > 0 else Direction.RAISE,
                             round(abs(level), 1), Unit.PERCENT)]
    kelvin, kelvin_spread = _kelvin(white, reading.white.spread)
    gap = kelvin - KELVIN_WHITE
    steps.append(PreSoftwareStep(PreProperty.WHITE_COLOUR,
                                 Direction.KEEP if _keep(gap, kelvin_spread)
                                 else Direction.COOLER if gap < 0 else Direction.WARMER,
                                 float(round(abs(gap), -1)), Unit.KELVIN))
    if reading.kind is PhotoKind.NORMAL:
        return PreSoftwareSuggestion(tuple(steps))
    if reading.tone:
        gamma, gamma_spread = _gamma(reading.tone)
        gap = gamma - GAMMA_TENTHS_NEUTRAL / 10
        steps.append(PreSoftwareStep(PreProperty.TONE,
                                     Direction.KEEP if _keep(gap, gamma_spread)
                                     else Direction.LOWER if gap > 0 else Direction.RAISE,
                                     round(abs(gap), 2), Unit.GAMMA))
    lowest = reading.lowest_distinct
    black_gap = len(NEAR_BLACK_STEPS) if lowest is None else lowest - 1
    steps.append(PreSoftwareStep(PreProperty.BLACK, Direction.KEEP if black_gap == 0 else Direction.RAISE,
                                 float(black_gap), Unit.STEP))
    return PreSoftwareSuggestion(tuple(steps))


def _chromaticity(rgb: np.ndarray) -> np.ndarray:
    return np.log(np.array([rgb[0] / rgb[1], rgb[2] / rgb[1]]))


def _kelvin_whites() -> tuple[np.ndarray, np.ndarray]:
    """Every KELVIN_FINE kelvin over the table, its white's chromaticity (linear between rows)."""
    rows = np.arange(KELVIN_TABLE_MIN, KELVIN_TABLE_MAX + 1, KELVIN_TABLE_STEP)
    whites = np.array([kelvin_white(int(k)) for k in rows])
    fine = np.arange(KELVIN_TABLE_MIN, KELVIN_TABLE_MAX + 1, KELVIN_FINE, dtype=float)
    interpolated = np.stack([np.interp(fine, rows, whites[:, c]) for c in range(3)], axis=1)
    return fine, np.array([_chromaticity(w) for w in interpolated])


_KELVINS: Final = _kelvin_whites()


def _kelvin_of(rgb: np.ndarray) -> float:
    kelvins, chromaticities = _KELVINS
    return float(kelvins[int(np.argmin(np.sum((chromaticities - _chromaticity(rgb)) ** 2, axis=1)))])


def _kelvin(white: np.ndarray, spread: float) -> tuple[float, float]:
    """The white's kelvin and how far its spread moves it (red and blue apart by the spread)."""
    kelvin = _kelvin_of(white)
    s = max(spread, _SMALLEST_SPREAD)
    moved = [_kelvin_of(white * np.array([1 + s, 1, 1 - s])), _kelvin_of(white * np.array([1 - s, 1, 1 + s]))]
    return kelvin, max(max(abs(m - kelvin) for m in moved), KELVIN_FINE)


def _gamma(tone: Sequence[ToneStep]) -> tuple[float, float]:
    """The display's gamma read as Photo Wall's gamma would give it on an sRGB display, and how far
    the greys' spreads move it."""
    signals = np.array([step.signal for step in tone])
    shares = np.array([float(LUMINANCE @ np.array(step.share.value)) for step in tone])
    spreads = np.array([max(step.share.spread, _SMALLEST_SPREAD) for step in tone])
    gamma = _gamma_fit(signals, shares)
    moved = [_gamma_fit(signals, shares * (1 + spreads)), _gamma_fit(signals, shares * (1 - spreads))]
    return gamma, max(abs(m - gamma) for m in moved)


def _gamma_fit(signals: np.ndarray, shares: np.ndarray) -> float:
    encoded = np.array([encode_srgb(float(s)) for s in shares])
    exponents = np.linspace(0.5, 2.0, 1501)
    errors = [float(np.sum((encoded - signals ** q) ** 2)) for q in exponents]
    return float(GAMMA_TENTHS_NEUTRAL / 10 * exponents[int(np.argmin(errors))])


# -- Post-software ---------------------------------------------------------------------------------


def _post(reading: Reading, lut: bytes | None) -> PostSoftwareSuggestion | SuggestionRefused:
    now = software_of(reading.entry)
    if lut_acts(now) is not None and lut is None:
        return SuggestionRefused(SuggestionRefusal.LUT_UNREAD,
                                 "the LUT in force could not be read: give the reader the database "
                                 "address (PHOTO_WALL_READ_DATABASE_URL), or try without the LUT")
    signals = (0.0, 1.0, *(step.signal for step in reading.tone))

    def outputs(software: SoftwarePicture) -> np.ndarray:
        operations = _operations(software)
        return np.array([final_pixel((srgb_to_linear(s),) * 3, operations, inside=True, lut=lut)[:3]
                         for s in signals])

    display = _Display.fitted(outputs(now), reading)
    aim = _above_black(_decoded(outputs(_neutral_four(now))))
    cost = _cost(reading, display, aim, outputs)
    best = _neutral_four(now)
    best_cost = cost(best)
    for _ in range(_ROUNDS):
        moved = False
        for knob, (_, low, high, step) in KNOBS.items():
            for value in range(low, high + 1, step):
                trial = replace(best, **{knob: value})
                trial_cost = cost(trial)
                if trial_cost < best_cost - 1e-12:
                    best, best_cost, moved = trial, trial_cost, True
        if not moved:
            break
    return PostSoftwareSuggestion(to=best, no_change=best == now)


def _neutral_four(software: SoftwarePicture) -> SoftwarePicture:
    return replace(software, **{knob: neutral for knob, (neutral, _, _, _) in KNOBS.items()})


def _decoded(encoded: np.ndarray) -> np.ndarray:
    return np.vectorize(srgb_to_linear)(encoded)


def _above_black(light: np.ndarray) -> np.ndarray:
    """Rows: black, white, greys -> each above the black (the black row dropped)."""
    return light[1:] - light[0]


class _Display:
    """The display as this reading shows it: light above its black = k (decode(signal) ** p) per
    channel, in paper white's units."""

    def __init__(self, k: np.ndarray, p: float) -> None:
        self.k, self.p = k, p

    def light(self, outputs: np.ndarray) -> np.ndarray:
        return self.k * _above_black(_decoded(outputs) ** self.p)

    @classmethod
    def fitted(cls, outputs: np.ndarray, reading: Reading) -> _Display:
        decoded = _decoded(outputs)
        shares = np.array([step.share.value for step in reading.tone]).reshape(-1, 3)
        best_p, best_error = 1.0, float("inf")
        for p in np.linspace(0.5, 2.0, 301) if len(shares) else (1.0,):
            above = _above_black(decoded ** p)
            predicted = above[1:] / above[0]
            error = float(np.sum((np.log(np.maximum(predicted, 1e-9)) - np.log(np.maximum(shares, 1e-9))) ** 2))
            if error < best_error:
                best_p, best_error = float(p), error
        white_above = _above_black(decoded ** best_p)[0]
        return cls(np.array(reading.white.value) / np.maximum(white_above, 1e-9), best_p)


def _cost(reading: Reading, display: _Display, aim: np.ndarray,
          outputs: Callable[[SoftwarePicture], np.ndarray]) -> Callable[[SoftwarePicture], float]:
    """The fit's cost of a software picture: each predicted value's distance from the aim over its
    spread, squared and summed, plus KEEP_SPREADS squared per knob away from neutral."""
    white_spread = max(reading.white.spread, _SMALLEST_SPREAD)
    spreads = np.array([max(step.share.spread, _SMALLEST_SPREAD) for step in reading.tone])
    aim_shares = aim[1:] / aim[0]

    def cost(software: SoftwarePicture) -> float:
        light = display.light(outputs(software))
        if np.any(light[0] <= 0):
            return float("inf")             # no white above black: nothing to read against
        total =float(np.sum(((light[0] / aim[0] - 1) / white_spread) ** 2))
        if len(spreads):
            shares = light[1:] / light[0]
            total += float(np.sum(((shares / aim_shares - 1) / spreads[:, None]) ** 2))
        for knob, (neutral, _, _, step) in KNOBS.items():
            away = abs(getattr(software, knob) - neutral) // step
            if away:
                total += KEEP_SPREADS ** 2 + _TIE_BREAK * away
        return total

    return cost


__all__ = ["lut_acts", "software_of", "suggest"]
