"""Central catalog data shared by planning and media adapters, never Player protocol."""

from typing import Literal

from pydantic import Field

from contracts.models import Identifier, Instant, Model, Variant


class Candidate(Model):
    asset_id: Identifier
    kind: Literal["image", "video"]
    original_width: int = Field(gt=0)
    original_height: int = Field(gt=0)
    captured_at: Instant
    variant: Variant | None = None
    preparation_failure: str | None = Field(default=None, pattern=r"^[a-z_]{1,64}$")


class SourcePool(Model):
    """One saved Source revision's whole pool as the Planner reads it (roadmap 4, slice C2):
    every in-pool photo, hydrated with its variant, no window and no limit (C11). `ready` is False until the pool has a complete check; what it has so far still
    plays (C7: the last pool keeps playing while Immich is down)."""

    source_ref: Identifier
    ready: bool
    candidates: tuple[Candidate, ...] = ()
