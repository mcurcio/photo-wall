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


class CatalogSnapshot(Model):
    # References name immutable authored query versions, e.g. holiday:1.
    source_ref: Identifier
    refreshed_at: Instant
    status: Literal["ok", "unavailable", "permission", "incompatible"] = "ok"
    candidates: tuple[Candidate, ...] = ()
