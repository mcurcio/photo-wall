"""Shown history over PostgreSQL (roadmap 5a; run ledger .claude/runs/photo-5a.md, slice O1).

`PgShownHistory` implements central.wall_plan.ports.ShownHistory and is the one home of migration
083's tables:
- `photo_selections`: what each offered layer shows (one row per assignment id, first write
  wins), written by coordination when it offers a plan;
- `shown_photos`: one row per layer its Player reported presented (owner, 2026-10-11: "“Shown”
  means it was displayed on a frame"), copied from its selection, written by coordination's
  `observe`.
A layer planned wholly into wanted-off time (Night off, a hold: `power_store.wanted_off_in`) is
never recorded shown: planning carries on through Night off so the morning reveals current state,
but a photo shown to a dark room was not seen (Power and the week, F3).
Times are the layers' planned intervals on Central's clock (the coordinator's), as the Planner
compares them; `presented_at` is the observation's receipt time on that same clock.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from central.infra.power_store import wanted_off_in
from central.wall_plan.order import KeptStill, PhotoShowing, WallHistory

# How long a shown row is kept. Longer than the longest no-repeat window (365 days), so a pass
# older than this restarts from what remains (design parked note "Shown-history retention").
SHOWN_KEEP_SECONDS: Final = 400 * 86400
# How long a selection is kept after its layer ends (its presented report may arrive late).
SELECTION_KEEP_SECONDS: Final = 86400

_COLUMNS: Final = ("assignment_id, asset_id, frame_id, scene_id, pass_number, starts_at, ends_at, "
                   "fade_in, after_end")


def _showing(row: dict) -> PhotoShowing:
    return PhotoShowing(
        assignment_id=row["assignment_id"], asset_id=row["asset_id"], frame_id=row["frame_id"],
        scene_id=row["scene_id"], pass_number=row["pass_number"], start=row["starts_at"],
        end=row["ends_at"], fade_in=row["fade_in"], after_end=row["after_end"],
    )


class PgShownHistory:
    """Stateless; every method runs in the caller's transaction (coordination's)."""

    def record_selected_in(self, conn: Any, showings: Iterable[PhotoShowing]) -> None:
        rows = [(s.assignment_id, s.asset_id, s.frame_id, s.scene_id, s.pass_number, s.start,
                 s.end, s.fade_in, s.after_end) for s in showings]
        if not rows:
            return
        with conn.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO photo_selections ({_COLUMNS}) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                rows,
            )

    def record_presented_in(self, conn: Any, assignment_id: str, presented_at: float) -> bool:
        selected = conn.execute(
            "SELECT frame_id, starts_at, ends_at FROM photo_selections WHERE assignment_id=%s",
            (assignment_id,),
        ).fetchone()
        if selected is None or wanted_off_in(conn, selected["frame_id"], selected["starts_at"],
                                             selected["ends_at"]):
            return False
        conn.execute(
            f"INSERT INTO shown_photos ({_COLUMNS}, presented_at) "
            f"SELECT {_COLUMNS}, %s FROM photo_selections WHERE assignment_id=%s "
            "ON CONFLICT DO NOTHING",
            (presented_at, assignment_id),
        )
        return True

    def wall_history_in(self, conn: Any, scene_ids: Iterable[str], *, now: float,
                        window_seconds: float) -> WallHistory:
        scenes = sorted(set(scene_ids))
        # Two index-backed halves (shown_photos_ends; shown_photos_pass, one max per named
        # Scene), so the per-second advance never scans the whole retained history.
        shown = conn.execute(
            f"SELECT {_COLUMNS} FROM shown_photos WHERE ends_at >= %s "
            f"UNION SELECT {', '.join('s.' + c for c in _COLUMNS.split(', '))} "
            "FROM unnest(%s::text[]) AS named(scene_id) "
            "CROSS JOIN LATERAL (SELECT max(pass_number) AS pass_number FROM shown_photos "
            "    WHERE scene_id = named.scene_id) AS current "
            "JOIN shown_photos s ON s.scene_id = named.scene_id "
            "    AND s.pass_number = current.pass_number "
            "ORDER BY starts_at, assignment_id",
            (now - window_seconds, scenes),
        ).fetchall()
        selected = conn.execute(
            f"SELECT {_COLUMNS} FROM photo_selections WHERE ends_at > %s "
            "ORDER BY starts_at, assignment_id",
            (now,),
        ).fetchall()
        # Each Frame's latest started layer that set what it keeps, offered or presented: a
        # covered layer is drawn, so its after-state applies, but its Player never reports it
        # presented (and a report can be lost); the offered selection names it either way, and
        # shown_photos holds the older history once selections are pruned. The Frame keeps a
        # photo only when that layer said "keep_this_photo" (a later "keep_nothing" clears it).
        # One index-backed row per Frame and table (photo_selections_kept, shown_photos_kept).
        kept = conn.execute(
            "SELECT f.id AS frame_id, k.asset_id, k.starts_at + k.fade_in AS since, k.after_end "
            "FROM frames f CROSS JOIN LATERAL ("
            "  SELECT * FROM ("
            "    (SELECT asset_id, starts_at, fade_in, after_end, assignment_id "
            "     FROM photo_selections WHERE frame_id = f.id AND after_end <> 'leave_as_is' "
            "       AND starts_at <= %s ORDER BY starts_at DESC, assignment_id DESC LIMIT 1) "
            "    UNION ALL "
            "    (SELECT asset_id, starts_at, fade_in, after_end, assignment_id "
            "     FROM shown_photos WHERE frame_id = f.id AND after_end <> 'leave_as_is' "
            "     ORDER BY starts_at DESC, assignment_id DESC LIMIT 1)) AS latest "
            "  ORDER BY starts_at DESC, assignment_id DESC LIMIT 1) AS k",
            (now,),
        ).fetchall()
        return WallHistory(
            shown=tuple(_showing(row) for row in shown),
            selected=tuple(_showing(row) for row in selected),
            kept=tuple(
                KeptStill(frame_id=row["frame_id"], asset_id=row["asset_id"], since=row["since"])
                for row in kept if row["after_end"] == "keep_this_photo"
            ),
        )

    def prune_in(self, conn: Any, *, now: float) -> None:
        conn.execute("DELETE FROM photo_selections WHERE ends_at < %s",
                     (now - SELECTION_KEEP_SECONDS,))
        conn.execute("DELETE FROM shown_photos WHERE ends_at < %s", (now - SHOWN_KEEP_SECONDS,))
