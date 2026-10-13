"""Central's edge for the reader (decision 0026): the two reads it needs, never a write.

- The sent entry the code names: the operator route GET /v1/operator/players/{player_id}/outputs/
  {output_id}/picture-changes/{picture_change} (central/tuning_history_routes.py), with the admin
  token, decoded as Central's own PictureEntryView (one shape for the console and the agent).
- A LUT's table, only for a post-software suggestion with a LUT on: its paper profile by read-only
  SQL (`SELECT profile FROM luts WHERE profile_sha256 = %s`), baked by central/displays/lut.py
  `baked_lut` when its recipe is today's (LUT_RECIPE_ID), else None. No operator route serves a
  LUT's bytes (the Players' route takes a Player's credentials), and none is added for a dev tool.
"""
from __future__ import annotations

from typing import Protocol

from central.displays.views import PictureEntryView
from contracts.calibration_target import TargetCode
from contracts.lut import LutRef


class CentralReads(Protocol):
    def sent_entry(self, code: TargetCode) -> PictureEntryView | None:
        """The sent entry `code` names; None when Central answers 404 unknown_entry."""
        ...

    def lut_table(self, ref: LutRef) -> bytes | None:
        """The LUT `ref` names as the Player would get it; None when it cannot be read (no
        database address given, no such profile, or another recipe)."""
        ...


class HttpCentralReads:
    """CentralReads over Central's operator API (httpx) and, when `database_url` is given, one
    read-only PostgreSQL connection per LUT read (psycopg, `SET TRANSACTION READ ONLY`). Any other
    answer than 200 or 404 from Central is RuntimeError naming its status; secrets come from the
    environment the command is run in (`op run`), never from arguments that land in a shell's
    history: PHOTO_WALL_ADMIN_TOKEN and, optionally, PHOTO_WALL_READ_DATABASE_URL."""

    def __init__(self, base_url: str, admin_token: str, database_url: str | None) -> None:
        ...

    def sent_entry(self, code: TargetCode) -> PictureEntryView | None:
        ...

    def lut_table(self, ref: LutRef) -> bytes | None:
        ...


__all__ = ["CentralReads", "HttpCentralReads"]
