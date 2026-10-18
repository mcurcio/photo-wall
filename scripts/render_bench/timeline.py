"""The tracer's plan, pure (bead B6): the steps, the plan's layers on the one Frame and the plane
strengths each moment must show. Which layers are seen and kept is the Player's own rule
(player/look.py, player/visibility.py, player/planes.py `compose`, player/controller_budget.py),
never a copy; the strength each must have is the bench's own (`expected_strength`, from bench
T3's measured plane alphas), never player/planes.py's rule, which the alpha check holds to it.
The only I/O is `tracer` reading each media file's bytes once, to name its Variant (sha256 and
byte size) as Central would."""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from contracts.display_turn import DisplayTurn
from contracts.frame_fit import FrameRectangle, place
from contracts.frame_lighting import FrameLighting, Paper
from contracts.models import FrameProfile, Layer, OutputBinding, Variant, stack_order
from contracts.photo_crop import CENTRED
from player.controller_budget import PI5, within
from player.look import look
from player.planes import Composition, FrameLayers, Screen, compose
from player.rendering import LocalLayer, OutputComposition
from player.visibility import visible_now

StepName = Literal["hold", "dissolve", "dip", "photo_to_video", "turn", "soak", "overload"]

FRAME_ID: Final = "bench-frame"
RUN_ID: Final = "render-bench"
OVERLOAD_PICTURES: Final = 5                # full-width pictures at once in the overload step
_PROFILE: Final = FrameProfile(width_px=3840, height_px=2160, diagonal_inches=55)     # 16:9
_LIGHTING: Final = FrameLighting(mat_depth_mm=0, lights=(), paper=Paper(whiteness=0.9, ink_density=2))
_RECIPE: Final = hashlib.sha256(b"render-bench").hexdigest()
_TYPES: Final = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4"}

Beneath = Literal["picture", "black"]

T3_PLANE_ALPHA: Final[dict[tuple[Beneath, float], int]] = {
    ("picture", 0.25): 16384, ("picture", 0.5): 32768, ("picture", 0.75): 49151,
    ("black", 0.25): 35199, ("black", 0.5): 48192, ("black", 0.75): 57725}
"""The plane alphas (the kernel's 16-bit values) bench T3 set for a layer drawn at alpha t whose
captures matched today's Player's mix (/Volumes/Dock/tmp/pi-render-bench/test3: rows D, a picture
over a picture, within 1 code of the hardware model; rows Q, a picture over black, within 6
codes): over a picture t itself, over black t's sRGB encoding."""


def expected_strength(alpha: float, beneath: Beneath) -> float:
    """The plane alpha the bench expects for a layer drawn at `alpha` (0..1) over `beneath`: the
    curve through T3_PLANE_ALPHA, `alpha` over a picture and over black the sRGB transfer function
    (IEC 61966-2-1), written here from the standard, not taken from the Player. Pure."""
    if beneath == "picture":
        return alpha
    return 12.92 * alpha if alpha <= 0.0031308 else 1.055 * alpha ** (1 / 2.4) - 0.055


@dataclass(frozen=True)
class Media:
    """One file the bench shows: a photo (JPEG or PNG) or a 1080p30 HEVC video (MP4), its pixel
    size and, for a video, its duration in seconds."""
    path: Path
    kind: Literal["photo", "video"]
    size: tuple[int, int]
    duration: float | None


@dataclass(frozen=True)
class Step:
    """One part of the tracer between `start` and `end` (plan seconds), with the screen's `turn`."""
    name: StepName
    start: float
    end: float
    turn: DisplayTurn


@dataclass(frozen=True)
class Tracer:
    """The whole tracer: its steps in order, the one Frame's binding (a 16:9 profile, no lights)
    and rectangle (centred, full width), and the plan's layers (contracts.models.Layer, each media
    layer's Variant named by its file's sha256 and size, its crop centred), by stack order."""
    steps: tuple[Step, ...]
    binding: OutputBinding
    rectangle: FrameRectangle
    layers: tuple[Layer, ...]


def _variant(media: Media) -> Variant:
    data = media.path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    media_type = _TYPES.get(media.path.suffix.lower())
    if media_type is None or (media_type == "video/mp4") != (media.kind == "video"):
        raise ValueError("tracer_media")
    return Variant(asset_id=f"bench-{digest[:16]}", recipe_id=_RECIPE, sha256=digest, size=len(data),
                   media_type=media_type, width=media.size[0], height=media.size[1], duration=media.duration)


class _Plan:
    """The tracer's layers as they are laid down: each starts when its picture comes and ends when
    a later step says (`end`)."""

    def __init__(self, output_id: str) -> None:
        self.output_id = output_id
        self.fields: dict[str, dict] = {}

    def add(self, key: str, variant: Variant | None, start: float, **fields) -> str:
        self.fields[key] = dict(assignment_id=key, run_id=RUN_ID, output_id=self.output_id, frame_id=FRAME_ID,
                                binding_generation=1, start=start, end=None, media_origin=start, variant=variant,
                                crop=None if variant is None else CENTRED,
                                presentation="media" if variant is not None else "black", **fields)
        return key

    def end(self, key: str, at: float, **fields) -> None:
        self.fields[key].update(end=at, **fields)

    def layers(self, last: float) -> tuple[Layer, ...]:
        made = [Layer(**{**fields, "end": last if fields["end"] is None else fields["end"]})
                for fields in self.fields.values()]
        return tuple(sorted(made, key=stack_order))


def tracer(output_id: str, photos: Sequence[Media], video: Media, *, origin: float, fade: float = 3.0,
           hold: float = 10.0, soak: float = 600.0) -> Tracer:
    """The tracer from plan time `origin`: photo 1 held; photo 2 dissolving in over it for `fade`
    seconds (a cross-fade: the outgoing stays at full); a dip (photo 2 fading out and photo 3 in,
    half of `fade` each, black between); photo 3 dissolving into `video`; the video held; the
    screen turned 90 with photo 4 dissolving in over the video; `soak` seconds of dissolves between
    the photos (the screen turned back first, as the overload needs it unturned: a turned 4K screen
    lays a full-width picture's height along its lines, which never overloads); then the overload
    step (five full-width pictures at once, the budget's cut). Each fade is followed by `hold`
    seconds. ValueError("tracer_media") unless there are at least four photos and one video, each
    of its kind. Reads each file's bytes (its Variant's name)."""
    photos = tuple(photos)
    if (len(photos) < 4 or any(photo.kind != "photo" for photo in photos) or video.kind != "video"
            or not (fade > 0 and hold > 0 and soak >= 0)):
        raise ValueError("tracer_media")
    variants = [_variant(photo) for photo in photos]
    clip = _variant(video)
    plan, steps = _Plan(output_id), []
    at = origin

    def step(name: StepName, length: float, turn: DisplayTurn = 0) -> None:
        nonlocal at
        steps.append(Step(name, at, at + length, turn))
        at += length

    shown = plan.add("photo-1", variants[0], at)
    step("hold", hold)
    incoming = plan.add("photo-2", variants[1], at, fade_in=fade)
    step("dissolve", fade)
    plan.end(shown, at)
    step("hold", hold)
    plan.end(incoming, at + fade / 2, fade_out=fade / 2)
    black = plan.add("dip-black", None, at, priority=-1)
    shown = plan.add("photo-3", variants[2], at + fade / 2, fade_in=fade / 2)
    step("dip", fade)
    plan.end(black, at)
    step("hold", hold)
    playing = plan.add("video", clip, at, fade_in=fade)
    step("photo_to_video", fade)
    plan.end(shown, at)
    step("hold", hold)
    step("turn", hold, 90)                       # the screen turns; the video plays on, turned
    shown = plan.add("photo-4", variants[3], at, fade_in=fade)
    step("dissolve", fade, 90)
    plan.end(playing, at)
    step("hold", hold, 90)
    soak_end, count = at + soak, 0
    if soak > 0:
        start = at + hold                        # the turn back and its handoff first
        while start + fade <= soak_end:
            count += 1
            incoming = plan.add(f"soak-{count}", variants[count % len(variants)], start, fade_in=fade)
            plan.end(shown, start + fade)
            shown, start = incoming, start + fade + hold
        step("soak", soak)
    for index in range(1, OVERLOAD_PICTURES):
        plan.add(f"overload-{index}", variants[index % len(variants)], at, opacity=0.5)
    step("overload", hold)
    return Tracer(tuple(steps), OutputBinding(output_id=output_id, frame_id=FRAME_ID, generation=1, profile=_PROFILE,
                                              lighting=_LIGHTING),
                  FrameRectangle(0.5, 0.5, 1.0), plan.layers(at))


def step_at(tracer: Tracer, at: float) -> Step:
    """The step plan time `at` falls in (the last one after the end, the first before it)."""
    for step in tracer.steps:
        if at < step.end:
            return step
    return tracer.steps[-1]


def layers_at(tracer: Tracer, at: float, lead: float = 0.0) -> tuple[Layer, ...]:
    """The plan's layers shown at plan time `at` or starting within `lead` seconds of it, by stack
    order."""
    return tuple(layer for layer in tracer.layers if layer.start - lead <= at < layer.end)


def _kept(tracer: Tracer, at: float, screen_size: tuple[int, int]) -> tuple[OutputComposition, Composition]:
    """The tracer's layers looked at plan time `at` and the one screen's composition of them, cut
    to the Pi 5's controller budget: the Player's own rules (player/look.py `look`,
    player/planes.py `compose`, player/controller_budget.py `within`)."""
    shown = tuple(LocalLayer(layer, None, 0.0, 1.0) for layer in layers_at(tracer, at))
    looked = look(OutputComposition(tracer.binding, shown, timebase=0.0), at)
    profile = tracer.binding.profile
    placement = place(tracer.rectangle, screen_size, profile.width_px / profile.height_px)
    screen = Screen(tracer.binding.output_id, screen_size, step_at(tracer, at).turn)
    (kept,), _ = within(PI5, (compose(screen, [FrameLayers(FRAME_ID, placement, looked.layers)]),))
    return looked, kept


def strengths_at(tracer: Tracer, at: float, screen_size: tuple[int, int]) -> dict[str, float]:
    """The plane strength each kept layer must have at plan time `at` (assignment id -> strength),
    bottom to top: the layers player/look.py `look` and player/planes.py `compose` show on a
    screen of `screen_size` as it hangs, cut to the Pi 5's controller budget
    (player/controller_budget.py `within`), each at `expected_strength` of its drawn alpha over
    what the nearest seen layer beneath it is (a media layer's picture, else black). Pure."""
    looked, kept = _kept(tracer, at, screen_size)
    kept_ids = {plane.layer.layer.assignment_id for plane in kept.planes}
    strengths: dict[str, float] = {}
    beneath: Beneath = "black"
    for local in visible_now(looked.layers):
        if local.layer.assignment_id in kept_ids:
            strengths[local.layer.assignment_id] = expected_strength(local.alpha, beneath)
        beneath = "picture" if local.layer.presentation == "media" else "black"
    return strengths


def ground_seen(tracer: Tracer, at: float, screen_size: tuple[int, int]) -> bool:
    """Whether the screen's ground (the Player's black beneath the planes) shows at plan time `at`
    on a screen of `screen_size` as it hangs: unless the bottom plane kept covers the whole screen
    at full strength (the tracer's pictures are all opaque: JPEG photos, video and black), the
    compositor shows the ground too. The bench's own rule of what covers, not the Player's. Pure."""
    _looked, kept = _kept(tracer, at, screen_size)
    if not kept.planes:
        return True
    bottom = kept.planes[0]
    whole = (bottom.at.x, bottom.at.y, bottom.at.width, bottom.at.height) == (0, 0, *screen_size)
    return not (whole and bottom.strength >= 1 - 1e-9)


__all__ = ["FRAME_ID", "OVERLOAD_PICTURES", "T3_PLANE_ALPHA", "Beneath", "Media", "Step", "StepName", "Tracer",
           "expected_strength", "ground_seen", "layers_at", "step_at", "strengths_at", "tracer"]
