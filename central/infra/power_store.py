"""Display power policy over PostgreSQL (Power and the week: slice T for Night off, slice S2 for
holds; migration 090 `power_holds`, and since decision 0034's migration 118 the `singletons`
document 'power_policy': Night off and how long a hold lasts, its revision Night off's).

`power_policy_in` is the one read of the policy: the Output document source
(central/infra/display_store.py), Why and the shown-history filter all call it in their own
transaction. Each command wakes every Node's Output document source in its own transaction
(`NOTIFY OUTPUT_DOCUMENTS_CHANNEL`, from app code; no trigger), and the source also wakes at the
timetable's next set time (a hold's end among them) and its next horizon move, so a timetable
rolls forward with no other write. Night off's chosen Frames never name a deleted Frame
(`forget_frame_in`, in the delete's transaction).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from central.actor import Actor
from central.db import Database, DatabaseTransactionClock, TransactionClock
from central.house_time.calculator import local_date_time, next_window, when_problems, window_at
from central.house_time.model import HouseTime
from central.infra.house_store import house_settings_in, house_time_in
from central.infra.output_wakes import wake_every_node_in
from central.infra.singletons import Singleton
from central.power.model import (
    EVERY_DAY,
    NIGHT_OFF_OFF,
    NIGHT_OFF_ON,
    AllFrames,
    ChosenFrames,
    Hold,
    HoldLasts,
    HoldSource,
    NightOff,
    NightOffDraft,
    PowerPolicy,
)
from central.power.ports import PowerRefused
from central.power.timetable import hold_end, spans
from central.power.views import HoldLastsView, HoldView, NightOffSave, NightOffView
from contracts.node_output import Darkness, Power

_HOLD_COLUMNS = "hold_id, power, source, started_at, ends_at"
# Serializes the power policy's read-modify-writes (a Night off save's revision check, a hold
# lasts change, a Frame delete) without a row lock, so an equal save takes no transaction id.
POWER_POLICY_LOCK: Final = 734118331


class _PowerPolicyDocument(BaseModel):
    """`singletons` 'power_policy': Night off without its revision (the row's revision is Night
    off's) and how long a hold lasts."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    night_off: NightOffDraft
    hold_lasts: HoldLasts


POWER_POLICY: Final[Singleton[_PowerPolicyDocument]] = Singleton("power_policy", _PowerPolicyDocument)
# The policy migration 089 and 090 seeded, read when the document is missing or no longer
# validates (a hand edit), so a read never crashes; its first save stores it.
_SEED: Final = _PowerPolicyDocument(
    night_off=NightOffDraft(off=NIGHT_OFF_OFF, on=NIGHT_OFF_ON, days=EVERY_DAY,
                            frames=ChosenFrames(frame_ids=()), darkness=Darkness.DISPLAY_OFF),
    hold_lasts="next-scheduled-change")
_TIMES: Final = DatabaseTransactionClock()


def _document_in(conn: Any) -> tuple[_PowerPolicyDocument, int]:
    """The stored document and its revision, or the seed at revision 1."""
    stored = POWER_POLICY.read_in(conn)
    return (_SEED, 1) if stored is None else (stored.value, stored.revision)


def _lock_in(conn: Any) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (POWER_POLICY_LOCK,))


def night_off_in(conn: Any) -> NightOff:
    """The stored Night off, its revision the document's (migration 118 copies 089's row)."""
    document, revision = _document_in(conn)
    return NightOff(**document.night_off.model_dump(), revision=revision)


def forget_frame_in(conn: Any, frame_id: str, *, by: Actor) -> None:
    """Drop `frame_id` from Night off's chosen Frames, in the caller's transaction (the Registry's
    Frame delete): the one owner of which Frames follow Night off keeps naming only Frames that
    exist, so a later save is not refused "unknown-frame". The revision rises, so an editor open on
    the old list is told to reload."""
    _lock_in(conn)
    document, _ = _document_in(conn)
    frames = document.night_off.frames
    if not isinstance(frames, ChosenFrames) or frame_id not in frames.frame_ids:
        return
    kept = ChosenFrames(frame_ids=tuple(chosen for chosen in frames.frame_ids if chosen != frame_id))
    night_off = document.night_off.model_copy(update={"frames": kept})
    POWER_POLICY.write_in(conn, document.model_copy(update={"night_off": night_off}), by=by,
                          times=_TIMES)
    wake_every_node_in(conn)


def hold_lasts_in(conn: Any) -> HoldLasts:
    """The stored "a hold lasts" setting."""
    return _document_in(conn)[0].hold_lasts


def power_policy_in(conn: Any, now: float) -> PowerPolicy:
    """Night off, the holds live at Central's `now` (ends_at after it, or until resumed: one Hold
    per hold and end, its Frames sorted), whether the house's zone is confirmed and how long a
    hold lasts. An expired row is ignored here and deleted by the next command (never by a reaper
    or a trigger)."""
    rows = conn.execute(
        f"SELECT frame_id, {_HOLD_COLUMNS} FROM power_holds WHERE ends_at IS NULL OR ends_at > %s",  # noqa: S608
        (now,)).fetchall()
    return PowerPolicy(night_off_in(conn), holds=_holds(rows),
                       zone_confirmed=house_settings_in(conn).zone_confirmed, hold_lasts=hold_lasts_in(conn))


def wanted_off_in(conn: Any, frame_id: str, start: float, end: float) -> bool:
    """Slice S2 (F3): whether `frame_id`'s wanted power is off for the whole of [start, end) by the
    policy live at `start` (power.timetable `spans`); shown history ignores such a layer."""
    if not start < end:
        return False
    return all(span.power is Power.OFF
               for span in spans(frame_id, power_policy_in(conn, start), house_time_in(conn), start, end))


def _holds(rows: Iterable[Mapping[str, Any]]) -> tuple[Hold, ...]:
    """Rows to Holds, newest first, then the earliest end first (until resumed last). Rows are
    grouped by the whole hold (id, power, start, end): each Frame's hold ends at its own next
    change, so the Frames that end together read as one Hold, and a double tap that straddles a
    Frame's creation gives that Frame its own start, so it reads as a Hold of its own with the
    same id rather than moving the others' start."""
    grouped: dict[tuple[Any, ...], list[str]] = {}
    for row in rows:
        grouped.setdefault((row["hold_id"], row["power"], row["source"], row["started_at"], row["ends_at"]),
                           []).append(row["frame_id"])
    holds = [Hold(hold_id=hold_id, power=Power(power), frame_ids=tuple(sorted(frame_ids)),
                  source=HoldSource(kind=source), started_at=started_at, ends_at=ends_at)
             for (hold_id, power, source, started_at, ends_at), frame_ids in grouped.items()]
    return tuple(sorted(holds, key=lambda hold: (-hold.started_at, str(hold.hold_id), hold.ends_at is None,
                                                 hold.ends_at or 0.0)))


def _hold_view(hold: Hold, house: HouseTime) -> HoldView:
    return HoldView(hold_id=hold.hold_id, power=hold.power, frame_ids=hold.frame_ids, source=hold.source,
                    started_at=hold.started_at, until_at=hold.ends_at,
                    until=None if hold.ends_at is None else local_date_time(house, hold.ends_at))



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
            night_off, settings, now = night_off_in(conn), house_settings_in(conn), self._times.now_in(conn)
        house = settings.time
        try:
            following, current = next_window(night_off.when, house, now), window_at(night_off.when, house, now)
        except ValueError:          # no days, or a sun time with no Place: no times to show
            following = current = None
        # The next off is the next window's start; the next on ends the window in force, else the next.
        on_window = current if current is not None else following
        return NightOffView(
            night_off=night_off, waits_for_zone=not settings.zone_confirmed,
            next_off=None if following is None else local_date_time(house, following.start),
            next_on=None if on_window is None else local_date_time(house, on_window.end))

    def holds(self) -> tuple[HoldView, ...]:
        with self._db.transaction() as conn:
            house, now = house_time_in(conn), self._times.now_in(conn)
            return tuple(_hold_view(hold, house) for hold in power_policy_in(conn, now).holds)

    def hold_lasts(self) -> HoldLastsView:
        with self._db.transaction() as conn:
            return HoldLastsView(hold_lasts=hold_lasts_in(conn))

    def set_hold_lasts(self, hold_lasts: HoldLasts, *, by: Actor) -> HoldLastsView:
        # A hold in force keeps its end: no timetable changes, so no Node is woken.
        with self._db.transaction() as conn:
            _lock_in(conn)
            document, _ = _document_in(conn)
            POWER_POLICY.write_in(conn, document.model_copy(update={"hold_lasts": hold_lasts}),
                                  by=by, times=self._times)
        return self.hold_lasts()

    def save_night_off(self, save: NightOffSave, *, by: Actor) -> NightOffView:
        draft = save.night_off
        with self._db.transaction() as conn:
            _lock_in(conn)
            document, revision = _document_in(conn)
            if revision != save.expected_revision:
                raise PowerRefused("changed", 409)
            problems = when_problems(draft.when, house_time_in(conn))
            if problems:
                raise PowerRefused(problems[0], 422)
            if not isinstance(draft.frames, AllFrames) and draft.frames.frame_ids:
                known = conn.execute("SELECT count(*) AS n FROM frames WHERE id = ANY(%s)",
                                     (list(draft.frames.frame_ids),)).fetchone()["n"]
                if known != len(draft.frames.frame_ids):
                    raise PowerRefused("unknown-frame", 422)
            written = POWER_POLICY.write_in(conn, document.model_copy(update={"night_off": draft}),
                                            by=by, times=self._times)
            if written.revision != revision:      # an equal save changes nothing a Node reads
                wake_every_node_in(conn)
        return self.night_off()

    def all_off(self, request_id: UUID, *, by: Actor) -> tuple[HoldView, ...]:
        return self._hold(request_id, Power.OFF, by)

    def all_on(self, request_id: UUID, *, by: Actor) -> tuple[HoldView, ...]:
        return self._hold(request_id, Power.ON, by)

    def resume_schedule(self, frame_ids: tuple[str, ...]) -> None:
        with self._db.transaction() as conn:
            _forget_expired(conn, self._times.now_in(conn))
            conn.execute("DELETE FROM power_holds WHERE frame_id = ANY(%s)", (list(frame_ids),))
            wake_every_node_in(conn)

    def _hold(self, request_id: UUID, power: Power, by: Actor) -> tuple[HoldView, ...]:
        """Hold every current Frame at `power`, each until its own `hold_end`. The newest hold on a
        Frame replaces an older one; a Frame already held by this request keeps its row (start and
        end included)."""
        with self._db.transaction() as conn:
            now, house = self._times.now_in(conn), house_time_in(conn)
            _forget_expired(conn, now)
            policy = power_policy_in(conn, now)
            frame_ids = [row["id"] for row in conn.execute("SELECT id FROM frames ORDER BY id").fetchall()]
            ends = [hold_end(frame_id, policy, house, now) for frame_id in frame_ids]
            conn.execute(
                "INSERT INTO power_holds (frame_id, hold_id, power, source, started_at, ends_at, "
                "created_at, created_by, changed_at, changed_by) "
                "SELECT frame_id, %(hold)s, %(power)s, 'console', %(now)s, ends_at, "
                "%(now)s, %(by)s, %(now)s, %(by)s "
                "FROM unnest(%(frames)s::text[], %(ends)s::double precision[]) AS held(frame_id, ends_at) "
                "ON CONFLICT (frame_id) DO UPDATE SET "
                "hold_id=EXCLUDED.hold_id, power=EXCLUDED.power, source=EXCLUDED.source, "
                "started_at=EXCLUDED.started_at, ends_at=EXCLUDED.ends_at, "
                "changed_at=EXCLUDED.changed_at, changed_by=EXCLUDED.changed_by "
                "WHERE power_holds.hold_id <> EXCLUDED.hold_id",
                {"hold": request_id, "power": power.value, "now": now, "by": by,
                 "frames": frame_ids, "ends": ends})
            rows = conn.execute(f"SELECT frame_id, {_HOLD_COLUMNS} FROM power_holds WHERE hold_id=%s",  # noqa: S608
                                (request_id,)).fetchall()
            wake_every_node_in(conn)
        return tuple(_hold_view(hold, house) for hold in _holds(rows))


__all__ = ["POWER_POLICY", "POWER_POLICY_LOCK", "PgPower", "forget_frame_in", "hold_lasts_in",
           "night_off_in", "power_policy_in", "wanted_off_in"]
