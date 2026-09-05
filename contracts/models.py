"""Protocol v1. No upstream identities, credentials or URLs cross this boundary."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Instant = Annotated[float, Field(allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Target(Model):
    kind: Literal["frame", "actuator"]
    id: Identifier

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


class FrameProfile(Model):
    width_px: int = Field(gt=0, le=16384)
    height_px: int = Field(gt=0, le=16384)
    diagonal_inches: Positive
    video: bool = True


class Calibration(Model):
    """Physical Frame geometry remains in registry; these values map output pixels.

    Corner order: top-left, top-right, bottom-right, bottom-left; normalized output.
    Renderer applies crop/layout before this projective transform and gain.
    """

    revision: int = Field(default=1, ge=1)
    rotation: Literal[0, 90, 180, 270] = 0
    corners: tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]] = (
        (0, 0), (1, 0), (1, 1), (0, 1)
    )
    crop: tuple[float, float, float, float] = (0, 0, 1, 1)
    gain: float = Field(default=1, ge=0, le=2)

    @model_validator(mode="after")
    def geometry(self) -> Self:
        if any(not 0 <= n <= 1 for point in self.corners for n in point):
            raise ValueError("corners must lie in normalized output space")
        left, top, right, bottom = self.crop
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("crop must describe a nonempty normalized rectangle")
        # Consistently convex clockwise in screen coordinates; excludes folded/degenerate quads.
        cross = []
        for i, a in enumerate(self.corners):
            b, c = self.corners[(i + 1) % 4], self.corners[(i + 2) % 4]
            cross.append((b[0]-a[0])*(c[1]-b[1]) - (b[1]-a[1])*(c[0]-b[0]))
        if min(cross) <= 1e-6:
            raise ValueError("corners must form a nondegenerate ordered convex aperture")
        return self


class OutputBinding(Model):
    output_id: Identifier
    frame_id: Identifier
    generation: int = Field(ge=1)
    configuration_revision: int = Field(default=1, ge=1)
    profile: FrameProfile
    calibration: Calibration = Field(default_factory=Calibration)
    preview: Calibration | None = None
    preview_expires: Instant | None = None

    @model_validator(mode="after")
    def preview_lease(self) -> Self:
        if (self.preview is None) != (self.preview_expires is None):
            raise ValueError("preview and expiry must be supplied together")
        if self.preview and self.preview.revision != self.calibration.revision:
            raise ValueError("preview must reference committed calibration revision")
        return self

    def effective_calibration(self, now: float) -> Calibration:
        if self.preview is not None and self.preview_expires is not None and now < self.preview_expires:
            return self.preview
        return self.calibration


class Variant(Model):
    sha256: Digest
    size: int = Field(gt=0, le=4 * 1024**3)
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
    def path(self) -> str:
        return f"/v1/media/{self.sha256}"


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

    @model_validator(mode="after")
    def interval(self) -> Self:
        if self.end <= self.start:
            raise ValueError("layer end must follow start")
        if self.presentation == "media" and self.variant is None:
            raise ValueError("media layer requires an exact variant")
        if self.presentation == "black" and self.variant is not None:
            raise ValueError("black layer cannot carry media")
        if self.fade_in + self.fade_out > self.end - self.start:
            raise ValueError("fades exceed layer interval")
        return self

    def position(self, now: float) -> float:
        position = max(0.0, now - self.media_origin)
        if self.variant and self.variant.duration:
            position %= self.variant.duration
        return position


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

    @model_validator(mode="after")
    def coherent_readiness(self) -> Self:
        failed = [f.assignment_id for f in self.failures]
        for ids in (self.secured, self.prepared, failed):
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate readiness assignment")
        if not set(self.prepared) <= set(self.secured):
            raise ValueError("prepared assignments must be secured")
        if set(self.prepared) & set(failed):
            raise ValueError("failed assignment cannot report prepared playback")
        return self


class Commit(Model):
    plan_id: Identifier
    revision: int = Field(ge=1)
    authority_epoch: int = Field(ge=1)
    assignment_ids: tuple[Identifier, ...]
    committed_at: Instant

    @model_validator(mode="after")
    def unique_assignments(self) -> Self:
        if len(self.assignment_ids) != len(set(self.assignment_ids)):
            raise ValueError("duplicate committed assignment")
        return self


class Observation(Model):
    plan_id: Identifier
    revision: int = Field(ge=1)
    assignment_id: Identifier
    authority_epoch: int = Field(ge=1)
    observed_at: Instant
    status: Literal["presented", "failed", "skipped", "fallback"]
    position: float = Field(default=0, ge=0)
    detail: Literal["none", "decode", "download", "capacity", "clock", "expired", "authority"] = "none"
