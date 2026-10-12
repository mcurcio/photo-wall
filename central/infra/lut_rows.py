"""The LUTs list's rows read in a caller's transaction (print look; migration 093's `luts`): whether
a paper profile is listed, and its name. The picture session's LUT edit (central/infra/display_store.py)
asks them; the list itself is central/infra/lut_store.py's.
"""
from __future__ import annotations

from typing import Any


def lut_name_in(conn: Any, profile_sha256: str) -> str | None:
    """The listed LUT's name; None when it is not on the list."""
    row = conn.execute("SELECT name FROM luts WHERE profile_sha256=%s", (profile_sha256,)).fetchone()
    return None if row is None else row["name"]


def lut_known_in(conn: Any, profile_sha256: str) -> bool:
    """Whether the LUTs list holds `profile_sha256`, under a key-share lock: a concurrent Remove
    waits for the caller's transaction, and then sees the choice it made."""
    return conn.execute("SELECT 1 FROM luts WHERE profile_sha256=%s FOR KEY SHARE",
                        (profile_sha256,)).fetchone() is not None


__all__ = ["lut_known_in", "lut_name_in"]
