"""Room lights over PostgreSQL (decision 0028; migration 099): `PgRoomLights` implements every port
of central/room_light/ports.py (the Home Assistant link's reports, a Pi's light meter, House >
Lights' setup and the room-lights document), and `LightSensorJudge` reads a Pi's meter off the host
line's state bucket as it is recorded.

Every write that changes the room-lights document raises `room_lights_change.change` in its own
transaction, under that row's lock (the one order every writer takes first), and sends
`NOTIFY ROOM_LIGHTS_CHANNEL`, delivered at commit: the worker's WALL writer
(central/node_bus_wiring.py `RoomLightsWall`) listens and puts the document again, so a House >
Lights edit in the API process reaches every Pi without waiting. Every instant is PostgreSQL's
`clock_timestamp()` (Central's one clock). No trigger, no stored procedure: the rules are
central/room_light/policy.py's.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final, Literal

from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from central.db import Database
from central.room_light.model import (
    Colour,
    FixedReporter,
    IlluminanceMessage,
    LightStateMessage,
    Recorded,
    Refusal,
    Refused,
    Reporter,
    RoomLight,
    Sensor,
    SensorPlace,
    SetupRefusal,
)
from central.room_light.policy import (
    check_room_light,
    check_room_light_removal,
    check_sensor,
    check_sensor_removal,
    illuminance_state,
    light_measured_by,
    light_reported_by_entity,
    light_state,
    room_lights_document,
    sensor_at_entity,
    sensor_on_player,
)
from central.room_light.views import (
    HomeAssistantLinkView,
    RefusedMessageView,
    RoomLightsView,
    RoomLightView,
    SensorView,
    StateView,
)
from contracts.frame_lighting import FrameLighting
from contracts.room_light import (
    FULL_ON,
    LIGHT_SENSOR_KEY,
    NotReported,
    RoomLightsDocument,
    RoomLightState,
    decode_light_sensor,
)
from nodeapi.pull import Read

log = logging.getLogger(__name__)

ROOM_LIGHTS_CHANNEL: Final = "room_lights"     # NOTIFY payload: empty; the WALL writer re-reads the document
REFUSALS_KEPT: Final = 20                      # the newest refused messages kept
HOST_STATE_STREAM: Final = "KV_state_host"     # the host line's state bucket (contracts/node_link.py STORE_LINES)
_HOST_STATE_SUBJECT: Final = "$KV.state_host."

_STATE: Final = TypeAdapter(StateView)
_REPORTER: Final = TypeAdapter(Reporter)
_COLOUR: Final = TypeAdapter(Colour)
_PLACE: Final = TypeAdapter(SensorPlace)
_KV_OPERATION: Final = "KV-Operation"


def _state_json(state: RoomLightState) -> dict[str, Any]:
    return _STATE.dump_python(state, mode="json")


def _room_light(row: Mapping[str, Any]) -> RoomLight:
    return RoomLight(id=row["id"], name=row["name"], reporter=_REPORTER.validate_python(row["reporter"]),
                     colour=_COLOUR.validate_python(row["colour"]))


def _sensor(row: Mapping[str, Any]) -> Sensor:
    return Sensor(id=row["id"], name=row["name"], at=_PLACE.validate_python(row["at"]))


def _initial_state(reporter: Any) -> RoomLightState:
    """A new (or re-reported) light's state: FULL_ON for a fixed light, else nothing heard yet."""
    return FULL_ON if isinstance(reporter, FixedReporter) else NotReported()


def room_lights_in(conn: Any) -> dict[str, RoomLight]:
    rows = conn.execute("SELECT id, name, reporter, colour FROM room_lights ORDER BY position, id").fetchall()
    return {row["id"]: _room_light(row) for row in rows}


def _sensors_in(conn: Any) -> dict[str, Sensor]:
    rows = conn.execute("SELECT id, name, at FROM sensors ORDER BY id").fetchall()
    return {row["id"]: _sensor(row) for row in rows}


def _locked_change_in(conn: Any) -> int:
    """The document's change, its row locked: every writer takes this lock first."""
    return int(conn.execute("SELECT change FROM room_lights_change WHERE one FOR UPDATE").fetchone()["change"])


def document_in(conn: Any) -> RoomLightsDocument:
    """The room-lights document now (policy.room_lights_document over the stored rows)."""
    change = int(conn.execute("SELECT change FROM room_lights_change WHERE one").fetchone()["change"])
    rows = conn.execute("SELECT id, name, reporter, colour, state FROM room_lights ORDER BY position, id").fetchall()
    return room_lights_document({row["id"]: _room_light(row) for row in rows},
                                {row["id"]: _STATE.validate_python(row["state"]) for row in rows}, change)


def _changed_in(conn: Any, before: RoomLightsDocument) -> bool:
    """When the document's lights differ from `before`'s: raise its change and wake the WALL writer
    (at commit). True when it did."""
    after = document_in(conn)
    if dict(after.lights) == dict(before.lights):
        return False
    conn.execute("UPDATE room_lights_change SET change = change + 1 WHERE one")
    conn.execute("SELECT pg_notify(%s, '')", (ROOM_LIGHTS_CHANNEL,))
    return True


def named_by_frames_in(conn: Any) -> dict[str, tuple[str, ...]]:
    """Room light id -> the Frames whose saved lighting or open Position trial names it, by Frame id."""
    named: dict[str, set[str]] = {}
    rows = conn.execute("SELECT id AS frame_id, lighting FROM frames UNION ALL "
                        "SELECT frame_id, lighting FROM node_calibration_trials WHERE state='active'").fetchall()
    for row in rows:
        for light in FrameLighting.model_validate(row["lighting"]).lights:
            named.setdefault(light.light, set()).add(row["frame_id"])
    return {light: tuple(sorted(frames)) for light, frames in named.items()}


class PgRoomLights:
    """Room lights' ports over one Database (RoomLightReports, SensorFacts, RoomLightSetup,
    RoomLightsDocuments)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- RoomLightReports (the worker's Home Assistant link) ----

    def record_light_state(self, entity: str, message: LightStateMessage) -> Recorded:
        with self._db.transaction() as conn:
            _locked_change_in(conn)
            light = light_reported_by_entity(room_lights_in(conn).values(), entity)
            if light is None:
                return Refused(Refusal.UNKNOWN_ENTITY)
            return self._recorded_in(conn, light.id, light_state(message))

    def record_illuminance(self, entity: str, message: IlluminanceMessage) -> Recorded:
        with self._db.transaction() as conn:
            _locked_change_in(conn)
            sensor = sensor_at_entity(_sensors_in(conn).values(), entity)
            if sensor is None:
                return Refused(Refusal.UNKNOWN_ENTITY)
            return self._reading_in(conn, sensor, message.lux)

    def record_refusal(self, topic: str, reason: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO home_assistant_refusals(topic, reason, at) VALUES (%s, %s, clock_timestamp())",
                         (topic[:512], reason[:128]))
            conn.execute("DELETE FROM home_assistant_refusals WHERE id NOT IN "
                         "(SELECT id FROM home_assistant_refusals ORDER BY id DESC LIMIT %s)", (REFUSALS_KEPT,))

    def record_link(self, state: Literal["not-configured", "connected", "away"]) -> None:
        """The link's state now, stamped with Central's time ("not-configured": the worker runs no
        link, so its `since` is None)."""
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO home_assistant_link(one, state, since) VALUES (true, %s, "
                         "CASE WHEN %s = 'not-configured' THEN NULL ELSE clock_timestamp() END) "
                         "ON CONFLICT (one) DO UPDATE SET state=EXCLUDED.state, since=EXCLUDED.since",
                         (state, state))

    def refusals(self) -> tuple[tuple[str, str], ...]:
        """The newest refused messages' (topic, reason), newest first."""
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT topic, reason FROM home_assistant_refusals ORDER BY id DESC").fetchall()
        return tuple((row["topic"], row["reason"]) for row in rows)

    # ---- SensorFacts (a Pi's wired meter, judged off the host line) ----

    def record_sensor_reading(self, player_id: str, lux: float | None) -> Recorded:
        with self._db.transaction() as conn:
            return self.record_sensor_reading_in(conn, player_id, lux)

    def record_sensor_reading_in(self, conn: Any, player_id: str, lux: float | None) -> Recorded:
        """`record_sensor_reading` in the caller's transaction (the host line's judge)."""
        _locked_change_in(conn)
        sensor = sensor_on_player(_sensors_in(conn).values(), player_id)
        if sensor is None:
            return Refused(Refusal.NOT_ITS_REPORTER)
        return self._reading_in(conn, sensor, lux)

    def _reading_in(self, conn: Any, sensor: Sensor, lux: float | None) -> Recorded:
        conn.execute("UPDATE sensors SET reading=%s, heard_at=clock_timestamp() WHERE id=%s",
                     (Jsonb({"lux": lux}), sensor.id))
        light = light_measured_by(room_lights_in(conn).values(), sensor.id)
        if light is None:
            return "unchanged"
        return self._recorded_in(conn, light.id, illuminance_state(IlluminanceMessage(lux)))

    def _recorded_in(self, conn: Any, light_id: str, state: RoomLightState) -> Recorded:
        """Record `state` as the light's, heard now: "recorded" when it changed the document."""
        before = document_in(conn)
        conn.execute("UPDATE room_lights SET state=%s, heard_at=clock_timestamp() WHERE id=%s",
                     (Jsonb(_state_json(state)), light_id))
        return "recorded" if _changed_in(conn, before) else "unchanged"

    # ---- RoomLightsDocuments (the worker's WALL writer) ----

    def current(self) -> RoomLightsDocument:
        with self._db.transaction() as conn:
            return document_in(conn)

    # ---- RoomLightSetup (House > Lights) ----

    def lights(self) -> RoomLightsView:
        with self._db.transaction() as conn:
            document = document_in(conn)
            rows = conn.execute("SELECT id, name, reporter, colour, EXTRACT(EPOCH FROM heard_at) AS heard_at "
                                "FROM room_lights ORDER BY position, id").fetchall()
            lights = {row["id"]: _room_light(row) for row in rows}
            sensor_rows = conn.execute("SELECT id, name, at, reading, EXTRACT(EPOCH FROM heard_at) AS heard_at "
                                       "FROM sensors ORDER BY id").fetchall()
            named = named_by_frames_in(conn)
            link = conn.execute("SELECT state, EXTRACT(EPOCH FROM since) AS since FROM home_assistant_link "
                                "WHERE one").fetchone()
            refused = conn.execute("SELECT topic, reason, EXTRACT(EPOCH FROM at) AS at FROM home_assistant_refusals "
                                   "ORDER BY id DESC").fetchall()
        return RoomLightsView(
            lights=tuple(RoomLightView(
                id=row["id"], name=row["name"], reporter=lights[row["id"]].reporter, colour=lights[row["id"]].colour,
                state=document.lights[row["id"]].state,
                heard_at=None if isinstance(lights[row["id"]].reporter, FixedReporter) or row["heard_at"] is None
                else float(row["heard_at"]),
                named_by=named.get(row["id"], ())) for row in rows),
            sensors=tuple(SensorView(
                id=row["id"], name=row["name"], at=_PLACE.validate_python(row["at"]),
                reading=None if row["reading"] is None else (
                    "unavailable" if row["reading"].get("lux") is None else float(row["reading"]["lux"])),
                heard_at=None if row["heard_at"] is None else float(row["heard_at"]),
                reports=None if (light := light_measured_by(lights.values(), row["id"])) is None else light.id)
                for row in sensor_rows),
            home_assistant=HomeAssistantLinkView(state="not-configured", since=None) if link is None
            else HomeAssistantLinkView(state=link["state"],
                                       since=None if link["since"] is None else float(link["since"])),
            refused=tuple(RefusedMessageView(topic=row["topic"], reason=row["reason"], at=float(row["at"]))
                          for row in refused))

    def save_room_light(self, light: RoomLight) -> SetupRefusal | None:
        with self._db.transaction() as conn:
            _locked_change_in(conn)
            lights = room_lights_in(conn)
            refusal = check_room_light(light, lights, _sensors_in(conn), named_by_frames_in(conn).get(light.id, ()))
            if refusal is not None:
                return refusal
            before = document_in(conn)
            reporter, colour = Jsonb(light.reporter.model_dump(mode="json")), Jsonb(_COLOUR.dump_python(
                light.colour, mode="json"))
            previous = lights.get(light.id)
            if previous is None:
                conn.execute("INSERT INTO room_lights(id, name, reporter, colour, state, heard_at, position) "
                             "SELECT %s, %s, %s, %s, %s, NULL, COALESCE(max(position), -1) + 1 FROM room_lights",
                             (light.id, light.name, reporter, colour, Jsonb(_state_json(_initial_state(light.reporter)))))
            elif previous.reporter != light.reporter:
                conn.execute("UPDATE room_lights SET name=%s, reporter=%s, colour=%s, state=%s, heard_at=NULL "
                             "WHERE id=%s", (light.name, reporter, colour,
                                             Jsonb(_state_json(_initial_state(light.reporter))), light.id))
            else:
                conn.execute("UPDATE room_lights SET name=%s, colour=%s WHERE id=%s",
                             (light.name, colour, light.id))
            _changed_in(conn, before)
        return None

    def remove_room_light(self, light_id: str) -> SetupRefusal | None:
        with self._db.transaction() as conn:
            _locked_change_in(conn)
            refusal = check_room_light_removal(light_id, room_lights_in(conn), named_by_frames_in(conn).get(light_id, ()))
            if refusal is not None:
                return refusal
            before = document_in(conn)
            conn.execute("DELETE FROM room_lights WHERE id=%s", (light_id,))
            _changed_in(conn, before)
        return None

    def save_sensor(self, sensor: Sensor) -> SetupRefusal | None:
        with self._db.transaction() as conn:
            _locked_change_in(conn)
            sensors, lights = _sensors_in(conn), room_lights_in(conn)
            refusal = check_sensor(sensor, sensors, lights)
            if refusal is not None:
                return refusal
            before = document_in(conn)
            at = Jsonb(sensor.at.model_dump(mode="json"))
            previous = sensors.get(sensor.id)
            if previous is None:
                conn.execute("INSERT INTO sensors(id, name, at, reading, heard_at) VALUES (%s, %s, %s, NULL, NULL)",
                             (sensor.id, sensor.name, at))
            elif previous.at != sensor.at:
                conn.execute("UPDATE sensors SET name=%s, at=%s, reading=NULL, heard_at=NULL WHERE id=%s",
                             (sensor.name, at, sensor.id))
                measured = light_measured_by(lights.values(), sensor.id)
                if measured is not None:   # a new place: nothing heard from it yet
                    conn.execute("UPDATE room_lights SET state=%s, heard_at=NULL WHERE id=%s",
                                 (Jsonb(_state_json(NotReported())), measured.id))
            else:
                conn.execute("UPDATE sensors SET name=%s WHERE id=%s", (sensor.name, sensor.id))
            _changed_in(conn, before)
        return None

    def remove_sensor(self, sensor_id: str) -> SetupRefusal | None:
        with self._db.transaction() as conn:
            _locked_change_in(conn)
            refusal = check_sensor_removal(sensor_id, _sensors_in(conn), room_lights_in(conn))
            if refusal is not None:
                return refusal
            conn.execute("DELETE FROM sensors WHERE id=%s", (sensor_id,))
        return None


class LightSensorJudge:
    """`RecordJudge` for the host line's state bucket (HOST_STATE_STREAM): each newly recorded
    LIGHT_SENSOR_KEY reading is recorded for the Sensor on that Node's Pi
    (`PgRoomLights.record_sensor_reading_in`), in the commit that records it; a record that is not a
    valid reading is logged and skipped; a Node with no player, or a Pi with no Sensor, is only
    recorded. A changed document wakes the WALL writer by its NOTIFY, at commit."""

    def __init__(self, lights: PgRoomLights) -> None:
        self._lights = lights

    def judge_in(self, conn: Any, device_id: str, stream: str, items: tuple[Read, ...]) -> None:
        if stream != HOST_STATE_STREAM:
            return
        readings = [item for item in items if item.subject == _HOST_STATE_SUBJECT + LIGHT_SENSOR_KEY
                    and not (item.headers or {}).get(_KV_OPERATION)]
        if not readings:
            return
        row = conn.execute("SELECT id FROM players WHERE device_id=%s AND retired_at IS NULL",
                           (device_id,)).fetchone()
        if row is None:
            return
        for item in readings:
            try:
                reading = decode_light_sensor(item.data)
            except ValueError as refused:
                log.warning("light sensor reading refused: device=%s seq=%s: %s", device_id, item.token.seq, refused)
                continue
            self._lights.record_sensor_reading_in(conn, row["id"], reading.lux)


__all__ = ["HOST_STATE_STREAM", "REFUSALS_KEPT", "ROOM_LIGHTS_CHANNEL", "LightSensorJudge", "PgRoomLights",
           "document_in", "named_by_frames_in", "room_lights_in"]
