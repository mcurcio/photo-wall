"""The LUTs list over PostgreSQL (print look, design "The LUT"; migration 093's `luts`): the paper
profiles the house keeps, each the one source of the LUT baked from it (central/displays/lut.py).

- **Add** checks and bakes the profile once (`checked_profile`), so a listed LUT always works; the
  same bytes twice are one row.
- **In use** (one rule, `_users_in`): a Display that stores the LUT, or whose live picture session's
  draft chooses it (the session's lease is read on PostgreSQL's clock, as the session's own rules
  read it). The list shows them; Remove refuses while there are any. The displays column's
  foreign key (RESTRICT) holds the stored half in the database too.
- **Known** (`lut_known_in`): the picture session's LUT edit asks it in its own transaction, under a
  key-share lock that a concurrent Remove waits on.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any, Final

from central.db import Database, DatabaseTransactionClock
from central.displays.lut import LutRefusal, PaperProfile, checked_profile
from central.displays.views import LutsView, LutView
from contracts.time import Clock

_LEASE_CLOCK: Final = DatabaseTransactionClock()

# Every (LUT, Display) use: the Display's stored choice, and its live session's draft.
_USERS: Final = (
    "SELECT DISTINCT u.profile_sha256, d.id, d.name, d.maker, d.product FROM displays d "
    "LEFT JOIN picture_sessions ps ON ps.display_id=d.id AND ps.idle_until > %(now)s "
    "CROSS JOIN LATERAL (VALUES (d.software_lut_profile), (ps.draft->>'software_lut_profile')) AS u(profile_sha256) "
    "WHERE u.profile_sha256 IS NOT NULL AND (%(lut)s::text IS NULL OR u.profile_sha256 = %(lut)s::text) "
    "ORDER BY d.name, d.maker, d.product, d.id")


class LutRefused(Exception):
    """A refusal the routes answer as {"error": code} (and the Displays that use the LUT, for
    lut_in_use): too_large 413, not_a_colour_profile 422, not_a_printer_profile 422, lut_unknown
    404, lut_in_use 409."""

    def __init__(self, code: str, status: int, displays: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.displays = displays


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
        """LutRefused: lut_unknown 404; lut_in_use 409, naming every Display that stores it or
        whose open picture session's draft chooses it."""
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


def lut_name_in(conn: Any, profile_sha256: str) -> str | None:
    """The listed LUT's name; None when it is not on the list."""
    row = conn.execute("SELECT name FROM luts WHERE profile_sha256=%s", (profile_sha256,)).fetchone()
    return None if row is None else row["name"]


def lut_known_in(conn: Any, profile_sha256: str) -> bool:
    """Whether the LUTs list holds `profile_sha256`, under a key-share lock: a concurrent Remove
    waits for the caller's transaction, and then sees the choice it made."""
    return conn.execute("SELECT 1 FROM luts WHERE profile_sha256=%s FOR KEY SHARE",
                        (profile_sha256,)).fetchone() is not None


def _users_in(conn: Any, profile_sha256: str | None) -> Mapping[str, tuple[str, ...]]:
    """Each LUT's users (only `profile_sha256`'s, when given), by Display name: the EDID's name, or
    its maker and product code when the EDID names none."""
    users: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute(_USERS, {"now": _LEASE_CLOCK.now_in(conn), "lut": profile_sha256}).fetchall():
        users[row["profile_sha256"]].append(row["name"] or f"{row['maker']} {row['product']}")
    return {lut: tuple(names) for lut, names in users.items()}


def _view(row: Mapping[str, Any], users: Mapping[str, Iterable[str]]) -> LutView:
    return LutView(profile_sha256=row["profile_sha256"], name=row["name"], added_at=row["added_at"],
                   used_by=tuple(users.get(row["profile_sha256"], ())))


__all__ = ["LutRefused", "PgLuts", "lut_known_in", "lut_name_in"]
