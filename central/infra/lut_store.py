"""The LUTs list over PostgreSQL (print look, design "The LUT"; migration 093's `luts`): the paper
profiles the house keeps, each the one source of the LUT baked from it (central/displays/lut.py).

- **Add** checks and bakes the profile once (`checked_profile`), so a listed LUT always works; the
  same bytes twice are one row.
- **In use** (one rule, `_users_in`): a Frame whose paper names the LUT's profile (decision 0028:
  the paper profile lives on the Frame's lighting), saved or drafted in its active Position trial.
  The list shows them by name; Remove refuses while there are any.
- **Known** (central/infra/lut_rows.py `lut_known_in`): Position's trial asks it for a drafted paper
  profile in its own transaction, under a key-share lock that a concurrent Remove waits on.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any, Final

from central.db import Database
from central.displays.lut import LUT_RECIPE_ID, LutRefusal, PaperProfile, checked_profile
from central.displays.views import LutsView, LutView
from contracts.frame_lighting import FrameLighting
from contracts.time import Clock

_LIGHTINGS: Final = ("SELECT f.id, f.name, f.lighting FROM frames f UNION ALL "
                     "SELECT f.id, f.name, t.lighting FROM node_calibration_trials t JOIN frames f ON f.id=t.frame_id "
                     "WHERE t.state='active' ORDER BY name, id")


class LutRefused(Exception):
    """A refusal the routes answer as {"error": code} (and the Frames whose paper uses the LUT, for
    lut_in_use): too_large 413, not_a_colour_profile 422, not_a_printer_profile 422, lut_unknown
    404, lut_in_use 409."""

    def __init__(self, code: str, status: int, frames: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.frames = frames


class PgLuts:
    """The LUTs list: list, add, remove, and a profile's bytes for the bake."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def list_luts(self) -> LutsView:
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT profile_sha256, name, added_at FROM luts ORDER BY name, profile_sha256").fetchall()
            users = _users_in(conn, None)
        return LutsView(luts=tuple(_view(row, users) for row in rows))

    def add_lut(self, profile: bytes) -> tuple[LutView, bool]:
        """(the listed LUT, whether this added it): the same bytes twice are one row. LutRefused:
        too_large 413, not_a_colour_profile or not_a_printer_profile 422; nothing is stored."""
        checked = checked_profile(profile)
        if isinstance(checked, LutRefusal):
            raise LutRefused(checked.value, 413 if checked is LutRefusal.TOO_LARGE else 422)
        return self._listed(checked, profile)

    def _listed(self, paper: PaperProfile, profile: bytes) -> tuple[LutView, bool]:
        with self._db.transaction() as conn:
            created = conn.execute(
                "INSERT INTO luts(profile_sha256, name, profile, added_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (profile_sha256) DO NOTHING RETURNING profile_sha256",
                (paper.sha256, paper.name, profile, self._clock.utc())).fetchone() is not None
            row = conn.execute("SELECT profile_sha256, name, added_at FROM luts WHERE profile_sha256=%s",
                               (paper.sha256,)).fetchone()
            return _view(row, _users_in(conn, paper.sha256)), created

    def remove_lut(self, profile_sha256: str) -> None:
        """LutRefused: lut_unknown 404; lut_in_use 409, naming every Frame whose saved or drafted
        paper names it."""
        with self._db.transaction() as conn:
            if conn.execute("SELECT 1 FROM luts WHERE profile_sha256=%s FOR UPDATE",
                            (profile_sha256,)).fetchone() is None:
                raise LutRefused("lut_unknown", 404)
            users = _users_in(conn, profile_sha256).get(profile_sha256, ())
            if users:
                raise LutRefused("lut_in_use", 409, users)
            conn.execute("DELETE FROM luts WHERE profile_sha256=%s", (profile_sha256,))

    def profile(self, profile_sha256: str) -> bytes | None:
        """The listed paper profile's bytes; None when it is not on the list."""
        with self._db.transaction() as conn:
            row = conn.execute("SELECT profile FROM luts WHERE profile_sha256=%s", (profile_sha256,)).fetchone()
        return None if row is None else bytes(row["profile"])


def _users_in(conn: Any, profile_sha256: str | None) -> Mapping[str, tuple[str, ...]]:
    """Each LUT's users (only `profile_sha256`'s, when given): the Frames whose saved lighting or
    active trial's draft names it as its paper profile, by Frame name, a Frame once per LUT."""
    users: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute(_LIGHTINGS).fetchall():
        profile = FrameLighting.model_validate(row["lighting"]).paper.profile
        if profile is None or (profile_sha256 is not None and profile.profile_sha256 != profile_sha256):
            continue
        if row["name"] not in users[profile.profile_sha256]:
            users[profile.profile_sha256].append(row["name"])
    return {lut: tuple(names) for lut, names in users.items()}


def _view(row: Mapping[str, Any], users: Mapping[str, Iterable[str]]) -> LutView:
    return LutView(profile_sha256=row["profile_sha256"], recipe_id=LUT_RECIPE_ID, name=row["name"],
                   added_at=row["added_at"],
                   used_by=tuple(users.get(row["profile_sha256"], ())))


__all__ = ["LutRefused", "PgLuts"]
