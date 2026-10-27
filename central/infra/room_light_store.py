"""Room lights over PostgreSQL (migrations 099, 109, 111): `PgRoomLights` implements central/room_light/ports.py's
RoomLightReports (every reporting source's adapter), RoomLightSetup (House › Lights),
RoomLightsDocuments (the worker's WALL writer) and DrawnReports (each Player's drawn report), and
the Home Assistant adapter's HeardEntities (central/infra/home_assistant/states.py: each entity Home
Assistant published, its kind, name and when Central last heard it, home_assistant_entities).

Every reference's facts are kept by its reference (light_words, reading_words, colour_words: the
last stated value and when, its doubt (why and since when), when last heard; policy.py's
`*_facts_after` and `doubted` decide them) whether or not a room light, Sensor or colour sensor names it, and are resolved only when
the document is built (policy.room_lights_document). Each source keeps its newest
WORDS_KEPT_PER_SOURCE references and REFUSALS_KEPT refusals.

Every write that changes the room-lights document raises `room_lights_change.change` in its own
transaction, under that row's lock (the house's lock, taken first through the HouseLock
the store is handed, central/infra/frame_lighting_store.py), and sends `NOTIFY ROOM_LIGHTS_CHANNEL`, delivered at commit: the worker's WALL writer listens
and puts the document again. Every instant is PostgreSQL's `clock_timestamp()` (Central's one clock).
No trigger, no stored procedure, no range in SQL: the rules are central/room_light/policy.py's and
the ranges pydantic's.

A doubt with no report behind it is written here too, under the same lock and wake: an unreadable
refusal doubts every reference at its (source, address) "can't read" (one a setup item names, or
one with facts), and a closed link doubts every reference of its source with facts "link closed".
The words that travel for a doubt are dated on the house's clock (its zone and 12- or 24-hour clock,
central/infra/house_store.py), so a Settings › House save that changes them raises the change too
(`house_clock_changing_within`, which PgHouse calls).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from central.db import Database
from central.frame_lighting.ports import HouseLock
from central.house_time.model import HouseClock
from central.infra.house_store import house_settings_in
from central.infra.light_sensor_judge import PI_SOURCE, missed_readings_in
from central.infra.room_light_rows import ROOM_LIGHT, lights_in
from central.infra.transactions import PgTransaction, pg_connection
from central.kernel.transactions import Transaction
from central.room_light.model import (
    ADDRESS_MAX,
    ColourFacts,
    ColourReading,
    ColourSensor,
    ColourSensorId,
    ColourSensorRef,
    Doubt,
    DoubtReason,
    Facts,
    FixedLight,
    Kept,
    LightFacts,
    LightReport,
    MeasuredLight,
    ReadingFacts,
    Recorded,
    ReportedLight,
    ReporterRef,
    ReportRefusal,
    RoomLight,
    RoomLightId,
    Sensor,
    SensorId,
    SensorRef,
    Source,
)
from central.room_light.policy import (
    check_colour_sensor,
    check_colour_sensor_removal,
    check_room_light,
    check_room_light_removal,
    check_sensor,
    check_sensor_removal,
    colour_facts_after,
    colour_line,
    doubt_view,
    doubted,
    drawn_state,
    entry_of,
    followed_by,
    light_facts_after,
    measured_by,
    reading_facts_after,
    room_lights_document,
)
from central.room_light.views import (
    ColourSensorView,
    DrawnView,
    PlayerDrawnView,
    RefusedReportsView,
    RefusedReportView,
    RoomLightsView,
    RoomLightView,
    SensorView,
)
from contracts.home_assistant import HOME_ASSISTANT_ENTITIES_MAX, GalleriaBirth
from contracts.room_light import (
    Dimmer,
    KelvinColour,
    LightColour,
    Measured,
    Reading,
    RoomLightsDocument,
    RoomLightsDrawn,
    room_lights_digest,
)

ROOM_LIGHTS_CHANNEL: Final = "room_lights"     # NOTIFY payload: empty; the WALL writer re-reads the document
REFUSALS_KEPT: Final = 20                      # the newest refused reports kept per source
WORDS_KEPT_PER_SOURCE: Final = 256             # the newest references' words kept per source
# Who writes a row, for its audit fields (created_by, changed_by; there are no accounts): the console's
# operator for setup, Galleria for its birth, a reference's own source for what it stated, and
# Central for a doubt it set itself (a refused report, a closed link).
OPERATOR: Final = "operator"
GALLERIA: Final = "galleria"
CENTRAL: Final = "central"

_COLOUR: Final = TypeAdapter(LightColour)


def sensors_in(conn: Any) -> dict[SensorId, Sensor]:
    rows = conn.execute("SELECT id, sensor FROM sensors ORDER BY id").fetchall()
    return {row["id"]: Sensor.model_validate(row["sensor"]) for row in rows}


def colour_sensors_in(conn: Any) -> dict[ColourSensorId, ColourSensor]:
    rows = conn.execute("SELECT id, sensor FROM colour_sensors ORDER BY id").fetchall()
    return {row["id"]: ColourSensor.model_validate(row["sensor"]) for row in rows}


def _doubt(row: Any) -> Doubt | None:
    return None if row["doubt_reason"] is None else Doubt(DoubtReason(row["doubt_reason"]), float(row["doubt_since"]))


def _kept[T](value: T | None, at: Any) -> Kept[T] | None:
    return None if value is None else Kept(value, float(at))


def _epochs(*columns: str) -> str:
    return ", ".join(f"EXTRACT(EPOCH FROM {column}) AS {column}" for column in columns)


def _light_facts(row: Any) -> LightFacts:
    return LightFacts(
        state=_kept(None if row["state"] is None else Dimmer.model_validate(row["state"]), row["state_at"]),
        colour=_kept(None if row["colour"] is None else _COLOUR.validate_python(row["colour"]), row["colour_at"]),
        doubt=_doubt(row), heard_at=float(row["heard_at"]))


def _reading_facts(row: Any) -> ReadingFacts:
    return ReadingFacts(
        reading=_kept(None if row["reading"] is None else Measured.model_validate(row["reading"]), row["reading_at"]),
        doubt=_doubt(row), heard_at=float(row["heard_at"]))


def _colour_facts(row: Any) -> ColourFacts:
    return ColourFacts(
        colour=_kept(None if row["colour"] is None else KelvinColour.model_validate(row["colour"]), row["colour_at"]),
        doubt=_doubt(row), heard_at=float(row["heard_at"]))


_LIGHT_SELECT: Final = (f"SELECT source, address, state, colour, doubt_reason, "
                        f"{_epochs('state_at', 'colour_at', 'doubt_since', 'heard_at')} FROM light_words")
_READING_SELECT: Final = (f"SELECT source, address, reading, doubt_reason, "
                          f"{_epochs('reading_at', 'doubt_since', 'heard_at')} FROM reading_words")
_COLOUR_SELECT: Final = (f"SELECT source, address, colour, doubt_reason, "
                         f"{_epochs('colour_at', 'doubt_since', 'heard_at')} FROM colour_words")


def facts_in(conn: Any) -> Facts:
    """Every reference's kept facts."""
    return Facts(
        lights={ReporterRef(source=row["source"], address=row["address"]): _light_facts(row)
                for row in conn.execute(_LIGHT_SELECT).fetchall()},
        readings={SensorRef(source=row["source"], address=row["address"]): _reading_facts(row)
                  for row in conn.execute(_READING_SELECT).fetchall()},
        colours={ColourSensorRef(source=row["source"], address=row["address"]): _colour_facts(row)
                 for row in conn.execute(_COLOUR_SELECT).fetchall()})


def _clock_in(conn: Any) -> HouseClock:
    """The house's clock (its zone and 12- or 24-hour clock), the one a doubt's words are dated on."""
    settings = house_settings_in(conn)
    return HouseClock(ZoneInfo(settings.time.zone), settings.clock)


def document_in(conn: Any) -> RoomLightsDocument:
    """The room-lights document now (policy.room_lights_document over the stored rows)."""
    change = int(conn.execute("SELECT change FROM room_lights_change WHERE one").fetchone()["change"])
    return room_lights_document(lights_in(conn), sensors_in(conn), colour_sensors_in(conn), facts_in(conn), change,
                                _clock_in(conn))


def _changed_in(conn: Any, before: RoomLightsDocument) -> bool:
    """When the document's lights differ from `before`'s: raise its change and wake the WALL writer
    (at commit). True when it did."""
    if dict(document_in(conn).lights) == dict(before.lights):
        return False
    conn.execute("UPDATE room_lights_change SET change = change + 1 WHERE one")
    conn.execute("SELECT pg_notify(%s, '')", (ROOM_LIGHTS_CHANNEL,))
    return True


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value.model_dump(mode="json"))


def _now_in(conn: Any) -> float:
    """Central's clock now (epoch seconds), the instant every fact this write keeps is stamped with."""
    return float(conn.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now").fetchone()["now"])


def _kept_in(conn: Any, house: HouseLock, table: str, source: str, address: str,
             write: Any) -> Recorded:
    """Under the house's lock: `write(now)` upserts (source, address)'s facts in `table`; then of that
    source's references no room light, Sensor or colour sensor names, only those among its newest
    WORDS_KEPT_PER_SOURCE stay (a named reference's facts are never trimmed); "recorded" when the
    document changed."""
    house.changing_within(PgTransaction(conn))
    before = document_in(conn)
    write(_now_in(conn))
    named = sorted({reference.address for reference in named_in(conn, source)})
    conn.execute(f"DELETE FROM {table} WHERE source=%(source)s AND NOT (address = ANY(%(named)s)) "  # noqa: S608
                 f"AND address NOT IN (SELECT address FROM {table} WHERE source=%(source)s "
                 "ORDER BY heard_at DESC, address LIMIT %(keep)s)",
                 {"source": source, "named": named, "keep": WORDS_KEPT_PER_SOURCE})
    return "recorded" if _changed_in(conn, before) else "unchanged"


def _put_words(conn: Any, table: str, source: str, address: str, values: dict[str, Any], instants: frozenset[str],
               now: float, by: str) -> None:
    """Upsert (source, address)'s row of `table` with `values` (column -> value; the `instants`
    columns as epoch seconds) and its audit fields: created at `now` by `by` when new, changed at
    `now` by `by` always."""
    columns = list(values)
    placeholders = ", ".join(f"to_timestamp(%({column})s)" if column in instants else f"%({column})s"
                             for column in columns)
    updates = ", ".join(f"{column}=EXCLUDED.{column}" for column in (*columns, "changed_at", "changed_by"))
    conn.execute(
        f"INSERT INTO {table}(source, address, {', '.join(columns)}, created_at, created_by, changed_at, "  # noqa: S608
        f"changed_by) VALUES (%(source)s, %(address)s, {placeholders}, to_timestamp(%(now)s), %(by)s, "
        f"to_timestamp(%(now)s), %(by)s) ON CONFLICT (source, address) DO UPDATE SET {updates}",
        {**values, "source": source, "address": address, "now": now, "by": by})


def _put_light(conn: Any, reporter: ReporterRef, facts: LightFacts, now: float, by: str) -> None:
    reason, since = _doubt_columns(facts.doubt)
    _put_words(conn, "light_words", reporter.source, reporter.address, {
        "state": _json(facts.state and facts.state.value), "state_at": facts.state and facts.state.at,
        "colour": _json(facts.colour and facts.colour.value), "colour_at": facts.colour and facts.colour.at,
        "doubt_reason": reason, "doubt_since": since, "heard_at": facts.heard_at},
        frozenset({"state_at", "colour_at", "doubt_since", "heard_at"}), now, by)


def _put_reading(conn: Any, sensor: SensorRef, facts: ReadingFacts, now: float, by: str) -> None:
    reason, since = _doubt_columns(facts.doubt)
    _put_words(conn, "reading_words", sensor.source, sensor.address, {
        "reading": _json(facts.reading and facts.reading.value), "reading_at": facts.reading and facts.reading.at,
        "doubt_reason": reason, "doubt_since": since, "heard_at": facts.heard_at},
        frozenset({"reading_at", "doubt_since", "heard_at"}), now, by)


def _put_colour(conn: Any, sensor: ColourSensorRef, facts: ColourFacts, now: float, by: str) -> None:
    reason, since = _doubt_columns(facts.doubt)
    _put_words(conn, "colour_words", sensor.source, sensor.address, {
        "colour": _json(facts.colour and facts.colour.value), "colour_at": facts.colour and facts.colour.at,
        "doubt_reason": reason, "doubt_since": since, "heard_at": facts.heard_at},
        frozenset({"colour_at", "doubt_since", "heard_at"}), now, by)


def _doubt_columns(doubt: Doubt | None) -> tuple[str | None, float | None]:
    return (None, None) if doubt is None else (doubt.reason.value, doubt.since)


def _light_in(conn: Any, house: HouseLock, reporter: ReporterRef, report: LightReport) -> Recorded:
    def write(now: float) -> None:
        row = conn.execute(f"{_LIGHT_SELECT} WHERE source=%s AND address=%s",  # noqa: S608
                           (reporter.source, reporter.address)).fetchone()
        _put_light(conn, reporter, light_facts_after(None if row is None else _light_facts(row), report, now), now,
                   reporter.source)
    return _kept_in(conn, house, "light_words", reporter.source, reporter.address, write)


def _reading_in(conn: Any, house: HouseLock, sensor: SensorRef, reading: Reading) -> Recorded:
    def write(now: float) -> None:
        row = conn.execute(f"{_READING_SELECT} WHERE source=%s AND address=%s",  # noqa: S608
                           (sensor.source, sensor.address)).fetchone()
        _put_reading(conn, sensor, reading_facts_after(None if row is None else _reading_facts(row), reading, now), now,
                     sensor.source)
    return _kept_in(conn, house, "reading_words", sensor.source, sensor.address, write)


def _colour_in(conn: Any, house: HouseLock, sensor: ColourSensorRef, reading: ColourReading) -> Recorded:
    def write(now: float) -> None:
        row = conn.execute(f"{_COLOUR_SELECT} WHERE source=%s AND address=%s",  # noqa: S608
                           (sensor.source, sensor.address)).fetchone()
        _put_colour(conn, sensor, colour_facts_after(None if row is None else _colour_facts(row), reading, now), now,
                    sensor.source)
    return _kept_in(conn, house, "colour_words", sensor.source, sensor.address, write)


def _doubts_in(conn: Any, house: HouseLock, source: Source, address: str | None, reason: DoubtReason) -> Recorded:
    """Under the house's lock: every reference of `source` (at `address` only, when given) that has
    facts takes `reason`'s doubt (policy.doubted), its last word standing; for "can't read" (the
    source spoke), so does every such reference a setup item names that has none yet. "recorded"
    when the document changed."""
    house.changing_within(PgTransaction(conn))
    before = document_in(conn)
    now = _now_in(conn)
    facts = facts_in(conn)
    named = named_in(conn, source) if reason == DoubtReason.CANT_READ else {}

    def mine[R: (ReporterRef, SensorRef, ColourSensorRef)](kept: Any, kind: type[R]) -> list[R]:
        found = {reference for reference in kept if reference.source == source}
        found |= {reference for reference in named if isinstance(reference, kind)}
        return sorted((reference for reference in found if address is None or reference.address == address),
                      key=lambda reference: reference.address)

    for reporter in mine(facts.lights, ReporterRef):
        _put_light(conn, reporter, doubted(facts.lights.get(reporter), reason, now,
                                           LightFacts(state=None, colour=None, doubt=None, heard_at=now)), now, CENTRAL)
    for sensor in mine(facts.readings, SensorRef):
        _put_reading(conn, sensor, doubted(facts.readings.get(sensor), reason, now,
                                           ReadingFacts(reading=None, doubt=None, heard_at=now)), now, CENTRAL)
    for colour_sensor in mine(facts.colours, ColourSensorRef):
        _put_colour(conn, colour_sensor, doubted(facts.colours.get(colour_sensor), reason, now,
                                                 ColourFacts(colour=None, doubt=None, heard_at=now)), now, CENTRAL)
    return "recorded" if _changed_in(conn, before) else "unchanged"


def _refusal_in(conn: Any, source: Source, address: str, reason: ReportRefusal) -> None:
    """Keep one refused report and the newest REFUSALS_KEPT of `source`. The address is cut to
    ADDRESS_MAX here and only here: a refused report's text is the source's own and may be anything."""
    conn.execute("INSERT INTO report_refusals(source, address, reason, at) "
                 "VALUES (%s, %s, %s, clock_timestamp())", (source, address[:ADDRESS_MAX], reason.value))
    conn.execute("DELETE FROM report_refusals WHERE source=%(source)s AND id NOT IN (SELECT id FROM "
                 "report_refusals WHERE source=%(source)s ORDER BY id DESC LIMIT %(keep)s)",
                 {"source": source, "keep": REFUSALS_KEPT})


def _refused_in(conn: Any, house: HouseLock, source: Source, address: str, reason: ReportRefusal) -> None:
    """The refusal in the one log; an unreadable one also doubts its references "can't read"."""
    _refusal_in(conn, source, address, reason)
    if reason == ReportRefusal.UNREADABLE:
        _doubts_in(conn, house, source, address[:ADDRESS_MAX], DoubtReason.CANT_READ)


class PgRoomLights:
    """Room lights' ports over one Database."""

    def __init__(self, db: Database, house: HouseLock) -> None:
        """`house` is the house's lock and its "which Frames name it" query (every change here takes
        the lock first)."""
        self._db = db
        self._house = house

    # ---- RoomLightReports ----

    def record_light_state(self, reporter: ReporterRef, report: LightReport) -> Recorded:
        with self._db.transaction() as conn:
            return _light_in(conn, self._house, reporter, report)

    def record_reading(self, sensor: SensorRef, reading: Reading) -> Recorded:
        with self._db.transaction() as conn:
            return _reading_in(conn, self._house, sensor, reading)

    def record_colour_reading(self, sensor: ColourSensorRef, reading: ColourReading) -> Recorded:
        with self._db.transaction() as conn:
            return _colour_in(conn, self._house, sensor, reading)

    def record_reading_within(self, tx: Transaction, sensor: SensorRef, reading: Reading) -> Recorded:
        conn = pg_connection(tx)
        with conn.transaction():
            return _reading_in(conn, self._house, sensor, reading)

    def record_refused(self, source: Source, address: str, reason: ReportRefusal) -> None:
        with self._db.transaction() as conn:
            _refused_in(conn, self._house, source, address, reason)

    def record_refused_within(self, tx: Transaction, source: Source, address: str, reason: ReportRefusal) -> None:
        conn = pg_connection(tx)
        with conn.transaction():
            _refused_in(conn, self._house, source, address, reason)

    def record_link_closed(self, source: Source) -> Recorded:
        with self._db.transaction() as conn:
            return _doubts_in(conn, self._house, source, None, DoubtReason.LINK_CLOSED)

    # ---- HouseClockChanges (Settings › House) ----

    def house_clock_changing_within(self, tx: Transaction, write: Callable[[], None]) -> Recorded:
        conn = pg_connection(tx)
        self._house.changing_within(tx)
        before = document_in(conn)
        write()
        return "recorded" if _changed_in(conn, before) else "unchanged"

    # ---- RoomLightsDocuments ----

    def current(self) -> RoomLightsDocument:
        """First takes every Pi reading the host line recorded and its judge did not keep (its
        savepoint failed), so the host line's record, not reading_words, decides a Pi's reading."""
        with self._db.transaction() as conn:
            for player_id, reading in missed_readings_in(conn):
                _reading_in(conn, self._house, SensorRef(source=PI_SOURCE, address=player_id), reading)
            return document_in(conn)

    # ---- DrawnReports ----

    def record_drawn_within(self, tx: Transaction, player_id: str, report: RoomLightsDrawn) -> None:
        conn = pg_connection(tx)
        with conn.transaction():
            conn.execute("INSERT INTO room_lights_drawn(player_id, drawn, following, reported_at) "
                         "VALUES (%s, %s, %s, clock_timestamp()) ON CONFLICT (player_id) DO UPDATE "
                         "SET drawn=EXCLUDED.drawn, following=EXCLUDED.following, reported_at=EXCLUDED.reported_at",
                         (player_id, report.drawn, report.following))

    def record_drawn(self, player_id: str, report: RoomLightsDrawn) -> None:
        """`record_drawn_within` in a transaction of its own (a test's seeding; the judge records
        within the Player line's record)."""
        with self._db.transaction() as conn:
            self.record_drawn_within(PgTransaction(conn), player_id, report)

    # ---- HeardEntities (the Home Assistant adapter) ----

    def record_heard(self, entity: str, kind: str, name: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO home_assistant_entities(entity, kind, name, heard_at) "
                         "VALUES (%s, %s, %s, clock_timestamp()) ON CONFLICT (entity) DO UPDATE "
                         "SET kind=EXCLUDED.kind, name=EXCLUDED.name, heard_at=EXCLUDED.heard_at",
                         (entity, kind, name))
            conn.execute("DELETE FROM home_assistant_entities WHERE entity NOT IN (SELECT entity FROM "
                         "home_assistant_entities ORDER BY heard_at DESC, entity LIMIT %s)",
                         (HOME_ASSISTANT_ENTITIES_MAX,))

    # ---- GalleriaBirths (the Home Assistant adapter) ----

    def record_galleria_birth(self, birth: GalleriaBirth) -> None:
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO galleria_birth(one, birth, heard_at, created_at, created_by, changed_at, "
                         "changed_by) VALUES (true, %(birth)s, clock_timestamp(), clock_timestamp(), %(by)s, "
                         "clock_timestamp(), %(by)s) "
                         "ON CONFLICT (one) DO UPDATE SET birth=EXCLUDED.birth, heard_at=EXCLUDED.heard_at, "
                         "changed_at=EXCLUDED.changed_at, changed_by=EXCLUDED.changed_by",
                         {"birth": Jsonb(birth.model_dump(mode="json")), "by": GALLERIA})

    def galleria_birth(self) -> GalleriaBirth | None:
        with self._db.transaction() as conn:
            kept = galleria_birth_in(conn)
        return None if kept is None else kept[0]

    # ---- RoomLightSetup ----

    def lights(self) -> RoomLightsView:
        with self._db.transaction() as conn:
            return _view_in(conn, self._house)

    def drawn(self) -> DrawnView:
        with self._db.transaction() as conn:
            digest_now = room_lights_digest(document_in(conn))
            rows = conn.execute("SELECT player_id, drawn, following, EXTRACT(EPOCH FROM reported_at) AS reported_at, "
                                "EXTRACT(EPOCH FROM clock_timestamp() - reported_at) AS age "
                                "FROM room_lights_drawn ORDER BY player_id").fetchall()
        return DrawnView(players=tuple(PlayerDrawnView(
            player_id=row["player_id"], reported_at=float(row["reported_at"]),
            state=drawn_state(RoomLightsDrawn(drawn=row["drawn"], following=row["following"]), float(row["age"]),
                              digest_now)) for row in rows))

    def refused(self) -> RefusedReportsView:
        with self._db.transaction() as conn:
            return RefusedReportsView(refused=refused_in(conn, None))

    def save_room_light(self, light_id: RoomLightId, light: RoomLight) -> RoomLightView:
        with self._db.transaction() as conn:
            tx = PgTransaction(conn)
            self._house.changing_within(tx)
            lights = lights_in(conn)
            refused = check_room_light(light_id, light, lights, sensors_in(conn), colour_sensors_in(conn),
                                       self._house.naming_within(tx).lights.get(light_id, ()))
            if refused is not None:
                raise refused
            before = document_in(conn)
            conn.execute("INSERT INTO room_lights(id, light, position) "
                         "SELECT %s, %s, COALESCE(max(position), -1) + 1 FROM room_lights "
                         "ON CONFLICT (id) DO UPDATE SET light=EXCLUDED.light",
                         (light_id, Jsonb(ROOM_LIGHT.dump_python(light, mode="json"))))
            _changed_in(conn, before)
            return next(view for view in _view_in(conn, self._house).lights if view.id == light_id)

    def remove_room_light(self, light_id: RoomLightId) -> None:
        with self._db.transaction() as conn:
            tx = PgTransaction(conn)
            self._house.changing_within(tx)
            refused = check_room_light_removal(light_id, lights_in(conn),
                                               self._house.naming_within(tx).lights.get(light_id, ()))
            if refused is not None:
                raise refused
            before = document_in(conn)
            conn.execute("DELETE FROM room_lights WHERE id=%s", (light_id,))
            _changed_in(conn, before)

    def save_sensor(self, sensor_id: SensorId, sensor: Sensor) -> SensorView:
        with self._db.transaction() as conn:
            self._house.changing_within(PgTransaction(conn))
            refused = check_sensor(sensor_id, sensor, sensors_in(conn), lights_in(conn), colour_sensors_in(conn))
            if refused is not None:
                raise refused
            before = document_in(conn)
            conn.execute("INSERT INTO sensors(id, sensor) VALUES (%s, %s) "
                         "ON CONFLICT (id) DO UPDATE SET sensor=EXCLUDED.sensor",
                         (sensor_id, Jsonb(sensor.model_dump(mode="json"))))
            _changed_in(conn, before)
            return next(view for view in _view_in(conn, self._house).sensors if view.id == sensor_id)

    def remove_sensor(self, sensor_id: SensorId) -> None:
        with self._db.transaction() as conn:
            self._house.changing_within(PgTransaction(conn))
            refused = check_sensor_removal(sensor_id, sensors_in(conn), lights_in(conn))
            if refused is not None:
                raise refused
            conn.execute("DELETE FROM sensors WHERE id=%s", (sensor_id,))

    def save_colour_sensor(self, sensor_id: ColourSensorId, sensor: ColourSensor) -> ColourSensorView:
        with self._db.transaction() as conn:
            self._house.changing_within(PgTransaction(conn))
            refused = check_colour_sensor(sensor_id, sensor, colour_sensors_in(conn), lights_in(conn),
                                          sensors_in(conn))
            if refused is not None:
                raise refused
            before = document_in(conn)
            conn.execute("INSERT INTO colour_sensors(id, sensor, created_at, created_by, changed_at, changed_by) "
                         "VALUES (%(id)s, %(sensor)s, clock_timestamp(), %(by)s, clock_timestamp(), %(by)s) "
                         "ON CONFLICT (id) DO UPDATE SET sensor=EXCLUDED.sensor, changed_at=EXCLUDED.changed_at, "
                         "changed_by=EXCLUDED.changed_by",
                         {"id": sensor_id, "sensor": Jsonb(sensor.model_dump(mode="json")), "by": OPERATOR})
            _changed_in(conn, before)
            return next(view for view in _view_in(conn, self._house).colour_sensors if view.id == sensor_id)

    def remove_colour_sensor(self, sensor_id: ColourSensorId) -> None:
        with self._db.transaction() as conn:
            self._house.changing_within(PgTransaction(conn))
            refused = check_colour_sensor_removal(sensor_id, colour_sensors_in(conn), lights_in(conn))
            if refused is not None:
                raise refused
            conn.execute("DELETE FROM colour_sensors WHERE id=%s", (sensor_id,))


def _view_in(conn: Any, house: HouseLock) -> RoomLightsView:
    lights, sensors, colour_sensors, facts = lights_in(conn), sensors_in(conn), colour_sensors_in(conn), facts_in(conn)
    clock = _clock_in(conn)
    named = house.naming_within(PgTransaction(conn)).lights

    def reporter_facts(light: RoomLight) -> LightFacts | ReadingFacts | None:
        if isinstance(light, FixedLight):
            return None
        if isinstance(light, MeasuredLight):
            sensor = sensors.get(light.sensor)
            return None if sensor is None else facts.readings.get(sensor.at)
        return facts.lights.get(light.reporter)

    def light_view(light_id: RoomLightId, light: RoomLight) -> RoomLightView:
        kept = reporter_facts(light)
        return RoomLightView(id=light_id, light=light, entry=entry_of(light, facts, sensors, colour_sensors, clock),
                             heard_at=None if kept is None else kept.heard_at,
                             doubt=None if kept is None else doubt_view(kept.doubt),
                             colour=colour_line(light, facts, colour_sensors), named_by=named.get(light_id, ()))

    def sensor_view(sensor_id: SensorId, sensor: Sensor) -> SensorView:
        kept = facts.readings.get(sensor.at)
        return SensorView(id=sensor_id, sensor=sensor,
                          reading=None if kept is None or kept.reading is None else kept.reading.value,
                          heard_at=None if kept is None else kept.heard_at,
                          doubt=None if kept is None else doubt_view(kept.doubt),
                          reports=measured_by(lights, sensor_id))

    def colour_sensor_view(sensor_id: ColourSensorId, sensor: ColourSensor) -> ColourSensorView:
        kept = facts.colours.get(sensor.at)
        return ColourSensorView(id=sensor_id, sensor=sensor,
                                colour=None if kept is None or kept.colour is None else kept.colour.value,
                                heard_at=None if kept is None else kept.heard_at,
                                doubt=None if kept is None else doubt_view(kept.doubt),
                                followed_by=followed_by(lights, sensor_id))

    return RoomLightsView(
        lights=tuple(light_view(light_id, light) for light_id, light in lights.items()),
        sensors=tuple(sensor_view(sensor_id, sensor) for sensor_id, sensor in sensors.items()),
        colour_sensors=tuple(colour_sensor_view(sensor_id, sensor) for sensor_id, sensor in colour_sensors.items()))


def refused_in(conn: Any, source: Source | None) -> tuple[RefusedReportView, ...]:
    """The newest refused reports (of `source` only, when given), newest first."""
    columns = "SELECT source, address, reason, EXTRACT(EPOCH FROM at) AS at FROM report_refusals"
    rows = (conn.execute(f"{columns} ORDER BY id DESC") if source is None  # noqa: S608
            else conn.execute(f"{columns} WHERE source=%s ORDER BY id DESC", (source,))).fetchall()  # noqa: S608
    return tuple(RefusedReportView(source=row["source"], address=row["address"], reason=ReportRefusal(row["reason"]),
                                   at=float(row["at"])) for row in rows)


def named_in(conn: Any, source: Source) -> dict[ReporterRef | SensorRef | ColourSensorRef, str]:
    """Every reference of `source` a room light reports by (ReporterRef), a Sensor is (SensorRef) or a
    colour sensor is (ColourSensorRef), with that room light's, Sensor's or colour sensor's id, room
    lights first in House > Lights order, then Sensors by id, then colour sensors by id."""
    named: dict[ReporterRef | SensorRef | ColourSensorRef, str] = {}
    for light_id, light in lights_in(conn).items():
        if isinstance(light, ReportedLight) and light.reporter.source == source:
            named[light.reporter] = light_id
    for sensor_id, sensor in sensors_in(conn).items():
        if sensor.at.source == source:
            named[sensor.at] = sensor_id
    for sensor_id, colour_sensor in colour_sensors_in(conn).items():
        if colour_sensor.at.source == source:
            named[colour_sensor.at] = sensor_id
    return named


@dataclass(frozen=True)
class HeardEntity:
    """One entity Home Assistant published or a room light or Sensor names: its kind, its published
    name (None: never published), the room light or Sensor it reports for (None: none) and when
    Central last heard it (None: never)."""
    kind: str
    entity: str
    name: str | None
    reports: str | None
    heard_at: float | None


def heard_entities_in(conn: Any, source: Source) -> tuple[HeardEntity, ...]:
    """Every entity Home Assistant published (home_assistant_entities) and every reference of `source`
    a room light or Sensor names, newest heard first, then the never heard by entity id. A reporter
    and a Sensor at one entity id are two entries (by kind)."""
    rows = conn.execute("SELECT entity, kind, name, EXTRACT(EPOCH FROM heard_at) AS heard_at "
                        "FROM home_assistant_entities").fetchall()
    found: dict[tuple[str, str], HeardEntity] = {
        (row["kind"], row["entity"]): HeardEntity(row["kind"], row["entity"], row["name"], None,
                                                  float(row["heard_at"])) for row in rows}
    for reference, named in named_in(conn, source).items():
        kind = ("light" if isinstance(reference, ReporterRef)
                else "illuminance" if isinstance(reference, SensorRef) else "colour-temperature")
        held = found.get((kind, reference.address))
        found[(kind, reference.address)] = HeardEntity(
            kind, reference.address, None if held is None else held.name, named,
            None if held is None else held.heard_at)
    return tuple(sorted(found.values(), key=lambda each: (each.heard_at is None, -(each.heard_at or 0.0),
                                                          each.entity, each.kind)))


def galleria_birth_in(conn: Any) -> tuple[GalleriaBirth, float] | None:
    """Galleria's last birth and when Central heard it (None: none heard)."""
    row = conn.execute("SELECT birth, EXTRACT(EPOCH FROM heard_at) AS heard_at FROM galleria_birth WHERE one").fetchone()
    return None if row is None else (GalleriaBirth.model_validate(row["birth"]), float(row["heard_at"]))


def last_heard_in(conn: Any) -> float | None:
    """When Central last heard any of Home Assistant's states (None: never)."""
    row = conn.execute("SELECT EXTRACT(EPOCH FROM max(heard_at)) AS heard_at FROM home_assistant_entities").fetchone()
    return None if row["heard_at"] is None else float(row["heard_at"])


__all__ = ["REFUSALS_KEPT", "ROOM_LIGHTS_CHANNEL", "WORDS_KEPT_PER_SOURCE", "HeardEntity", "PgRoomLights",
           "colour_sensors_in", "document_in", "facts_in", "galleria_birth_in", "heard_entities_in", "last_heard_in",
           "named_in", "refused_in", "sensors_in"]
