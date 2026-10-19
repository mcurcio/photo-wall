"""Protocol v1. No upstream identities, credentials or URLs cross this boundary."""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import BeforeValidator, Field, model_serializer, model_validator

from contracts.display_turn import DisplayTurn, hanging, is_turn
from contracts.frame_fit import (
    FrameBox,
    OutputSize,
    PictureSize,
    Placement,
    ScreenMm,
    box_size_mm,
    place_box,
)
from contracts.frame_lighting import FrameLighting

# The identifier rules' one home is contracts/identifiers.py; re-exported here under these names.
from contracts.identifiers import IDENTIFIER_PATTERN as IDENTIFIER_PATTERN
from contracts.identifiers import TARGET_ID_PATTERN as TARGET_ID_PATTERN
from contracts.identifiers import Digest as Digest
from contracts.identifiers import Identifier as Identifier
from contracts.identifiers import Instant as Instant
from contracts.identifiers import TargetIdentifier as TargetIdentifier
from contracts.node_output import DISPLAY_MM_MAX
from contracts.wire_model import Model

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class FrameProfile(Model):
    width_px: int = Field(gt=0, le=16384)
    height_px: int = Field(gt=0, le=16384)
    diagonal_inches: Positive
    video: bool = True


Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


def _exact_turn(value: object) -> DisplayTurn:
    """`value` when `is_turn(value)`, else ValueError("display_turn"): pydantic's Literal alone would
    take a JSON false as 0 and 90.0 as 90."""
    if not is_turn(value):
        raise ValueError("display_turn")
    return value


# A Display turn as a wire model's field (render redesign S2): the one pydantic form of
# contracts/display_turn.py `DisplayTurn`, checked by its `is_turn`.
ExactTurn = Annotated[DisplayTurn, BeforeValidator(_exact_turn)]


BoxSide = Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]


class PositionBox(Model):
    """A Frame's Position as a box (frame box run, .claude/runs/frame-box/page.md; owner,
    2026-10-19: "I need to be able to set width and height independently, along with the position
    of that box on the display"): `left`, `top`, `width` and `height` are fractions of its screen's
    width and height as it hangs (the Frame's turn, frames.turn). The one source of the Frame's
    shape and pixel size; the default is the whole screen. Its rule's one home is
    contracts/frame_fit.py `FrameBox` (an after-validator builds one, so a box past the screen's
    right or bottom edge is refused with FrameBox's ValueError("frame_box")); where it lands in
    pixels is `frame_placement`. `mode` names the form in stored JSON and on the wire."""
    mode: Literal["box"] = "box"
    left: Fraction = 0.0
    top: Fraction = 0.0
    width: BoxSide = 1.0
    height: BoxSide = 1.0

    @model_validator(mode="after")
    def boxed(self) -> Self:
        """`self.box()` (FrameBox's own check); its ValueError("frame_box") becomes the model's
        validation error."""
        self.box()
        return self

    def box(self) -> FrameBox:
        """The contracts/frame_fit.py `FrameBox` of these four fields. Pure."""
        return FrameBox(left=self.left, top=self.top, width=self.width, height=self.height)


class Calibration(Model):
    """A Frame's Position (the console's word): where its picture sits on its screen as it hangs,
    one box (`PositionBox`; the default is the whole screen). `revision` rises with every saved
    Position."""

    revision: int = Field(default=1, ge=1)
    fit: PositionBox = Field(default_factory=PositionBox)


# A Display's picture area in whole millimetres as built (its EDID base block's; contracts/
# frame_fit.py `ScreenMm` on the wire), each side 1..DISPLAY_MM_MAX.
DisplayMmSide = Annotated[int, Field(ge=1, le=DISPLAY_MM_MAX, strict=True)]
DisplayMm = tuple[DisplayMmSide, DisplayMmSide]


class OutputBinding(Model):
    output_id: Identifier
    frame_id: Identifier
    generation: int = Field(ge=1)
    configuration_revision: int = Field(default=1, ge=1)
    calibration: Calibration = Field(default_factory=Calibration)
    lighting: FrameLighting                             # the Frame's lights, mat and paper
    # The Display seen on this Output: its picture area as built (frame box run decision 12); None
    # when none is seen or it states none. Required: the Player draws the Frame's physical size
    # from it (`frame_size_mm` of the drawn box and the screen's turn).
    display_mm: DisplayMm | None


def frame_output(calibration: Calibration, output: OutputSize | None, turn: DisplayTurn) -> PictureSize | None:
    """The Frame output of a Frame whose Output's mode is `output` (width, height, as built) and
    whose screen hangs at `turn`: its picture's pixels, `frame_placement`'s (width, height). None
    exactly when `output` is None or has a side below 1 (no screen size is known). The box alone
    gives the Frame's shape (frame box run, .claude/runs/frame-box/page.md). Pure; the one place a
    Frame's calibration becomes its Frame output."""
    if output is None or output[0] < 1 or output[1] < 1:
        return None
    placement = frame_placement(calibration.fit, output, turn)
    return placement.width, placement.height


def frame_placement(fit: PositionBox, output: OutputSize, turn: DisplayTurn) -> Placement:
    """Where a Frame's box lands, in whole pixels, on a screen whose mode is `output` (width,
    height as built, never turned) hung at `turn`: `place_box(fit.box(), hanging(output, turn))`.
    The one home, for Central, of a Frame's box on its screen. Pure. ValueError as `hanging`
    ("display_turn", "screen_size")."""
    return place_box(fit.box(), hanging(output, turn))


def frame_size_mm(fit: PositionBox, display_mm: ScreenMm | None, turn: DisplayTurn) -> tuple[float, float] | None:
    """A Frame's physical (width, height) in millimetres as it hangs: `box_size_mm(fit.box(),
    hanging(display_mm, turn))`, where `display_mm` is the picture area of the Display seen on its
    Output as built (its EDID's; OutputBinding.display_mm, displays.width_mm and height_mm). None
    when `display_mm` is None (the Display states no size, or none is seen). The one home, for
    Central and the Player, of a Frame's physical size (frame box run). Pure. ValueError as
    `hanging` ("display_turn", "screen_size") for a known size."""
    if display_mm is None:
        return None
    return box_size_mm(fit.box(), hanging(display_mm, turn))


class IdentifyOutput(Model):
    """Short-lived operational request for a Player to identify one physical Output."""

    request_id: str = Field(min_length=1, max_length=128)
    output_id: Identifier
    authority_epoch: int = Field(ge=1)
    remaining_seconds: float = Field(gt=0, le=15, allow_inf_nan=False)

# The largest media variant a Player accepts (a prepared copy's bytes).
MAX_VARIANT_BYTES = 4 * 1024**3


class Variant(Model):
    """One prepared copy of a photo. It is named by its inputs, `asset_id` (the photo revision)
    and `recipe_id` (what sizes and encodes it), and a Player fetches it by that name at
    `GET /v1/media/{asset_id}/{recipe_id}`. `sha256` and `size` are the bytes current when the
    plan was issued; a re-make may serve other bytes of the same content, so a Player checks the
    body against the `Repr-Digest` and `Content-Length` it is served, never these."""

    asset_id: Identifier
    recipe_id: Digest
    sha256: Digest
    size: int = Field(gt=0, le=MAX_VARIANT_BYTES)
    media_type: Literal["image/jpeg", "image/png", "video/mp4"]
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    duration: Positive | None = None

    @model_validator(mode="after")
    def temporal_kind(self) -> Self:
        if (self.media_type == "video/mp4") != (self.duration is not None):
            raise ValueError("video requires duration; a still has no media duration")
        return self

    @property
    def key(self) -> str:
        """`<asset_id>.<recipe_id>`, the variant's name."""
        return f"{self.asset_id}.{self.recipe_id}"

    @property
    def path(self) -> str:
        return f"/v1/media/{self.asset_id}/{self.recipe_id}"


class PlayerConfiguration(Model):
    protocol: Literal[1] = 1
    player_id: Identifier
    authority_epoch: int = Field(ge=1)
    configuration_revision: int = Field(ge=1)
    bindings: tuple[OutputBinding, ...]
    # Unvalidated bindings remain visible for calibration but cannot execute media.
    enabled_outputs: tuple[Identifier, ...] = ()

    def authorizes(self, layer: Layer) -> bool:
        return any(b.output_id == layer.output_id and b.frame_id == layer.frame_id
                   and b.generation == layer.binding_generation
                   and b.output_id in self.enabled_outputs for b in self.bindings)

    @model_validator(mode="after")
    def coherent_outputs(self) -> Self:
        outputs = {binding.output_id for binding in self.bindings}
        if len(outputs) != len(self.bindings):
            raise ValueError("duplicate Output")
        if len({binding.frame_id for binding in self.bindings}) != len(self.bindings):
            raise ValueError("duplicate Frame")
        if len(set(self.enabled_outputs)) != len(self.enabled_outputs):
            raise ValueError("duplicate enabled Output")
        if not set(self.enabled_outputs) <= outputs:
            raise ValueError("enabled Output must be bound")
        return self


class PlayerTime(Model):
    """Small authenticated clock sample, independent of control-state delivery."""

    protocol: Literal[1] = 1
    player_id: Identifier
    authority_epoch: int = Field(ge=1)
    server_time: Instant


class PhotoCrop(Model):
    """Which part of a photo a Frame shows (roadmap 5a, run ledger .claude/runs/photo-5a.md): the
    largest window with the Frame profile's aspect, slid along the photo's one free axis. 0 keeps
    the left (or top) edge, 1 the right (or bottom), 0.5 is centred; a photo with exactly the
    profile's aspect has no free axis and the offset changes nothing. The window's aspect comes
    from the profile alone (contracts/photo_crop.py `crop_window`); Central chooses the offset
    (central/wall_plan/fit.py), the Player only draws it."""

    offset: Fraction


# What a Frame keeps once a layer has been shown, for when nothing plays on it (the Player's
# fallback, an outage included). The layer takes effect once its fade-in is over:
# "keep_this_photo" keeps this opaque still up; "keep_nothing" keeps nothing (black);
# "leave_as_is" changes nothing (an earlier layer's after-state still holds). Central plans it
# (docs/execution-contract.md "What a Frame keeps"); the Player only applies it.
AfterEnd = Literal["leave_as_is", "keep_this_photo", "keep_nothing"]


def fade_share(now: float, *, start: float, end: float, fade_in: float, fade_out: float) -> float:
    """The share of its opacity a layer shown over [start, end) has at `now`, through a fade-in
    of `fade_in` seconds from `start` and a fade-out of `fade_out` seconds to `end`: the lesser
    of the two when they overlap. The one fade rule, for Central's planned opacity
    (central/runtime.py) and the alpha the Player draws (`Layer.alpha`)."""
    faded_in = min(1.0, max(0.0, (now - start) / fade_in)) if fade_in else 1.0
    faded_out = min(1.0, max(0.0, (end - now) / fade_out)) if fade_out else 1.0
    return min(faded_in, faded_out)


class Layer(Model):
    assignment_id: Identifier
    run_id: Identifier
    output_id: Identifier
    frame_id: Identifier
    binding_generation: int = Field(ge=1)
    start: Instant
    end: Instant
    media_origin: Instant
    priority: int = 0
    root_order: int = Field(default=0, ge=0)
    admission_order: int = Field(default=0, ge=0)
    variant: Variant | None = None
    presentation: Literal["media", "black"] = "media"
    opacity: float = Field(default=1, ge=0, le=1)
    fade_in: float = Field(default=0, ge=0)
    fade_out: float = Field(default=0, ge=0)
    required: bool = True
    after_end: AfterEnd = "leave_as_is"
    # Roadmap 5a: the window this media layer draws (contracts/photo_crop.py); required on a media
    # layer, absent on a black one (run ledger .claude/runs/photo-5a.md).
    crop: PhotoCrop | None = None

    @model_validator(mode="after")
    def interval(self) -> Self:
        if self.end <= self.start:
            raise ValueError("layer end must follow start")
        if self.presentation == "media" and (self.variant is None or self.crop is None):
            raise ValueError("media layer requires an exact variant and its crop")
        if self.presentation == "black" and (self.variant is not None or self.crop is not None):
            raise ValueError("black layer cannot carry media")
        if self.fade_in + self.fade_out > self.end - self.start:
            raise ValueError("fades exceed layer interval")
        if self.after_end == "keep_this_photo" and (
            self.variant is None or self.variant.media_type == "video/mp4" or self.opacity != 1
        ):
            raise ValueError("only an opaque still may be kept")
        return self

    # The layer at an instant, each value a function of plan time alone: `position` (the media
    # time a video is sought to) and `alpha`, which changes only during `fades`. A renderer draws
    # a layer's alpha at the time of the frame it draws (player/look.py), never at a planning
    # tick's time; a video's picture is its decoder's newest sample, not a plan-time value.
    def position(self, now: float) -> float:
        position = max(0.0, now - self.media_origin)
        if self.variant and self.variant.duration:
            position %= self.variant.duration
        return position

    def alpha(self, now: float) -> float:
        """Its strength at `now`: its opacity through its fade-in and fade-out."""
        return self.opacity * fade_share(now, start=self.start, end=self.end,
                                         fade_in=self.fade_in, fade_out=self.fade_out)

    def fades(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """Its fade-in and its fade-out as half-open spans of plan time, either empty when it has
        none: `alpha` changes inside them and nowhere else, so it is its opacity between them."""
        return (self.start, self.start + self.fade_in), (self.end - self.fade_out, self.end)


# How long before a layer starts its Player prepares it (its decoder resident) and Central's cue
# may grant it: the one home of the executor's `prepare_lead`, coordination's `prepare_seconds`
# and the bound on a Scene's Transition (central/runtime.py `Scene.check_transition`), which
# keeps that long of each photo's turn free of a dissolve.
PREPARE_SECONDS: Final = 5.0


def stack_order(layer: Layer) -> tuple[int, int, int, float]:
    """The one stacking order of a Frame's layers, bottom to top: priority, then root order,
    then admission order, then start, so of two photos of one Run that overlap (a cross-fade)
    the later one is drawn over the earlier. Central's Planner orders a plan's layers by it and
    the Player's executor draws by it (player/executor.py `_compositions`)."""
    return (layer.priority, layer.root_order, layer.admission_order, layer.start)


class Plan(Model):
    protocol: Literal[1] = 1
    plan_id: Identifier
    revision: int = Field(ge=1)
    player_id: Identifier
    authority_epoch: int = Field(ge=1)
    issued_at: Instant
    valid_from: Instant
    valid_until: Instant
    bindings: tuple[OutputBinding, ...]
    layers: tuple[Layer, ...]

    @model_validator(mode="after")
    def authority(self) -> Self:
        if self.valid_until <= self.valid_from or self.issued_at > self.valid_until:
            raise ValueError("invalid plan interval")
        bindings = {b.output_id: b for b in self.bindings}
        if len(bindings) != len(self.bindings):
            raise ValueError("duplicate Output")
        if len({b.frame_id for b in self.bindings}) != len(self.bindings):
            raise ValueError("duplicate Frame")
        if len({a.assignment_id for a in self.layers}) != len(self.layers):
            raise ValueError("duplicate assignment")
        for layer in self.layers:
            binding = bindings.get(layer.output_id)
            if not binding or (layer.frame_id, layer.binding_generation) != (
                binding.frame_id, binding.generation
            ):
                raise ValueError("layer binding authority mismatch")
            if layer.end <= self.valid_from or layer.start >= self.valid_until or layer.end > self.valid_until:
                raise ValueError("layer exceeds plan validity")
        return self


class Failure(Model):
    assignment_id: Identifier
    code: Annotated[str, Field(pattern=r"^[a-z_]{1,64}$")]


class LostGrant(Model):
    """A start grant the Player saw and no longer holds: refused, or dropped since (a clock
    blip, preparation lost and regained). `readiness_sequence` is the grant's, so Central
    re-grants only the grant it still holds, once."""
    assignment_id: Identifier
    readiness_sequence: int = Field(ge=1)


class Readiness(Model):
    plan_id: Identifier
    revision: int = Field(ge=1)
    authority_epoch: int = Field(ge=1)
    sequence: int = Field(ge=1)
    secured: tuple[Identifier, ...] = ()
    prepared: tuple[Identifier, ...] = ()
    capacity_ok: bool = False
    clock_uncertainty: float = Field(ge=0)
    observed_at: Instant
    failures: tuple[Failure, ...] = ()
    # Level state, repeated in every report while true; required (no default): a Player that
    # cannot say what it lost is refused rather than left without recovery.
    lost_grants: tuple[LostGrant, ...]

    @model_validator(mode="after")
    def coherent_readiness(self) -> Self:
        failed = [f.assignment_id for f in self.failures]
        lost = [g.assignment_id for g in self.lost_grants]
        for ids in (self.secured, self.prepared, failed, lost):
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate readiness assignment")
        if not set(self.prepared) <= set(self.secured):
            raise ValueError("prepared assignments must be secured")
        if set(self.prepared) & set(failed):
            raise ValueError("failed assignment cannot report prepared playback")
        if not set(lost) <= set(self.prepared):
            raise ValueError("a lost grant must be prepared")
        return self


class BaseHealth(Model):
    """A base-image health check-in an ENROLLED device posts after its base boots
    (0012 r7). Distinct from `Readiness`: it carries no plan/assignment fields and
    the endpoint requires NO live plan offer, so an enrolled-but-unbound device can
    move the latest-verified frontier. `running_tag` is the base tag the device
    booted; Central accepts it only when it equals the tag it recorded as
    last-served to that device and `healthy` is true (the sole writer of
    known-good). `Readiness` is deliberately left untouched -- the running tag
    lives here, not there."""

    authority_epoch: int = Field(ge=1)
    sequence: int = Field(ge=1)                    # monotonicity guard: known-good only advances
    running_tag: Annotated[str, Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*$", max_length=256)]
    healthy: bool


class Commit(Model):
    plan_id: Identifier
    revision: int = Field(ge=1)
    authority_epoch: int = Field(ge=1)
    readiness_sequence: int = Field(ge=1)
    assignment_ids: tuple[Identifier, ...]
    committed_at: Instant

    @model_validator(mode="after")
    def unique_assignments(self) -> Self:
        if len(self.assignment_ids) != len(set(self.assignment_ids)):
            raise ValueError("duplicate committed assignment")
        return self


class AppliedCalibration(Model):
    """Actual operational transform attached to an app draw acknowledgment.

    This describes renderer state, not compositor or physical-pixel evidence.
    """
    calibration: Calibration
    trial_id: Identifier | None = None
    trial_generation: int | None = Field(default=None, ge=1)
    trial_sequence: int | None = Field(default=None, ge=1)
    candidate_sha256: Digest | None = None

    @model_validator(mode="after")
    def complete_trial_identity(self) -> Self:
        values = (self.trial_id, self.trial_generation, self.trial_sequence, self.candidate_sha256)
        if any(value is None for value in values) and any(value is not None for value in values):
            raise ValueError("operational trial identity must be complete")
        return self


class Observation(Model):
    plan_id: Identifier
    revision: int = Field(ge=1)
    assignment_id: Identifier
    authority_epoch: int = Field(ge=1)
    observed_at: Instant
    status: Literal["presented", "failed", "skipped", "fallback"]
    applied_calibration: AppliedCalibration | None = None

    @model_serializer(mode="wrap")
    def serialize_applied_calibration(self, handler):
        value = handler(self)
        if self.applied_calibration is None:
            value.pop("applied_calibration", None)
        return value

    position: float = Field(default=0, ge=0)
    detail: Literal["none", "decode", "download", "capacity", "clock", "expired", "authority"] = "none"


class Revocation(Model):
    plan_id: Identifier
    revision: int = Field(ge=1)
    authority_epoch: int = Field(ge=1)
    sequence: int = Field(ge=1)
    assignment_ids: tuple[Identifier, ...]
    mode: Literal["invalidate", "cancel"] = "invalidate"

    @model_validator(mode="after")
    def unique_assignments(self) -> Self:
        if len(set(self.assignment_ids)) != len(self.assignment_ids):
            raise ValueError("duplicate revoked assignment")
        return self
