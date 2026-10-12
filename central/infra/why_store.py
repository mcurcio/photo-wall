"""Why over PostgreSQL (Power and the week: slice T `frame_why`, slice S3 `week`).

One transaction per answer, one clock: Runtime is projected (never advanced or saved) at
max(the clock's now, the stored Runtime's now), and the same instant is the timetable's `now`, so
content and power are explained at one moment.
"""
from __future__ import annotations

from datetime import date

from central.db import Database, TransactionClock
from central.infra.display_store import power_facts_in
from central.infra.power_store import power_policy_in
from central.power.timetable import frame_timetable
from central.runtime import Program, Scene
from central.runtime_store import RuntimeStore
from central.why.explain import content_why, kept_out, power_why
from central.why.model import FrameWhy, Week
from central.why.ports import WhyRefused
from central.why.week import week


class PgWhy:
    """central.why.ports WhyQueries over PostgreSQL."""

    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        self._db, self._times = db, times

    def frame_why(self, frame_id: str) -> FrameWhy:
        with self._db.transaction() as conn:
            facts = power_facts_in(conn, frame_id)
            if facts is None:
                raise WhyRefused("unknown_frame", 404)
            runtime = RuntimeStore.read_in(conn)
            state = runtime.export_state()
            now = max(self._times.now_in(conn), state["now"] if state["now"] is not None else float("-inf"))
            policy = power_policy_in(conn, now)
        house = runtime.house
        programs = {program_id: Program.model_validate(program) for program_id, program in state["programs"].items()}
        scenes = {scene_id: Scene.model_validate(scene) for scene_id, scene in state["scenes"].items()}
        projection = runtime.operator_projection(now)
        content = content_why(projection.current, frame_id, programs=programs,
                              refused=kept_out(projection, programs, scenes, frame_id))
        timetable = frame_timetable(frame_id, policy, house, now) if facts.bound else None
        power = power_why(timetable=timetable, test=facts.test, document=facts.document, report=facts.report,
                          house=house, now=now)
        return FrameWhy(frame_id=frame_id, content=content, power=power)

    def week(self, first_day: date) -> Week:
        with self._db.transaction() as conn:
            runtime = RuntimeStore.read_in(conn)
            policy = power_policy_in(conn, self._times.now_in(conn))
            frame_ids = tuple(row["id"] for row in conn.execute("SELECT id FROM frames").fetchall())
        state = runtime.export_state()
        chosen = state["default_scene"]
        return week(programs={program_id: Program.model_validate(program)
                              for program_id, program in state["programs"].items()},
                    plays=runtime.program_windows_between,
                    default_scene_id=None if chosen is None else chosen["scene_id"], policy=policy,
                    frame_ids=frame_ids, house=runtime.house, first_day=first_day)


__all__ = ["PgWhy"]
