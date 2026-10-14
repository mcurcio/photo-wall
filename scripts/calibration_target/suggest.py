"""What to change, by stage (decisions 0026, 0027; owner, 2026-10-13: "the calibration has two stages:
pre-software-adjust and post-software-adjust. The best possible display calibration is one that
doesnt require any software adjustments."; their order is "the users choice -- not something that
the software enforces"). Pure.

The stage is the entry's test pattern; nothing here remembers an earlier reading or enforces an
order.

- **Pre-software** (TARGET_PRE_SOFTWARE, the screen target drawn raw): the display's own controls,
  as directions and gap sizes only (PreSoftwareStep). White level: screen white's level over paper
  white's as read, a percent gap from 1. White colour: the white's channel balance, as a colour
  temperature gap in kelvin (contracts/kelvin.py's white). Tone: the screen greys' shares against sRGB(signal), as a
  gamma gap. The white level's raise is bounded by the viewing angle (design-v2, "Angle"; measured,
  never corrected): the panel's own off-axis dimming can only make the screen read dimmer, so LOWER
  is sure at any angle, and RAISE is said only when the shortfall exceeds `angle_dimming` of the
  reading's angle; within it the step is UNSURE (take the photo squarer). Black: the lowest
  near-black code apart from raw 0 should be 1 (PLUGE, as decision 0024's Black level): raise the
  display's black (its Brightness) while it is above 1. Each other step is judged by model.py
  `judged`: KEEP only when the reading proves the gap within MATCH_TOLERANCE, RETAKE when its
  spread is too wide to tell. A NORMAL photo yields the two white steps only.
- **Post-software** (TARGET_POST_SOFTWARE, through Photo Wall's steps but the mat shadow): the
  residual, as a software picture. The display's response is taken from this reading (each grey's
  measured light against the chain's output for it now: player/geometry.py `final_pixel` over
  appliance/display_host/picture.py `operations_for` of the entry's software picture, with the
  entry's LUT table); the aim is screen white = paper white (as read) and the greys at
  sRGB(final_pixel(x; neutral brightness, contrast, colour temperature and gamma, the look as
  set)). Only those four move, each away from neutral only when that improves the fit by more than KEEP_SPREADS of the
  readings' spread (a penalty per step from neutral, so a display right on its own yields "no
  change"); saturation, the highlight rolloff, the black floor and the LUT (the print look) never
  move. The black is not judged here. Without a grey read (a NORMAL photo, or every grey dropped)
  the display's response is unknown: SuggestionRefused TONE_UNREAD, never a fit on an assumed one.
  A brightness raise for a shortfall within `angle_dimming` is not made: brightness stays at the
  entry's and `unsure` names WHITE_LEVEL. "No change" is said only when the white level, white
  colour and tone, measured as pre-software but against the aim, are each proven matched
  (`judged`); a property whose spread is too wide to tell is named in `retake`.

Library steps (owner, 2026-10-14: "Make sure that you are using standard libraries and not building
the algorithm"): the sRGB transfer function is colour-science's; both exponent fits are
scipy.optimize.minimize_scalar (bounded). Kept by hand, each with its reason: the whole-step knob
search (no library integer search fits whole steps with a cost per step from neutral, in 15 lines)
and `_kelvin_of` (it inverts Photo Wall's own kelvin table, contracts/kelvin.py; a library's
correlated colour temperature is a different scale).

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

import warnings
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Final

import numpy as np
from scipy.optimize import minimize_scalar

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
from player.geometry import final_pixel

from .model import (
    KEEP_SPREADS,
    MATCH_TOLERANCE,
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
    Verdict,
    angle_dimming,
    judged,
)

with warnings.catch_warnings():
    # colour-science warns at import that matplotlib (its plotting, unused here) is missing.
    warnings.filterwarnings("ignore", message='"Matplotlib" related API')
    import colour

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
GAMMA_FIT: Final = (1.0, 4.0)               # the display gamma the tone fit searches (Photo Wall's units)
EXPONENT_FIT: Final = (0.5, 2.0)            # the post-software display's exponent over sRGB


def suggest(reading: Reading, lut: bytes | None) -> Suggestion:
    """The suggestion for `reading` by its stage. `lut`: the table (contracts/lut.py LUT_BYTES) of
    the LUT the entry's software picture names, None when it names none or it could not be read;
    post-software with a LUT named and `lut` None is SuggestionRefused LUT_UNREAD (never a
    prediction without the look), and with no grey read TONE_UNREAD. Pure."""
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


# -- What each property measures (both stages) ---------------------------------------------------------


Measures = dict[PreProperty, tuple[float, float]]


def _measures(white: np.ndarray, spread: float, tone: Sequence[ToneStep]) -> Measures:
    """Each property's gap from its aim and how far the spreads move it, in its step's unit: the
    white level in percent (LUMINANCE of `white` less 1), its colour in kelvin (against
    KELVIN_WHITE), the tone in gamma (against GAMMA_TENTHS_NEUTRAL, when greys were read). `white`
    and the greys' shares are already over their aims (pre-software: paper white and sRGB)."""
    kelvin, kelvin_spread = _kelvin(white, spread)
    out = {PreProperty.WHITE_LEVEL: (100 * (float(LUMINANCE @ white) - 1), 100 * spread),
           PreProperty.WHITE_COLOUR: (kelvin - KELVIN_WHITE, kelvin_spread)}
    if tone:
        gamma, gamma_spread = _gamma(tone)
        out[PreProperty.TONE] = (gamma - GAMMA_TENTHS_NEUTRAL / 10, gamma_spread)
    return out


def _verdicts(measures: Measures) -> dict[PreProperty, Verdict]:
    return {prop: judged(gap, spread, MATCH_TOLERANCE[prop]) for prop, (gap, spread) in measures.items()}


# -- Pre-software ----------------------------------------------------------------------------------


def _pre(reading: Reading) -> PreSoftwareSuggestion:
    tone = () if reading.kind is PhotoKind.NORMAL else reading.tone
    measures = _measures(np.array(reading.white.value), reading.white.spread, tone)
    verdicts = _verdicts(measures)
    level, level_spread = measures[PreProperty.WHITE_LEVEL]
    steps = [PreSoftwareStep(PreProperty.WHITE_LEVEL,
                             _level_direction(level, verdicts[PreProperty.WHITE_LEVEL], reading),
                             round(abs(level), 1), Unit.PERCENT, round(level_spread, 1))]
    gap, spread = measures[PreProperty.WHITE_COLOUR]
    steps.append(PreSoftwareStep(PreProperty.WHITE_COLOUR,
                                 _direction(verdicts[PreProperty.WHITE_COLOUR],
                                            Direction.COOLER if gap < 0 else Direction.WARMER),
                                 float(round(abs(gap), -1)), Unit.KELVIN, float(round(spread, -1))))
    if reading.kind is PhotoKind.NORMAL:
        return PreSoftwareSuggestion(tuple(steps))
    if PreProperty.TONE in measures:
        gap, spread = measures[PreProperty.TONE]
        steps.append(PreSoftwareStep(PreProperty.TONE,
                                     _direction(verdicts[PreProperty.TONE],
                                                Direction.LOWER if gap > 0 else Direction.RAISE),
                                     round(abs(gap), 2), Unit.GAMMA, round(spread, 2)))
    lowest = reading.lowest_distinct
    black_gap = len(NEAR_BLACK_STEPS) if lowest is None else lowest - 1
    steps.append(PreSoftwareStep(PreProperty.BLACK, Direction.KEEP if black_gap == 0 else Direction.RAISE,
                                 float(black_gap), Unit.STEP, 0.0))
    return PreSoftwareSuggestion(tuple(steps))


def _direction(verdict: Verdict, moving: Direction) -> Direction:
    """KEEP when proven matched, RETAKE when undecided, else `moving`."""
    return {Verdict.MATCHED: Direction.KEEP, Verdict.UNDECIDED: Direction.RETAKE}.get(verdict, moving)


def _level_direction(level: float, verdict: Verdict, reading: Reading) -> Direction:
    """The white level's direction for a gap of `level` percent: KEEP or RETAKE by its verdict;
    otherwise LOWER when brighter (the angle only dims); RAISE when the shortfall exceeds what the
    viewing angle can explain, else UNSURE."""
    if level > 0 or verdict is not Verdict.OFF:
        return _direction(verdict, Direction.LOWER)
    return Direction.RAISE if not _within_angle(level, reading) else Direction.UNSURE


def _within_angle(level: float, reading: Reading) -> bool:
    """A shortfall of `level` percent (negative) the panel's off-axis dimming could explain."""
    return level < 0 and -level / 100 <= angle_dimming(reading.angle.degrees)


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
    """The gamma g in GAMMA_FIT for which the shares, encoded as sRGB, best follow
    signal ** (g / neutral) (least squares; scipy's bounded scalar minimiser)."""
    encoded = colour.cctf_encoding(np.clip(shares, 0.0, 1.0), function="sRGB")
    neutral = GAMMA_TENTHS_NEUTRAL / 10
    found = minimize_scalar(lambda g: float(np.sum((encoded - signals ** (g / neutral)) ** 2)),
                            bounds=GAMMA_FIT, method="bounded", options={"xatol": 1e-4})
    return float(found.x)


# -- Post-software ---------------------------------------------------------------------------------


def _post(reading: Reading, lut: bytes | None) -> PostSoftwareSuggestion | SuggestionRefused:
    if not reading.tone:
        return SuggestionRefused(SuggestionRefusal.TONE_UNREAD,
                                 "no grey was read (a normal photo, or every grey dropped): send a "
                                 "ProRAW photo, with what reflects moved if reflections dropped the greys")
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
    best = _searched(_neutral_four(now), cost, KNOBS)
    unsure: tuple[PreProperty, ...] = ()
    level = 100 * (float(LUMINANCE @ np.array(reading.white.value)) - 1)
    if best.brightness_percent > now.brightness_percent and _within_angle(level, reading):
        # The shortfall may be the panel's off-axis dimming: brightness is never raised for it.
        held = {knob: limits for knob, limits in KNOBS.items() if knob != "brightness_percent"}
        best = _searched(replace(_neutral_four(now), brightness_percent=now.brightness_percent), cost, held)
        unsure = (PreProperty.WHITE_LEVEL,)
    verdicts = _verdicts(_against_aim(reading, aim))
    retake = tuple(prop for prop, verdict in verdicts.items() if verdict is Verdict.UNDECIDED)
    proven = all(verdict is Verdict.MATCHED for verdict in verdicts.values())
    return PostSoftwareSuggestion(to=best, no_change=best == now and proven, unsure=unsure, retake=retake)


def _against_aim(reading: Reading, aim: np.ndarray) -> Measures:
    """The reading's measures (as pre-software's) with the white over the aim's white and each grey's
    share over the aim's, carried back onto sRGB(signal), so a reading on the aim measures 0."""
    aim_shares = aim[1:] / aim[0]
    signals = _decoded(np.array([step.signal for step in reading.tone]))
    tone = tuple(replace(step, share=replace(step.share, value=tuple(
        np.array(step.share.value) / aim_shares[index] * signals[index])))
        for index, step in enumerate(reading.tone))
    return _measures(np.array(reading.white.value) / aim[0], reading.white.spread, tone)


def _searched(start: SoftwarePicture, cost: Callable[[SoftwarePicture], float],
              knobs: dict[str, tuple[int, int, int, int]]) -> SoftwarePicture:
    """The knobs' whole steps searched one at a time from `start` for the least cost (kept by hand: no
    library integer search weighs whole steps against a cost per step from neutral)."""
    best, best_cost = start, cost(start)
    for _ in range(_ROUNDS):
        moved = False
        for knob, (_, low, high, step) in knobs.items():
            for value in range(low, high + 1, step):
                trial = replace(best, **{knob: value})
                trial_cost = cost(trial)
                if trial_cost < best_cost - 1e-12:
                    best, best_cost, moved = trial, trial_cost, True
        if not moved:
            break
    return best


def _neutral_four(software: SoftwarePicture) -> SoftwarePicture:
    return replace(software, **{knob: neutral for knob, (neutral, _, _, _) in KNOBS.items()})


def _decoded(encoded: np.ndarray) -> np.ndarray:
    return np.asarray(colour.cctf_decoding(np.asarray(encoded, np.float64), function="sRGB"))


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

        def error(p: float) -> float:
            above = _above_black(decoded ** p)
            predicted = above[1:] / above[0]
            return float(np.sum((np.log(np.maximum(predicted, 1e-9)) - np.log(np.maximum(shares, 1e-9))) ** 2))

        best_p = float(minimize_scalar(error, bounds=EXPONENT_FIT, method="bounded", options={"xatol": 1e-4}).x)
        white_above = _above_black(decoded ** best_p)[0]
        return cls(np.array(reading.white.value) / np.maximum(white_above, 1e-9), best_p)


def _cost(reading: Reading, display: _Display, aim: np.ndarray,
          outputs: Callable[[SoftwarePicture], np.ndarray]) -> Callable[[SoftwarePicture], float]:
    """The fit's cost of a software picture: each predicted value's distance from the aim over its
    spread, squared and summed, plus KEEP_SPREADS squared per knob away from neutral. The white is
    its level's distance from the aim's and its colour's."""
    white_spread = max(reading.white.spread, _SMALLEST_SPREAD)
    spreads = np.array([max(step.share.spread, _SMALLEST_SPREAD) for step in reading.tone])
    aim_shares = aim[1:] / aim[0]

    def cost(software: SoftwarePicture) -> float:
        light = display.light(outputs(software))
        if np.any(light[0] <= 0):
            return float("inf")             # no white above black: nothing to read against
        ratio = light[0] / aim[0]
        level = float(LUMINANCE @ light[0]) / float(LUMINANCE @ aim[0])
        total = ((level - 1) / white_spread) ** 2
        total += float(np.sum(((ratio / level - 1) / white_spread) ** 2))
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
