"""Display power policy over PostgreSQL (Power and the week: slice T for Night off, slice S2 for
holds; migrations 088 `night_off` and 089 `power_holds`).

`power_policy_in` is the one read of the policy: the Output document source
(central/infra/display_store.py), Why and the shown-history filter all call it in their own
transaction. Each command wakes every Node's Output document source in its own transaction
(`NOTIFY OUTPUT_DOCUMENTS_CHANNEL`, from app code; no trigger), and the source also wakes at the
timetable's next set time, its next horizon move and the next hold end, so a timetable rolls
forward with no other write.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from central.db import Database, TransactionClock
from central.house_time.calculator import local_date_time, next_window, when_problems, window_at
from central.house_time.model import HouseTime
from central.infra.house_store import house_time_in
from central.power.model import AllFrames, Hold, HoldSource, NightOff, PowerPolicy
from central.power.ports import PowerRefused
from central.power.timetable import hold_end, spans
from central.power.views import HoldView, NightOffSave, NightOffView
from contracts.node_output import Power

_HOLD_COLUMNS = "hold_id, power, source, started_at, ends_at"


def night_off_in(conn: Any) -> NightOff:
    """The stored Night off (migration 088 writes the row; it always exists)."""
    row = conn.execute("SELECT settings, revision FROM night_off").fetchone()
    return NightOff.model_validate({**row["settings"], "revision": row["revision"]})


def power_policy_in(conn: Any, now: float) -> PowerPolicy:
    """Night off and the holds live at Central's `now` (ends_at after it, or until resumed): one
    Hold per hold, its Frames sorted. An expired row is ignored here and deleted by the next
    command (never by a reaper or a trigger)."""
    rows = conn.execute(
        f"SELECT frame_id, {_HOLD_COLUMNS} FROM power_holds WHERE ends_at IS NULL OR ends_at > %s",  # noqa: S608
        (now,)).fetchall()
    return PowerPolicy(night_off_in(conn), holds=_holds(rows))


def wanted_off_in(conn: Any, frame_id: str, start: float, end: float) -> bool:
    """Slice S2 (F3): whether `frame_id`'s wanted power is off for the whole of [start, end) by the
    policy live at `start` (power.timetable `spans`); shown history ignores such a layer."""
    if not start < end:
        return False
    return all(span.power is Power.OFF
               for span in spans(frame_id, power_policy_in(conn, start), house_time_in(conn), start, end))


def _holds(rows: Iterable[Mapping[str, Any]]) -> tuple[Hold, ...]:
    """Rows to Holds, newest first. Rows are grouped by the whole hold (id, power, start, end): a
    double tap that straddles a Frame's creation gives that Frame its own start, so it reads as a
    Hold of its own with the same id rather than moving the others' start."""
    grouped: dict[tuple[Any, ...], list[str]] = {}
    for row in rows:
        grouped.setdefault((row["hold_id"], row["power"], row["source"], row["started_at"], row["ends_at"]),
                           []).append(row["frame_id"])
    holds = [Hold(hold_id=hold_id, power=Power(power), frame_ids=tuple(sorted(frame_ids)),
                  source=HoldSource(kind=source), started_at=started_at, ends_at=ends_at)
             for (hold_id, power, source, started_at, ends_at), frame_ids in grouped.items()]
    return tuple(sorted(holds, key=lambda hold: (-hold.started_at, str(hold.hold_id))))


def _hold_view(hold: Hold, house: HouseTime) -> HoldView:
    return HoldView(hold_id=hold.hold_id, power=hold.power, frame_ids=hold.frame_ids, source=hold.source,
                    started_at=hold.started_at, until_at=hold.ends_at,
                    until=None if hold.ends_at is None else local_date_time(house, hold.ends_at))


def _wake_every_node(conn: Any) -> None:
    """Wake every unretired Node's Output document source when the caller's transaction commits
    (display_store's `_notify` shape; its channel, imported here because display_store imports
    this module)."""
    from central.infra.display_store import OUTPUT_DOCUMENTS_CHANNEL
    conn.execute("SELECT pg_notify(%s, device_id) FROM players WHERE retired_at IS NULL",
                 (OUTPUT_DOCUMENTS_CHANNEL,))


def _forget_expired(conn: Any, now: float) -> None:
    """A command's lazy delete of the holds that ended by Central's `now`."""
    conn.execute("DELETE FROM power_holds WHERE ends_at IS NOT NULL AND ends_at <= %s", (now,))


class PgPower:
    """central.power.ports PowerQueries and PowerCommands over PostgreSQL."""

    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        """`times` is Central's clock for hold starts and ends (the database's in production, G11)."""
        self._db, self._times = db, times

    def night_off(self) -> NightOffView:
        with self._db.transaction() as conn:
            night_off, house, now = night_off_in(conn), house_time_in(conn), self._times.now_in(conn)
        try:
            following, current = next_window(night_off.when, house, now), window_at(night_off.when, house, now)
        except ValueError:          # no days, or a sun time with no Place: no times to show
            following = current = None
        # The next off is the next window's start; the next on ends the window in force, else the next.
        on_window = current if current is not None else following
        return NightOffView(
            night_off=night_off,
            next_off=None if following is None else local_date_time(house, following.start),
            next_on=None if on_window is None else local_date_time(house, on_window.end))

    def holds(self) -> tuple[HoldView, ...]:
        with self._db.transaction() as conn:
            house, now = house_time_in(conn), self._times.now_in(conn)
            return tuple(_hold_view(hold, house) for hold in power_policy_in(conn, now).holds)

    def save_night_off(self, save: NightOffSave) -> NightOffView:
        draft = save.night_off
        with self._db.transaction() as conn:
            row = conn.execute("SELECT revision FROM night_off FOR UPDATE").fetchone()
            if row["revision"] != save.expected_revision:
                raise PowerRefused("changed", 409)
            problems = when_problems(draft.when, house_time_in(conn))
            if problems:
                raise PowerRefused(problems[0], 422)
            if not isinstance(draft.frames, AllFrames) and draft.frames.frame_ids:
                known = conn.execute("SELECT count(*) AS n FROM frames WHERE id = ANY(%s)",
                                     (list(draft.frames.frame_ids),)).fetchone()["n"]
                if known != len(draft.frames.frame_ids):
                    raise PowerRefused("unknown-frame", 422)
            conn.execute("UPDATE night_off SET settings=%s, revision=revision + 1, updated_at=%s",
                         (Jsonb(draft.model_dump(mode="json")), self._times.now_in(conn)))
            _wake_every_node(conn)
        return self.night_off()

    def all_off(self, request_id: UUID) -> HoldView:
        return self._hold(request_id, Power.OFF)

    def all_on(self, request_id: UUID) -> HoldView:
        return self._hold(request_id, Power.ON)

    def resume_schedule(self, frame_ids: tuple[str, ...]) -> None:
        with self._db.transaction() as conn:
            _forget_expired(conn, self._times.now_in(conn))
            conn.execute("DELETE FROM power_holds WHERE frame_id = ANY(%s)", (list(frame_ids),))
            _wake_every_node(conn)

    def _hold(self, request_id: UUID, power: Power) -> HoldView:
        """Hold every current Frame at `power` until `hold_end`. The newest hold on a Frame replaces
        an older one; a Frame already held by this request keeps its row (start included)."""
        with self._db.transaction() as conn:
            now, house, night_off = self._times.now_in(conn), house_time_in(conn), night_off_in(conn)
            _forget_expired(conn, now)
            ends_at = hold_end(night_off, house, now, night_off.hold_lasts)
            conn.execute(
                "INSERT INTO power_holds (frame_id, hold_id, power, source, started_at, ends_at) "
                "SELECT id, %s, %s, 'console', %s, %s FROM frames ON CONFLICT (frame_id) DO UPDATE SET "
                "hold_id=EXCLUDED.hold_id, power=EXCLUDED.power, source=EXCLUDED.source, "
                "started_at=EXCLUDED.started_at, ends_at=EXCLUDED.ends_at "
                "WHERE power_holds.hold_id <> EXCLUDED.hold_id",
                (request_id, power.value, now, ends_at))
            rows = conn.execute(f"SELECT frame_id, {_HOLD_COLUMNS} FROM power_holds WHERE hold_id=%s",  # noqa: S608
                                (request_id,)).fetchall()
            _wake_every_node(conn)
        held = _holds(rows)
        if not held:      # no Frames: an empty hold, stored nowhere
            return HoldView(hold_id=request_id, power=power, frame_ids=(), source=HoldSource(),
                            started_at=now, until_at=ends_at,
                            until=None if ends_at is None else local_date_time(house, ends_at))
        view = _hold_view(held[-1], house)          # the oldest part: the request's own start
        return view.model_copy(update={"frame_ids": tuple(sorted(f for hold in held for f in hold.frame_ids))})


__all__ = ["PgPower", "night_off_in", "power_policy_in", "wanted_off_in"]
