"""Reading located photos against their sent entry (decision 0026; the owner's reflection plan,
2026-10-13). Pure.

The rules, in order:
1. **Each surface against itself.** A patch's value is the median of its middle PATCH_SHARE, less
   its own surface's BLACK in its group; only the whites cross surfaces (screen white over paper
   white). Nothing is subtracted across surfaces.
2. **Reflection edges drop the group.** A group whose screen black or paper black stands above the
   median of its surface's group blacks by more than BLACK_EDGE_TOLERANCE of the white has a
   reflection edge across it: the whole group is dropped (REFLECTION_EDGE), wherever it is.
3. **Copies vote.** Per value, copies outside their tolerance of the median of the rest are dropped
   (COPY_UNLIKE_THE_REST); a value with fewer than two agreeing copies is unread
   (TOO_FEW_CLEAN_COPIES, or COPIES_DISAGREE when the copies split). Never averaged across a split.
4. **Refuse rather than guess.** Under half the white copies left: WHITE_UNREAD, `where` the dropped
   groups' places. A white patch over CLIP_SHARE clipped: CLIPPED.
5. **Two photos.** The codes must name one entry (DIFFERENT_ENTRIES); each photo's white is read
   alone first and they must agree within PHOTOS_TOLERANCE (PHOTOS_DISAGREE: a shadow over the
   whole paper shows only so); then per patch the darker of the two readings above its black is
   kept (reflections only add light) and the copies vote as one set.
6. **Normal photos** give the white only: `tone` empty, `lowest_distinct` None.

A perfectly even reflection over the whole target is unseen and harmless (both surfaces and the
viewer get it); a whole-paper shadow is unseen in one photo (decision 0026, Costs).

As built (.claude/errata.md, calibration targets reader): rule 2 compares a PAIRED group's black-to-
black difference (screen black less paper black) with the median of the groups' differences, both
ways, as the owner's plan item 2 says ("a black-to-black difference inside a group means a
reflection edge crosses it"): a reflection broader than a group lifts both blacks alike and keeps
the group. A RULER group, with no paper black, compares its screen blacks with the median of every
group's screen black. Rule 5's darker reading is taken per patch on the raw light (each photo's
light scaled to the first's by the median ratio of the groups' screen whites), so a reflection on a
black is dropped too; each patch is then read above its black as in rule 1.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from math import sqrt
from typing import Final

import cv2
import numpy as np

from central.displays.views import PictureEntryView
from contracts.calibration_target import (
    Group,
    GroupKind,
    Patch,
    Role,
    Surface,
    calibration_target,
)
from contracts.tuning import SIGNAL_LEVELS, TARGET_PATTERNS

from .locate import Located
from .model import (
    BLACK_EDGE_TOLERANCE,
    CLIP_SHARE,
    DISTINCT_SIGMAS,
    GREY_TOLERANCE,
    PATCH_SHARE,
    PHOTOS_TOLERANCE,
    WHITE_TOLERANCE,
    Dropped,
    DropReason,
    PhotoKind,
    Place,
    Reading,
    Refusal,
    Refused,
    ToneStep,
    Value,
)

_MEDIAN_ERROR: Final = 1.2533       # a median's standard error over a mean's, normal noise
_MAD_SIGMA: Final = 1.4826          # a normal spread's sigma over its median absolute deviation

PatchKey = tuple[int, int]          # (group index, patch index in the group)


@dataclass(frozen=True)
class _Light:
    """One patch's light in one photo: the median of its middle per channel (linear), the share of
    its pixels clipped, and its median's standard error (mean of the channels)."""
    rgb: np.ndarray
    clipped: float
    noise: float


def read_photos(located: tuple[Located] | tuple[Located, Located], entry: PictureEntryView) -> Reading | Refused:
    """The Reading of one or two located photos of the sent entry their code names (the caller
    fetched `entry` for located[0].code). Refused NOT_A_TARGET when entry.test_pattern is not in
    contracts/tuning.py TARGET_PATTERNS; DIFFERENT_ENTRIES when two photos' codes differ; then the
    module's rules. Two photos of different kinds (RAW and NORMAL) are read as NORMAL. Pure."""
    if not 1 <= len(located) <= 2:
        raise ValueError("located")
    if entry.test_pattern not in TARGET_PATTERNS:
        shown = "no test pattern" if entry.test_pattern is None else f"the {entry.test_pattern.value} pattern"
        return Refused(Refusal.NOT_A_TARGET, (), f"the photo's picture change showed {shown}, not a target")
    if any(other.code != located[0].code for other in located[1:]):
        return Refused(Refusal.DIFFERENT_ENTRIES, (), "the two photos show different picture changes")
    kind = PhotoKind.RAW if all(one.photo.kind is PhotoKind.RAW for one in located) else PhotoKind.NORMAL
    lights = []
    for one in located:
        light = _lights(one)
        clipped = _clipped(light)
        if clipped:
            return Refused(Refusal.CLIPPED, clipped, f"{one.photo.name}: a white reached the camera's "
                           "clip: take the photo a little darker")
        lights.append(light)
    if len(lights) == 2:
        alone = [_read(light, entry, kind) for light in lights]
        for reading in alone:
            if isinstance(reading, Refused) and reading.why is not Refusal.WHITE_UNREAD:
                return reading
        whites = [np.array(r.white.value) for r in alone if isinstance(r, Reading)]
        if len(whites) == 2:
            gap = float(np.max(np.abs(whites[1] / whites[0] - 1)))
            if gap > PHOTOS_TOLERANCE:
                return Refused(Refusal.PHOTOS_DISAGREE, (),
                               f"the two photos' whites differ by {100 * gap:.1f} %: the light on the "
                               "paper changed between them (a shadow over it?)")
        return _read(_darker(lights[0], lights[1]), entry, kind)
    return _read(lights[0], entry, kind)


# -- Sampling ---------------------------------------------------------------------------------


def _linear(located: Located) -> np.ndarray:
    pixels = located.photo.pixels
    if located.photo.kind is PhotoKind.RAW:
        return pixels
    return np.where(pixels <= 0.04045, pixels / 12.92, ((pixels + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _lights(located: Located) -> dict[PatchKey, _Light]:
    image = _linear(located)
    out = {}
    for group in calibration_target().groups:
        for index, patch in enumerate(group.patches):
            homography = located.screen if patch.surface is Surface.SCREEN else located.paper
            out[(group.index, index)] = _sample(image, located.photo.clipped, homography, patch)
    return out


def _sample(image: np.ndarray, clipped: np.ndarray, homography: np.ndarray, patch: Patch) -> _Light:
    rect = patch.rect
    inset_x, inset_y = (1 - PATCH_SHARE) / 2 * rect.width_mm, (1 - PATCH_SHARE) / 2 * rect.height_mm
    x0, y0 = rect.x_mm + inset_x, rect.y_mm + inset_y
    x1, y1 = rect.x_mm + rect.width_mm - inset_x, rect.y_mm + rect.height_mm - inset_y
    quad = cv2.perspectiveTransform(np.array([[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]], np.float64),
                                    homography)[0]
    left, top = np.floor(quad.min(axis=0)).astype(int)
    right, bottom = np.ceil(quad.max(axis=0)).astype(int)
    height, width = image.shape[:2]
    left, top = max(left, 0), max(top, 0)
    right, bottom = min(right, width), min(bottom, height)
    if right - left < 1 or bottom - top < 1:
        raise ValueError("patch_outside_photo")
    mask = np.zeros((bottom - top, right - left), np.uint8)
    cv2.fillConvexPoly(mask, np.round(quad - [left, top]).astype(np.int32), 1)
    inside = mask.astype(bool)
    if not inside.any():
        inside[inside.shape[0] // 2, inside.shape[1] // 2] = True
    pixels = image[top:bottom, left:right][inside]
    grey = pixels.mean(axis=1)
    sigma = _MAD_SIGMA * float(np.median(np.abs(grey - np.median(grey))))
    return _Light(rgb=np.median(pixels, axis=0).astype(np.float64),
                  clipped=float(clipped[top:bottom, left:right][inside].mean()),
                  noise=_MEDIAN_ERROR * sigma / sqrt(len(pixels)))


def _clipped(lights: dict[PatchKey, _Light]) -> tuple[Place, ...]:
    return tuple(_place(patch) for key, patch in _patches()
                 if patch.role is Role.WHITE and lights[key].clipped > CLIP_SHARE)


def _darker(first: dict[PatchKey, _Light], second: dict[PatchKey, _Light]) -> dict[PatchKey, _Light]:
    """Per patch the darker light of two photos, the second scaled to the first's exposure by the
    median ratio of the PAIRED groups' screen white above black (the panel's light does not depend
    on the room's)."""
    ratios = []
    for group in _paired():
        black, white = _role_key(group, Surface.SCREEN, Role.BLACK), _role_key(group, Surface.SCREEN, Role.WHITE)
        ratios.append((first[white].rgb - first[black].rgb) / (second[white].rgb - second[black].rgb))
    scale = np.median(np.array(ratios), axis=0)
    out = {}
    for key, light in first.items():
        other = second[key]
        out[key] = _Light(rgb=np.minimum(light.rgb, other.rgb * scale), clipped=max(light.clipped, other.clipped),
                          noise=max(light.noise, other.noise * float(np.mean(scale))))
    return out


# -- The layout's groups ----------------------------------------------------------------------


def _patches() -> Iterable[tuple[PatchKey, Patch]]:
    for group in calibration_target().groups:
        for index, patch in enumerate(group.patches):
            yield (group.index, index), patch


def _paired() -> tuple[Group, ...]:
    return tuple(g for g in calibration_target().groups if g.kind is GroupKind.PAIRED)


def _role_key(group: Group, surface: Surface, role: Role) -> PatchKey:
    for index, patch in enumerate(group.patches):
        if patch.surface is surface and patch.role is role:
            return (group.index, index)
    raise ValueError("layout_group")


def _place(patch: Patch) -> Place:
    return Place(patch.surface, *patch.rect.centre())


def _group_place(group: Group) -> Place:
    centres = [p.rect.centre() for p in group.patches if p.surface is Surface.SCREEN]
    return Place(Surface.SCREEN, float(np.mean([c[0] for c in centres])), float(np.mean([c[1] for c in centres])))


def _grey_key(signal: float) -> str:
    return f"grey {signal:g}"


# -- Reading ----------------------------------------------------------------------------------


def _read(lights: dict[PatchKey, _Light], entry: PictureEntryView, kind: PhotoKind) -> Reading | Refused:
    paired = _paired()
    rulers = tuple(g for g in calibration_target().groups if g.kind is GroupKind.RULER)
    dropped: list[Dropped] = []

    def above(key: PatchKey, black: PatchKey) -> np.ndarray:
        return lights[key].rgb - lights[black].rgb

    screen_white = float(np.median([above(_role_key(g, Surface.SCREEN, Role.WHITE),
                                          _role_key(g, Surface.SCREEN, Role.BLACK)).mean() for g in paired]))
    tolerance = BLACK_EDGE_TOLERANCE * screen_white
    kept_paired, kept_rulers = _edges(lights, paired, rulers, tolerance, dropped)

    white_copies = []
    for group in kept_paired:
        screen = above(_role_key(group, Surface.SCREEN, Role.WHITE), _role_key(group, Surface.SCREEN, Role.BLACK))
        paper = above(_role_key(group, Surface.PAPER, Role.WHITE), _role_key(group, Surface.PAPER, Role.BLACK))
        white_copies.append((screen / paper, _group_place(group)))
    white, white_drops = _vote("white", white_copies, _white_close)
    dropped.extend(white_drops)
    if white is None or 2 * white.copies < len(paired):
        where = tuple(dict.fromkeys(d.where for d in dropped if d.value in ("group", "white")))
        kept = 0 if white is None else white.copies
        return Refused(Refusal.WHITE_UNREAD, where,
                       f"{kept} of {len(paired)} white copies agree: move what reflects or shades there")
    if kind is PhotoKind.NORMAL:
        return Reading(entry=entry, stage=entry.test_pattern, kind=kind, white=white, tone=(),
                       lowest_distinct=None, dropped=tuple(dropped))

    tone = []
    signals = sorted({p.signal for g in paired for p in g.patches if p.role is Role.GREY}, reverse=True)
    for signal in signals:
        copies = []
        for group in kept_paired:
            grey = next(i for i, p in enumerate(group.patches) if p.role is Role.GREY)
            if group.patches[grey].signal != signal:
                continue
            black = _role_key(group, Surface.SCREEN, Role.BLACK)
            share = above((group.index, grey), black) / above(_role_key(group, Surface.SCREEN, Role.WHITE), black)
            copies.append((share, _place(group.patches[grey])))
        value, drops = _vote(_grey_key(signal), copies, _grey_close)
        dropped.extend(drops)
        if value is not None:
            tone.append(ToneStep(signal=signal, share=value))
    return Reading(entry=entry, stage=entry.test_pattern, kind=kind, white=white, tone=tuple(tone),
                   lowest_distinct=_lowest_distinct(lights, kept_rulers), dropped=tuple(dropped))


def _edges(lights: dict[PatchKey, _Light], paired: Sequence[Group], rulers: Sequence[Group],
           tolerance: float, dropped: list[Dropped]) -> tuple[list[Group], list[Group]]:
    """Rule 2: the groups kept; each dropped one recorded as REFLECTION_EDGE."""
    def light(key: PatchKey) -> float:
        return float(lights[key].rgb.mean())

    differences = {g.index: light(_role_key(g, Surface.SCREEN, Role.BLACK)) - light(_role_key(g, Surface.PAPER, Role.BLACK))
                   for g in paired}
    middle = float(np.median(list(differences.values())))
    screen_blacks = [light(_role_key(g, Surface.SCREEN, Role.BLACK)) for g in paired]
    ruler_blacks = {g.index: [light((g.index, i)) for i, p in enumerate(g.patches) if p.role is Role.BLACK]
                    for g in rulers}
    black = float(np.median(screen_blacks + [b for blacks in ruler_blacks.values() for b in blacks]))
    kept_paired, kept_rulers = [], []
    for group in paired:
        if abs(differences[group.index] - middle) > tolerance:
            dropped.append(Dropped("group", _group_place(group), DropReason.REFLECTION_EDGE))
        else:
            kept_paired.append(group)
    for group in rulers:
        if any(abs(b - black) > tolerance for b in ruler_blacks[group.index]):
            dropped.append(Dropped("group", _group_place(group), DropReason.REFLECTION_EDGE))
        else:
            kept_rulers.append(group)
    return kept_paired, kept_rulers


def _white_close(copy: np.ndarray, middle: np.ndarray) -> bool:
    return bool(np.all(np.abs(copy / middle - 1) <= WHITE_TOLERANCE))


def _grey_close(copy: np.ndarray, middle: np.ndarray) -> bool:
    absolute, relative = GREY_TOLERANCE
    return bool(np.all(np.abs(copy - middle) <= absolute + relative * np.abs(middle)))


def _vote(name: str, copies: Sequence[tuple[np.ndarray, Place]],
          close: Callable[[np.ndarray, np.ndarray], bool]) -> tuple[Value | None, list[Dropped]]:
    """Rule 3: the copies within `close` of the median of all of them make the Value (their median,
    spread and count); the rest are dropped. None when fewer than two agree."""
    if len(copies) < 2:
        return None, [Dropped(name, place, DropReason.TOO_FEW_CLEAN_COPIES) for _, place in copies]
    middle = np.median(np.array([c for c, _ in copies]), axis=0)
    agree = [(c, place) for c, place in copies if close(c, middle)]
    if len(agree) < 2:
        why = DropReason.COPIES_DISAGREE if len(copies) - len(agree) >= 2 else DropReason.TOO_FEW_CLEAN_COPIES
        return None, [Dropped(name, place, why) for _, place in copies]
    drops = [Dropped(name, place, DropReason.COPY_UNLIKE_THE_REST)
             for c, place in copies if not close(c, middle)]
    values = np.array([c for c, _ in agree])
    value = np.median(values, axis=0)
    spread = float(np.max(np.abs(values / np.where(value == 0, 1, value) - 1)))
    return Value(value=tuple(float(v) for v in value), spread=spread, copies=len(agree)), drops


def _lowest_distinct(lights: dict[PatchKey, _Light], rulers: Sequence[Group]) -> int | None:
    """The lowest near-black code that, with every code above it, stands apart from its group's
    nearest raw 0 by DISTINCT_SIGMAS of their noise (the median over its copies)."""
    steps: dict[int, list[tuple[float, float]]] = {}
    for group in rulers:
        blacks = [(i, p) for i, p in enumerate(group.patches) if p.role is Role.BLACK]
        for index, patch in enumerate(group.patches):
            if patch.role is not Role.NEAR_BLACK:
                continue
            centre = np.array(patch.rect.centre())
            black, _ = min(blacks, key=lambda item: float(np.linalg.norm(np.array(item[1].rect.centre()) - centre)))
            step, zero = lights[(group.index, index)], lights[(group.index, black)]
            steps.setdefault(round(patch.signal * SIGNAL_LEVELS), []).append(
                (float((step.rgb - zero.rgb).mean()), sqrt(step.noise ** 2 + zero.noise ** 2)))
    distinct = {code: float(np.median([d for d, _ in found]))
                > DISTINCT_SIGMAS * max(n for _, n in found) / sqrt(len(found))
                for code, found in steps.items()}
    lowest = None
    for code in sorted(distinct, reverse=True):
        if not distinct[code]:
            break
        lowest = code
    return lowest


__all__ = ["read_photos"]
