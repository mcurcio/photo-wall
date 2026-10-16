"""Room lights over PostgreSQL (migration 099): `PgRoomLights` implements central/room_light/ports.py's
RoomLightReports (every reporting source's adapter), RoomLightSetup (House › Lights),
RoomLightsDocuments (the worker's WALL writer) and DrawnReports (each Player's drawn report).

Every word is kept by its reference (light_words, reading_words) whether or not a room light or
Sensor names it, and is resolved only when the document is built (policy.room_lights_document). Each
source keeps its newest WORDS_KEPT_PER_SOURCE references and REFUSALS_KEPT refusals.

Every write that changes the room-lights document raises `room_lights_change.change` in its own
transaction, under that row's lock (the house's lock, central/infra/frame_lighting_store.py, taken
first), and sends `NOTIFY ROOM_LIGHTS_CHANNEL`, delivered at commit: the worker's WALL writer listens
and puts the document again. Every instant is PostgreSQL's `clock_timestamp()` (Central's one clock).
No trigger, no stored procedure, no range in SQL: the rules are central/room_light/policy.py's and
the ranges pydantic's.
"""
from __future__ import annotations

from typing import Any, Final

from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from central.db import Database
from central.infra.frame_lighting_store import naming_in, take_house_lock
from central.infra.room_light_rows import ROOM_LIGHT, lights_in
from central.infra.transactions import pg_connection
from central.kernel.transactions import Transaction
from central.room_light.model import (
    ADDRESS_MAX,
    FixedLight,
    LightWord,
    MeasuredLight,
    ReadingWord,
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
    Words,
)
from central.room_light.policy import (
    check_room_light,
    check_room_light_removal,
    check_sensor,
    check_sensor_removal,
    drawn_state,
    entry_of,
    measured_by,
    room_lights_document,
)
from central.room_light.views import (
    DrawnView,
    PlayerDrawnView,
    RefusedReportsView,
    RefusedReportView,
    RoomLightsView,
    RoomLightView,
    SensorView,
)
from contracts.room_light import (
    ColourDimmer,
    Dimmer,
    Measured,
    RoomLightsDocument,
    RoomLightsDrawn,
    Unavailable,
    room_lights_digest,
)

ROOM_LIGHTS_CHANNEL: Final = "room_lights"     # NOTIFY payload: empty; the WALL writer re-reads the document
REFUSALS_KEPT: Final = 20                      # the newest refused reports kept per source
WORDS_KEPT_PER_SOURCE: Final = 256             # the newest references' words kept per source

_LIGHT_WORD: Final = TypeAdapter(LightWord)
_READING_WORD: Final = TypeAdapter(ReadingWord)
_WORD_TABLES: Final = ("light_words", "reading_words")


def _sensors_in(conn: Any) -> dict[SensorId, Sensor]:
    rows = conn.execute("SELECT id, sensor FROM sensors ORDER BY id").fetchall()
    return {row["id"]: Sensor.model_validate(row["sensor"]) for row in rows}


def _words_in(conn: Any) -> Words:
    lights = conn.execute("SELECT source, address, word FROM light_words").fetchall()
    readings = conn.execute("SELECT source, address, word FROM reading_words").fetchall()
    return Words(
        lights={ReporterRef(source=row["source"], address=row["address"]): _LIGHT_WORD.validate_python(row["word"])
                for row in lights},
        readings={SensorRef(source=row["source"], address=row["address"]): _READING_WORD.validate_python(row["word"])
                  for row in readings})


def _heard_in(conn: Any) -> dict[tuple[str, str, str], float]:
    """(table, source, address) -> when Central heard that reference (epoch seconds)."""
    heard: dict[tuple[str, str, str], float] = {}
    for table in _WORD_TABLES:
        rows = conn.execute(f"SELECT source, address, EXTRACT(EPOCH FROM heard_at) AS heard_at "  # noqa: S608
                            f"FROM {table}").fetchall()
        heard.update({(table, row["source"], row["address"]): float(row["heard_at"]) for row in rows})
    return heard


def document_in(conn: Any) -> RoomLightsDocument:
    """The room-lights document now (policy.room_lights_document over the stored rows)."""
    change = int(conn.execute("SELECT change FROM room_lights_change WHERE one").fetchone()["change"])
    return room_lights_document(lights_in(conn), _sensors_in(conn), _words_in(conn), change)


def _changed_in(conn: Any, before: RoomLightsDocument) -> bool:
    """When the document's lights differ from `before`'s: raise its change and wake the WALL writer
    (at commit). True when it did."""
    if dict(document_in(conn).lights) == dict(before.lights):
        return False
    conn.execute("UPDATE room_lights_change SET change = change + 1 WHERE one")
    conn.execute("SELECT pg_notify(%s, '')", (ROOM_LIGHTS_CHANNEL,))
    return True


def _word_in(conn: Any, table: str, source: str, address: str, word: dict[str, Any]) -> Recorded:
    """Keep `word` as (source, address)'s last in `table`, heard now, and the newest
    WORDS_KEPT_PER_SOURCE of that source; "recorded" when the document changed."""
    take_house_lock(conn)
    before = document_in(conn)
    conn.execute(f"INSERT INTO {table}(source, address, word, heard_at) VALUES (%s, %s, %s, clock_timestamp()) "  # noqa: S608
                 "ON CONFLICT (source, address) DO UPDATE SET word=EXCLUDED.word, heard_at=EXCLUDED.heard_at",
                 (source, address, Jsonb(word)))
    conn.execute(f"DELETE FROM {table} WHERE source=%(source)s AND address NOT IN "  # noqa: S608
                 f"(SELECT address FROM {table} WHERE source=%(source)s ORDER BY heard_at DESC, address "
                 "LIMIT %(keep)s)", {"source": source, "keep": WORDS_KEPT_PER_SOURCE})
    return "recorded" if _changed_in(conn, before) else "unchanged"


class PgRoomLights:
    """Room lights' ports over one Database."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- RoomLightReports ----

    def record_light_state(self, reporter: ReporterRef, state: Dimmer | ColourDimmer | Unavailable) -> Recorded:
        with self._db.transaction() as conn:
            return _word_in(conn, "light_words", reporter.source, reporter.address, state.model_dump(mode="json"))

    def record_reading(self, sensor: SensorRef, reading: Measured | Unavailable) -> Recorded:
        with self._db.transaction() as conn:
            return _word_in(conn, "reading_words", sensor.source, sensor.address, reading.model_dump(mode="json"))

    def record_reading_within(self, tx: Transaction, sensor: SensorRef, reading: Measured | Unavailable) -> Recorded:
        conn = pg_connection(tx)
        with conn.transaction():
            return _word_in(conn, "reading_words", sensor.source, sensor.address, reading.model_dump(mode="json"))

    def record_refused(self, source: Source, address: str, reason: ReportRefusal) -> None:
        """The address is cut to ADDRESS_MAX here and only here: a refused report's text is the
        source's own and may be anything."""
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO report_refusals(source, address, reason, at) "
                         "VALUES (%s, %s, %s, clock_timestamp())", (source, address[:ADDRESS_MAX], reason.value))
            conn.execute("DELETE FROM report_refusals WHERE source=%(source)s AND id NOT IN (SELECT id FROM "
                         "report_refusals WHERE source=%(source)s ORDER BY id DESC LIMIT %(keep)s)",
                         {"source": source, "keep": REFUSALS_KEPT})

    # ---- RoomLightsDocuments ----

    def current(self) -> RoomLightsDocument:
        with self._db.transaction() as conn:
            return document_in(conn)

    # ---- DrawnReports ----

    def record_drawn(self, player_id: str, report: RoomLightsDrawn) -> None:
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO room_lights_drawn(player_id, drawn, following, reported_at) "
                         "VALUES (%s, %s, %s, clock_timestamp()) ON CONFLICT (player_id) DO UPDATE "
                         "SET drawn=EXCLUDED.drawn, following=EXCLUDED.following, reported_at=EXCLUDED.reported_at",
                         (player_id, report.drawn, report.following))

    # ---- RoomLightSetup ----

    def lights(self) -> RoomLightsView:
        with self._db.transaction() as conn:
            return _view_in(conn)

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
            take_house_lock(conn)
            lights = lights_in(conn)
            refused = check_room_light(light_id, light, lights, _sensors_in(conn),
                                       naming_in(conn).lights.get(light_id, ()))
            if refused is not None:
                raise refused
            before = document_in(conn)
            conn.execute("INSERT INTO room_lights(id, light, position) "
                         "SELECT %s, %s, COALESCE(max(position), -1) + 1 FROM room_lights "
                         "ON CONFLICT (id) DO UPDATE SET light=EXCLUDED.light",
                         (light_id, Jsonb(ROOM_LIGHT.dump_python(light, mode="json"))))
            _changed_in(conn, before)
            return next(view for view in _view_in(conn).lights if view.id == light_id)

    def remove_room_light(self, light_id: RoomLightId) -> None:
        with self._db.transaction() as conn:
            take_house_lock(conn)
            refused = check_room_light_removal(light_id, lights_in(conn), naming_in(conn).lights.get(light_id, ()))
            if refused is not None:
                raise refused
            before = document_in(conn)
            conn.execute("DELETE FROM room_lights WHERE id=%s", (light_id,))
            _changed_in(conn, before)

    def save_sensor(self, sensor_id: SensorId, sensor: Sensor) -> SensorView:
        with self._db.transaction() as conn:
            take_house_lock(conn)
            refused = check_sensor(sensor_id, sensor, _sensors_in(conn), lights_in(conn))
            if refused is not None:
                raise refused
            before = document_in(conn)
            conn.execute("INSERT INTO sensors(id, sensor) VALUES (%s, %s) "
                         "ON CONFLICT (id) DO UPDATE SET sensor=EXCLUDED.sensor",
                         (sensor_id, Jsonb(sensor.model_dump(mode="json"))))
            _changed_in(conn, before)
            return next(view for view in _view_in(conn).sensors if view.id == sensor_id)

    def remove_sensor(self, sensor_id: SensorId) -> None:
        with self._db.transaction() as conn:
            take_house_lock(conn)
            refused = check_sensor_removal(sensor_id, _sensors_in(conn), lights_in(conn))
            if refused is not None:
                raise refused
            conn.execute("DELETE FROM sensors WHERE id=%s", (sensor_id,))


def _view_in(conn: Any) -> RoomLightsView:
    lights, sensors, words = lights_in(conn), _sensors_in(conn), _words_in(conn)
    heard = _heard_in(conn)
    named = naming_in(conn).lights

    def light_heard(light: RoomLight) -> float | None:
        if isinstance(light, FixedLight):
            return None
        if isinstance(light, MeasuredLight):
            sensor = sensors.get(light.sensor)
            return None if sensor is None else heard.get(("reading_words", sensor.at.source, sensor.at.address))
        return heard.get(("light_words", light.reporter.source, light.reporter.address))

    return RoomLightsView(
        lights=tuple(RoomLightView(id=light_id, light=light, entry=entry_of(light, words, sensors),
                                   heard_at=light_heard(light), named_by=named.get(light_id, ()))
                     for light_id, light in lights.items()),
        sensors=tuple(SensorView(id=sensor_id, sensor=sensor, reading=words.readings.get(sensor.at),
                                 heard_at=heard.get(("reading_words", sensor.at.source, sensor.at.address)),
                                 reports=measured_by(lights, sensor_id))
                      for sensor_id, sensor in sensors.items()))


def refused_in(conn: Any, source: Source | None) -> tuple[RefusedReportView, ...]:
    """The newest refused reports (of `source` only, when given), newest first."""
    columns = "SELECT source, address, reason, EXTRACT(EPOCH FROM at) AS at FROM report_refusals"
    rows = (conn.execute(f"{columns} ORDER BY id DESC") if source is None  # noqa: S608
            else conn.execute(f"{columns} WHERE source=%s ORDER BY id DESC", (source,))).fetchall()  # noqa: S608
    return tuple(RefusedReportView(source=row["source"], address=row["address"], reason=ReportRefusal(row["reason"]),
                                   at=float(row["at"])) for row in rows)


def references_in(conn: Any, source: Source) -> dict[str, tuple[RoomLightId | None, SensorId | None, float | None]]:
    """Every address of `source` heard or named by a room light or Sensor: (the room light it
    reports, the Sensor it is, when Central last heard it)."""
    heard: dict[str, float] = {}
    for (_, word_source, address), at in _heard_in(conn).items():
        if word_source == source:
            heard[address] = max(at, heard.get(address, at))
    reports = {light.reporter.address: light_id for light_id, light in lights_in(conn).items()
               if isinstance(light, ReportedLight) and light.reporter.source == source}
    sensors = {sensor.at.address: sensor_id for sensor_id, sensor in _sensors_in(conn).items()
               if sensor.at.source == source}
    return {address: (reports.get(address), sensors.get(address), heard.get(address))
            for address in sorted(heard.keys() | reports.keys() | sensors.keys())}


__all__ = ["REFUSALS_KEPT", "ROOM_LIGHTS_CHANNEL", "WORDS_KEPT_PER_SOURCE", "PgRoomLights", "document_in",
           "references_in", "refused_in"]
