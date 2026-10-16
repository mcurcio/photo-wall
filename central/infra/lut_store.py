"""The paper profiles list over PostgreSQL (migration 093's `luts`): the paper profiles the house
keeps, each the one source of the LUT baked from it (central/frame_lighting/paper_profiles.py).

- **Add** checks and bakes the profile once (`checked_profile`), so a listed profile always bakes;
  the same bytes twice are one row.
- **In use**: a Frame whose paper names the profile, saved or drafted in its active Position trial
  (central/infra/frame_lighting_store.py `naming_in`, the one query). The list shows them by name;
  Remove refuses while there are any, under the house's lock, so a Frame-lighting check never names
  a profile a Remove has taken.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from central.db import Database
from central.frame_lighting.paper_profiles import (
    LUT_RECIPE_ID,
    PaperProfile,
    PaperProfileRefusal,
    PaperProfileRefused,
    checked_profile,
)
from central.frame_lighting.views import PaperProfilesView, PaperProfileView
from central.infra.frame_lighting_store import naming_in, take_house_lock
from contracts.time import Clock


class PgLuts:
    """The paper profiles list: list, add, remove, and a profile's bytes for the bake."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def list_luts(self) -> PaperProfilesView:
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT profile_sha256, name, added_at FROM luts ORDER BY name, profile_sha256").fetchall()
            users = _users_in(conn)
        return PaperProfilesView(luts=tuple(_view(row, users) for row in rows))

    def add_lut(self, profile: bytes) -> tuple[PaperProfileView, bool]:
        """(the listed profile, whether this added it): the same bytes twice are one row.
        PaperProfileRefused (too_large, not_a_colour_profile, not_a_printer_profile); nothing is stored."""
        checked = checked_profile(profile)
        if isinstance(checked, PaperProfileRefusal):
            raise PaperProfileRefused(checked)
        return self._listed(checked, profile)

    def _listed(self, paper: PaperProfile, profile: bytes) -> tuple[PaperProfileView, bool]:
        with self._db.transaction() as conn:
            created = conn.execute(
                "INSERT INTO luts(profile_sha256, name, profile, added_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (profile_sha256) DO NOTHING RETURNING profile_sha256",
                (paper.sha256, paper.name, profile, self._clock.utc())).fetchone() is not None
            row = conn.execute("SELECT profile_sha256, name, added_at FROM luts WHERE profile_sha256=%s",
                               (paper.sha256,)).fetchone()
            return _view(row, _users_in(conn)), created

    def remove_lut(self, profile_sha256: str) -> None:
        """PaperProfileRefused: unknown_paper_profile; named_by_a_frame, naming every Frame whose saved
        or drafted paper names it."""
        with self._db.transaction() as conn:
            take_house_lock(conn)
            if conn.execute("SELECT 1 FROM luts WHERE profile_sha256=%s", (profile_sha256,)).fetchone() is None:
                raise PaperProfileRefused(PaperProfileRefusal.UNKNOWN_PAPER_PROFILE)
            users = _users_in(conn).get(profile_sha256, ())
            if users:
                raise PaperProfileRefused(PaperProfileRefusal.NAMED_BY_A_FRAME, users)
            conn.execute("DELETE FROM luts WHERE profile_sha256=%s", (profile_sha256,))

    def profile(self, profile_sha256: str) -> bytes | None:
        """The listed paper profile's bytes; None when it is not on the list."""
        with self._db.transaction() as conn:
            row = conn.execute("SELECT profile FROM luts WHERE profile_sha256=%s", (profile_sha256,)).fetchone()
        return None if row is None else bytes(row["profile"])


def _users_in(conn: Any) -> Mapping[str, tuple[str, ...]]:
    """Each profile's users: the names of the Frames whose saved or drafted paper names it, in order."""
    names = {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM frames").fetchall()}
    return {profile: tuple(sorted(names[frame] for frame in frames))
            for profile, frames in naming_in(conn).paper_profiles.items()}


def _view(row: Mapping[str, Any], users: Mapping[str, tuple[str, ...]]) -> PaperProfileView:
    return PaperProfileView(profile_sha256=row["profile_sha256"], recipe_id=LUT_RECIPE_ID, name=row["name"],
                            added_at=row["added_at"], used_by=users.get(row["profile_sha256"], ()))


__all__ = ["PgLuts"]
