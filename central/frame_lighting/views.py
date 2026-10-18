"""The paper profiles list's operator API shapes (GET /v1/operator/paper-profiles;
central/paper_profile_routes.py)."""
from __future__ import annotations

from contracts.models import Digest, Instant, Model


class PaperProfileView(Model):
    """One paper profile on the list: its digest, the recipe Central bakes its LUT with today (a
    Frame's paper names it as {profile_sha256, recipe_id}, contracts/lut.py LutRef), the profile's
    own name, when it was added (Central's clock), and the Frames whose paper names it (saved, or
    drafted in an open Position trial), by name."""
    profile_sha256: Digest
    recipe_id: str
    name: str
    added_at: Instant
    used_by: tuple[str, ...]


class PaperProfilesView(Model):
    """GET /v1/operator/paper-profiles."""
    paper_profiles: tuple[PaperProfileView, ...]


__all__ = ["PaperProfileView", "PaperProfilesView"]
