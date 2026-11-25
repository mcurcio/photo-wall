"""Pauses over PostgreSQL (decision 0036, slice 3): the one repository of migration 135's
`frame_pauses` table, called inside Coordination's transaction (central/coordination.py
`Coordinator.pause`, `carry_on`). One row per Frame; a row changes only at its pause, its Carry on
and its replacement by a later pause once it is over, and nothing unchanged is written.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from psycopg.types.json import Jsonb

from central.everyday.model import Pause
from contracts.models import Layer


def _pause(row: Mapping[str, Any]) -> Pause:
    return Pause(pause_id=row["pause_id"], frame_id=row["frame_id"], by=row["created_by"],
                 started_at=row["started_at"], ends_at=row["ends_at"], ended_at=row["ended_at"],
                 scene_id=row["scene_id"], held=Layer.model_validate(row["held"]))


class PgPauses:
    """Stateless; every method runs in the caller's transaction."""

    @staticmethod
    def of_in(conn: Any, pause_id: str) -> tuple[Pause, ...]:
        """Every Frame's row of `pause_id`, in Frame id order."""
        return tuple(_pause(row) for row in conn.execute(
            "SELECT * FROM frame_pauses WHERE pause_id=%s ORDER BY frame_id", (pause_id,)).fetchall())

    @staticmethod
    def live_in(conn: Any, now: float, frame_ids: Sequence[str] | None = None) -> tuple[Pause, ...]:
        """Every pause whose hold is still drawn, or still to be, at `now` (`Pause.live`), in Frame
        id order (of `frame_ids` alone when given)."""
        rows = conn.execute("SELECT * FROM frame_pauses ORDER BY frame_id").fetchall()
        wanted = None if frame_ids is None else set(frame_ids)
        return tuple(pause for pause in map(_pause, rows)
                     if pause.live(now) and (wanted is None or pause.frame_id in wanted))

    @staticmethod
    def put_in(conn: Any, pause: Pause, *, now: float) -> None:
        """A Frame's new pause, replacing its row (the caller judged the one it held over)."""
        conn.execute(
            "INSERT INTO frame_pauses(frame_id,pause_id,started_at,ends_at,ended_at,scene_id,held,"
            "created_at,created_by,changed_at,changed_by) VALUES(%(frame)s,%(pause)s,%(start)s,%(end)s,"
            "NULL,%(scene)s,%(held)s,%(now)s,%(by)s,%(now)s,%(by)s) "
            "ON CONFLICT(frame_id) DO UPDATE SET pause_id=EXCLUDED.pause_id,started_at=EXCLUDED.started_at,"
            "ends_at=EXCLUDED.ends_at,ended_at=NULL,scene_id=EXCLUDED.scene_id,held=EXCLUDED.held,"
            "created_at=EXCLUDED.created_at,created_by=EXCLUDED.created_by,"
            "changed_at=EXCLUDED.changed_at,changed_by=EXCLUDED.changed_by",
            {"frame": pause.frame_id, "pause": pause.pause_id, "start": pause.started_at,
             "end": pause.ends_at, "scene": pause.scene_id, "held": Jsonb(pause.held.model_dump(mode="json")),
             "now": now, "by": pause.by})

    @staticmethod
    def end_in(conn: Any, pause: Pause, held: Layer, *, now: float, by: str) -> None:
        """Carry on: the pause ended at `now`, its hold `held` (cut, or as it was), by `by`."""
        conn.execute(
            "UPDATE frame_pauses SET ended_at=%s,held=%s,changed_at=%s,changed_by=%s "
            "WHERE frame_id=%s AND pause_id=%s AND ended_at IS NULL",
            (now, Jsonb(held.model_dump(mode="json")), now, by, pause.frame_id, pause.pause_id))


__all__ = ["PgPauses"]
