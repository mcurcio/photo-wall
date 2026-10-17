"""Protocol v1. No upstream identities, credentials or URLs cross this boundary."""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from contracts.frame_fit import FULL_SCREEN, OutputSize, PictureSize, convex_clockwise, picture_size
from contracts.kelvin import KELVIN_TABLE_MAX, KELVIN_TABLE_MIN, KELVIN_TABLE_STEP

# The API identifier rule (path parameters, Scene/Program/Source ids). The console's
# IDENTIFIER_PATTERN (authoring.js) is pinned equal to it by a test.
IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
Identifier = Annotated[str, Field(pattern=rf"^{IDENTIFIER_PATTERN}$")]
# The one usable Frame/Actuator id rule: a Scene reaches a target only as
# "<kind>:<id>" (central/runtime.py `Target`), so an id has no ':' and at most 96
# characters. New Frames are created under it (central/registry.py `FrameCreate`);
# the console's FRAME_ID_PATTERN is pinned equal to it by a test.
TARGET_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"
TargetIdentifier = Annotated[str, Field(pattern=rf"^{TARGET_ID_PATTERN}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Instant = Annotated[float, Field(allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FrameProfile(Model):
    width_px: int = Field(gt=0, le=16384)
    height_px: int = Field(gt=0, le=16384)
    diagonal_inches: Positive
    video: bool = True


Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
FitPoint = tuple[Fraction, Fraction]
FitCorners = tuple[FitPoint, FitPoint, FitPoint, FitPoint]   # TL, TR, BR, BL; fractions of the output


class CornersFit(Model):
    """Frame fit Corners (roadmap 1c; an explicit opt-in, owner 2026-10-10 q1): the picture is
    stretched exactly onto the four corners (a perspective transform), and a quarter turn picks
    which corner edge is the picture's top."""
    mode: Literal["corners"] = "corners"
    corners: FitCorners = FULL_SCREEN
    rotation: Literal[0, 90, 180, 270] = 0

    @model_validator(mode="after")
    def convex(self) -> Self:
        if not convex_clockwise(self.corners):
            raise ValueError("corners must form a nondegenerate ordered convex aperture")
        return self


class CoverFit(Model):
    """Frame fit Cover (roadmap 1c; the default, owner 2026-10-10 q1): the four corners are the
    opening; the picture is the unskewed rectangle turned `rotation` degrees clockwise that holds
    every corner (contracts/frame_fit.py `cover_rectangle`), and nothing is drawn outside the
    opening (owner q2: "Black/nothing")."""
    mode: Literal["cover"] = "cover"
    corners: FitCorners = FULL_SCREEN
    rotation: float = Field(default=0, ge=0, lt=360, allow_inf_nan=False)

    @model_validator(mode="after")
    def convex(self) -> Self:
        if not convex_clockwise(self.corners):
            raise ValueError("corners must form a nondegenerate ordered convex aperture")
        return self


FrameFit = Annotated[CornersFit | CoverFit, Field(discriminator="mode")]


class Calibration(Model):
    """A Frame's Position (the console's word): how its picture sits on the output, as its Frame
    fit (roadmap 1c; Cover by default, Corners an explicit opt-in, owner 2026-10-10 q1).
    `revision` rises with every saved Position."""

    revision: int = Field(default=1, ge=1)
    fit: FrameFit = Field(default_factory=CoverFit)


MAT_LIGHTS_MAX = 4          # a cap on the final pass's per-pixel cost (print look design)
MAT_DEPTH_MM_MAX = 20.0     # allows exaggeration: a real flush 45-degree bevel casts millimetres


class Light(Model):
    """A light the room shines on one Frame (print look; owner, 2026-10-12: "a directional light
    source (or multiple)"; "cool white light coming from the side AND warm white coming from the
    top"). Directional: far away, so it falls on the whole Frame at one angle. What each field does
    to the picture (the shadow's width, edge and darkness, and how lights combine) is the Player's
    (player/geometry.py, the mat shadow); this is its shape and ranges only."""

    from_degrees: int = Field(ge=0, le=359)        # clockwise from the wall's up (0 above, 90 from the right);
                                                   # wall-referenced through the Frame's rotation
    steep_degrees: int = Field(ge=5, le=90)        # off the wall: low is grazing (a long shadow), 90 straight on (none)
    softness_degrees: int = Field(ge=0, le=45)     # the light's apparent size: a bare bulb ~1, a window ~30
    strength_tenths: int = Field(ge=0, le=100)     # 0.0..10.0 against the room's general light (10 = as bright as it)
    colour_kelvin: int = Field(ge=KELVIN_TABLE_MIN, le=KELVIN_TABLE_MAX, multiple_of=KELVIN_TABLE_STEP)
                                                   # contracts/kelvin.py `kelvin_white`; 6500 is the Display's white


class Mat(Model):
    """A Frame's mat (print look; owner, 2026-10-12: "the mat shadow is definitely a Frame
    quality"): how far its inner edge stands in front of the picture, and the lights that fall on it.
    On the Frame's binding beside its profile and Position, saved through Position's trial; it never
    changes the Frame output, so it never re-plans photos. `Mat()` (depth 0, no lights) casts no
    shadow, and the Player skips the step (`shades`)."""

    depth_mm: float = Field(default=0.0, ge=0, le=MAT_DEPTH_MM_MAX, multiple_of=0.5, allow_inf_nan=False)
    lights: tuple[Light, ...] = Field(default=(), max_length=MAT_LIGHTS_MAX)

    @property
    def shades(self) -> bool:
        """Whether the mat casts any shadow: a depth above 0 and a light of strength above 0. The one
        skip rule of the Player's mat-shadow step."""
        return self.depth_mm > 0 and any(light.strength_tenths > 0 for light in self.lights)


class OutputBinding(Model):
    output_id: Identifier
    frame_id: Identifier
    generation: int = Field(ge=1)
    configuration_revision: int = Field(default=1, ge=1)
    profile: FrameProfile
    calibration: Calibration = Field(default_factory=Calibration)
    mat: Mat


def frame_output(profile: FrameProfile, calibration: Calibration,
                 output: OutputSize | None) -> PictureSize | None:
    """The Frame output of a Frame whose Output reports `output` (width, height): its picture's
    pixels (contracts/frame_fit.py `picture_size`, in the Frame fit's mode, corners and rotation,
    at the profile's aspect width_px / height_px). None when `output` is None or has a side
    below 1 (its Pi has not reported the Output's size). Pure; the one place a Frame's
    calibration and profile become its Frame output (follow-ups 2026-10-11, slice A1)."""
    if output is None or output[0] < 1 or output[1] < 1:
        return None
    fit = calibration.fit
    return picture_size(fit.mode, fit.corners, fit.rotation, output,
                        profile.width_px / profile.height_px)


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

    # The layer's look at an instant, each value a function of plan time alone: `position`,
    # `alpha`, and `motion`, when either changes. A renderer evaluates them at the time of the
    # frame it draws (player/look.py), never at a planning tick's time.
    def position(self, now: float) -> float:
        position = max(0.0, now - self.media_origin)
        if self.variant and self.variant.duration:
            position %= self.variant.duration
        return position

    def alpha(self, now: float) -> float:
        """Its strength at `now`: its opacity through its fade-in and fade-out."""
        fade_in = min(1.0, max(0.0, (now - self.start) / self.fade_in)) if self.fade_in else 1
        fade_out = min(1.0, max(0.0, (self.end - now) / self.fade_out)) if self.fade_out else 1
        return self.opacity * min(fade_in, fade_out)

    def motion(self) -> tuple[tuple[float, float], ...]:
        """When its look changes, as half-open spans of plan time: a video's whole time (its
        position runs), else its fade-in and its fade-out (its alpha runs)."""
        if self.variant is not None and self.variant.duration:
            return ((self.start, self.end),)
        return tuple(span for span, fade in (((self.start, self.start + self.fade_in), self.fade_in),
                                             ((self.end - self.fade_out, self.end), self.fade_out))
                     if fade)


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
