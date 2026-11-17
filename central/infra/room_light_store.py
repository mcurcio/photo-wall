"""Room lights over PostgreSQL (decision 0034, migration 120): `PgRoomLights` implements
central/room_light/ports.py's RoomLightReports (every reporting source's adapter), RoomLightSetup
(House › Lights) and RoomLightsDocuments (the worker's WALL writer), and the Home Assistant
adapter's HeardEntities and GalleriaBirths (central/infra/home_assistant/states.py: each entity Home
Assistant published, its kind, name and when Central last heard it, home_assistant_entities; and
Galleria's birth, the `singletons` document 'galleria_birth').

The setup is one document, `singletons` 'room_light_setup' (central/infra/room_light_rows.py: room
lights, Sensors, colour sensors and the room-lights document's change number); its row is the
house's lock. Facts are kept for named references only, one row per (kind, source, address) of
`room_light_facts` (kinds light, reading, colour; the kind's model as JSON beside when its source
was last heard); a report for a reference no setup item names is judged and dropped.

A report's write path, the class-B rule (`_report_in`, its one home):
  1. no lock: does the setup name (source, address) for this kind? No: nothing is written;
  2. no lock: the facts after it (policy.*_facts_after over the stored facts);
  3. unchanged (but for when it was heard): nothing is written, except `heard_at` once it is older
     than HEARD_WRITE_SECONDS;
  4. changed: when the room-lights document changes with it, the house's lock is taken
     (HouseLock.changing_within), the report judged again under it, the facts written, the setup's
     `change` raised and `NOTIFY ROOM_LIGHTS_CHANNEL` sent (delivered at commit: the worker's WALL
     writer listens and puts the document again); else the facts row alone is written, no lock.

Every instant is PostgreSQL's `clock_timestamp()` (Central's one clock); a write stamping several
columns reads it once (`_CLOCK`) and binds that one instant to each. No trigger, no stored
procedure, no range in SQL: the rules are central/room_light/policy.py's and the ranges pydantic's.

A doubt with no report behind it is written here too, under the house's lock and wake: an
unreadable refusal doubts every reference at its (source, address) a setup item names "can't
read", and a closed link doubts every named reference of its source with facts "link closed". The
words that travel for a doubt are dated on the house's clock (its zone and 12- or 24-hour clock,
central/infra/house_store.py), so a Settings › House save that changes them raises the change too
(`house_clock_changing_within`, which PgHouse calls). Refused reports are `events` (kind 'refusal',
central/infra/events.py, kept a week).

What each Player drew with is judged on read from its newest `room-lights-drawn` value on the
Player line (`node_states`), never copied.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, TypeAdapter

from central.actor import Actor
from central.db import Database, DatabaseTransactionClock
from central.frame_lighting.ports import HouseLock
from central.house_time.model import HouseClock
from central.infra.events import refusal_in, refusals_in
from central.infra.house_store import house_settings_in
from central.infra.light_sensor_judge import PI_SOURCE, missed_readings_in
from central.infra.node_link_latest import latest_values_in
from central.infra.room_light_rows import (
    ROOM_LIGHT_SETUP,
    _PlacedLight,
    _RoomLightSetup,
    lights_of,
    setup_in,
)
from central.infra.singletons import Singleton
from central.infra.transactions import PgTransaction, pg_connection
from central.kernel.transactions import Transaction
from central.room_light.model import (
    ADDRESS_MAX,
    ColourFacts,
    ColourReading,
    ColourSensor,
    ColourSensorId,
    ColourSensorRef,
    DoubtReason,
    Facts,
    FixedLight,
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
from contracts.node_state import PLAYER_LINE, ROOM_LIGHTS_DRAWN_KEY
from contracts.room_light import (
    ROOM_LIGHTS_DRAWN_BYTES,
    Reading,
    RoomLightsDocument,
    RoomLightsDrawn,
    room_lights_digest,
)
from contracts.wire_model import decoded

ROOM_LIGHTS_CHANNEL: Final = "room_lights"     # NOTIFY payload: empty; the WALL writer re-reads the document
REFUSALS_SHOWN: Final = 20                     # the newest refused reports shown (kept a week as `events`)
HEARD_WRITE_SECONDS: Final = 60.0              # an unchanged report or entity rewrites when it was heard at most this often
# Who writes, for the audit fields: a facts row, its reference's own source for what it stated and
# Central for a doubt it set itself (a refused report, a closed link); the setup, the request's
# principal for a House › Lights edit and Central for a change a report raised; Galleria's birth, Central.
CENTRAL: Final[Actor] = "central"

FactsKind = Literal["light", "reading", "colour"]
_Reference = ReporterRef | SensorRef | ColourSensorRef
_Facts = LightFacts | ReadingFacts | ColourFacts
_KIND_OF: Final[dict[type, FactsKind]] = {ReporterRef: "light", SensorRef: "reading", ColourSensorRef: "colour"}
_REFERENCE_OF: Final[dict[FactsKind, type]] = {kind: reference for reference, kind in _KIND_OF.items()}
_FACTS_OF: Final[dict[FactsKind, TypeAdapter[Any]]] = {
    "light": TypeAdapter(LightFacts), "reading": TypeAdapter(ReadingFacts), "colour": TypeAdapter(ColourFacts)}
_EMPTY_OF: Final[dict[FactsKind, Callable[[float], _Facts]]] = {
    "light": lambda at: LightFacts(state=None, colour=None, doubt=None, heard_at=at),
    "reading": lambda at: ReadingFacts(reading=None, doubt=None, heard_at=at),
    "colour": lambda at: ColourFacts(colour=None, doubt=None, heard_at=at)}


class _GalleriaBirthDocument(BaseModel):
    """Galleria's last birth and when Central first heard it (Central's clock)."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    birth: GalleriaBirth
    heard_at: float


GALLERIA_BIRTH: Final[Singleton[_GalleriaBirthDocument]] = Singleton("galleria_birth", _GalleriaBirthDocument)

_CLOCK: Final = DatabaseTransactionClock()
"""Central's clock, read once per write (epoch seconds): the instant every column the write stamps takes."""


# ---- The setup ----

def sensors_in(conn: Any) -> dict[SensorId, Sensor]:
    return dict(sorted(setup_in(conn).sensors.items()))


def colour_sensors_in(conn: Any) -> dict[ColourSensorId, ColourSensor]:
    return dict(sorted(setup_in(conn).colour_sensors.items()))


def _named(setup: _RoomLightSetup) -> dict[_Reference, str]:
    """Every reference a room light reports by (ReporterRef), a Sensor is (SensorRef) or a colour
    sensor is (ColourSensorRef), with that item's id, room lights first in House › Lights order, then
    Sensors by id, then colour sensors by id."""
    named: dict[_Reference, str] = {}
    for light_id, light in lights_of(setup).items():
        if isinstance(light, ReportedLight):
            named[light.reporter] = light_id
    for sensor_id, sensor in sorted(setup.sensors.items()):
        named[sensor.at] = sensor_id
    for sensor_id, colour_sensor in sorted(setup.colour_sensors.items()):
        named[colour_sensor.at] = sensor_id
    return named


def named_in(conn: Any, source: Source) -> dict[ReporterRef | SensorRef | ColourSensorRef, str]:
    """Every reference of `source` a setup item names (`_named`), with that item's id."""
    return {reference: item for reference, item in _named(setup_in(conn)).items() if reference.source == source}


# ---- Facts ----

def facts_in(conn: Any) -> Facts:
    """Every named reference's kept facts."""
    kept: dict[FactsKind, dict[Any, Any]] = {"light": {}, "reading": {}, "colour": {}}
    for row in conn.execute("SELECT kind, source, address, facts, heard_at FROM room_light_facts").fetchall():
        kind: FactsKind = row["kind"]
        reference = _REFERENCE_OF[kind](source=row["source"], address=row["address"])
        kept[kind][reference] = _FACTS_OF[kind].validate_python({**row["facts"], "heard_at": float(row["heard_at"])})
    return Facts(lights=kept["light"], readings=kept["reading"], colours=kept["colour"])


def _kept_of(facts: Facts, kind: FactsKind) -> Mapping[Any, Any]:
    return {"light": facts.lights, "reading": facts.readings, "colour": facts.colours}[kind]


def _with(facts: Facts, reference: _Reference, kept: _Facts) -> Facts:
    """`facts` with `reference`'s replaced by `kept`."""
    kind = _KIND_OF[type(reference)]
    replaced = {**_kept_of(facts, kind), reference: kept}
    return Facts(lights=replaced if kind == "light" else facts.lights,
                 readings=replaced if kind == "reading" else facts.readings,
                 colours=replaced if kind == "colour" else facts.colours)


def _put_facts_in(conn: Any, reference: _Reference, kept: _Facts, now: float, by: str) -> None:
    """Upsert `reference`'s facts and its audit fields: created at `now` by `by` when new, changed at
    `now` by `by` always."""
    kind = _KIND_OF[type(reference)]
    conn.execute(
        "INSERT INTO room_light_facts (kind, source, address, facts, heard_at, created_at, created_by, changed_at,"
        " changed_by) VALUES (%(kind)s, %(source)s, %(address)s, %(facts)s, %(heard)s, %(now)s, %(by)s, %(now)s,"
        " %(by)s) ON CONFLICT (kind, source, address) DO UPDATE SET facts=EXCLUDED.facts,"
        " heard_at=EXCLUDED.heard_at, changed_at=EXCLUDED.changed_at, changed_by=EXCLUDED.changed_by",
        {"kind": kind, "source": reference.source, "address": reference.address,
         "facts": Jsonb(_FACTS_OF[kind].dump_python(kept, mode="json", exclude={"heard_at"})),
         "heard": kept.heard_at, "now": now, "by": by})


def _forget_unnamed_in(conn: Any, setup: _RoomLightSetup) -> None:
    """Delete every facts row no setup item names any more (named references only are kept)."""
    named = list(_named(setup))
    conn.execute(
        "DELETE FROM room_light_facts f WHERE NOT EXISTS (SELECT 1 FROM unnest(%s::text[], %s::text[], %s::text[])"
        " AS n(kind, source, address) WHERE (n.kind, n.source, n.address) = (f.kind, f.source, f.address))",
        ([_KIND_OF[type(each)] for each in named], [each.source for each in named], [each.address for each in named]))


# ---- The document ----

def _clock_in(conn: Any) -> HouseClock:
    """The house's clock (its zone and 12- or 24-hour clock), the one a doubt's words are dated on."""
    settings = house_settings_in(conn)
    return HouseClock(ZoneInfo(settings.time.zone), settings.clock)


def _document(setup: _RoomLightSetup, facts: Facts, clock: HouseClock) -> RoomLightsDocument:
    return room_lights_document(lights_of(setup), setup.sensors, setup.colour_sensors, facts, setup.change, clock)


def document_in(conn: Any) -> RoomLightsDocument:
    """The room-lights document now (policy.room_lights_document over the stored setup and facts)."""
    return _document(setup_in(conn), facts_in(conn), _clock_in(conn))


def _changed_in(conn: Any, before: RoomLightsDocument, by: Actor) -> bool:
    """When the document's lights differ from `before`'s: raise its change and wake the WALL writer
    (at commit). True when it did."""
    if dict(document_in(conn).lights) == dict(before.lights):
        return False
    setup = setup_in(conn)
    ROOM_LIGHT_SETUP.write_in(conn, setup.model_copy(update={"change": setup.change + 1}), by=by, times=_CLOCK)
    conn.execute("SELECT pg_notify(%s, '')", (ROOM_LIGHTS_CHANNEL,))
    return True


# ---- A report's write path (the class-B rule's one home) ----

def _report_in[F: (LightFacts, ReadingFacts, ColourFacts)](
        conn: Any, house: HouseLock, reference: _Reference, after: Callable[[F | None, float], F]) -> Recorded:
    """`reference`'s report, whose facts after it `after(stored, now)` gives: judged before stored
    (the module's steps 1 to 4)."""
    kind = _KIND_OF[type(reference)]
    setup = setup_in(conn)
    if reference not in _named(setup):                                     # 1
        return "unchanged"
    now = _CLOCK.now_in(conn)
    facts = facts_in(conn)
    stored: F | None = _kept_of(facts, kind).get(reference)
    judged = after(stored, now)                                            # 2
    if stored is not None and replace(judged, heard_at=stored.heard_at) == stored:   # 3
        if now - stored.heard_at >= HEARD_WRITE_SECONDS:
            conn.execute("UPDATE room_light_facts SET heard_at=%s WHERE kind=%s AND source=%s AND address=%s",
                         (judged.heard_at, kind, reference.source, reference.address))
        return "unchanged"
    clock = _clock_in(conn)
    if dict(_document(setup, _with(facts, reference, judged), clock).lights) == dict(
            _document(setup, facts, clock).lights):                        # 4, the facts alone
        _put_facts_in(conn, reference, judged, now, reference.source)
        return "unchanged"
    house.changing_within(PgTransaction(conn))                             # 4, the document changes
    setup = setup_in(conn)
    if reference not in _named(setup):
        return "unchanged"
    before = document_in(conn)
    judged = after(_kept_of(facts_in(conn), kind).get(reference), now)
    _put_facts_in(conn, reference, judged, now, reference.source)
    return "recorded" if _changed_in(conn, before, CENTRAL) else "unchanged"


def _light_in(conn: Any, house: HouseLock, reporter: ReporterRef, report: LightReport) -> Recorded:
    return _report_in(conn, house, reporter, lambda stored, now: light_facts_after(stored, report, now))


def _reading_in(conn: Any, house: HouseLock, sensor: SensorRef, reading: Reading) -> Recorded:
    return _report_in(conn, house, sensor, lambda stored, now: reading_facts_after(stored, reading, now))


def _colour_in(conn: Any, house: HouseLock, sensor: ColourSensorRef, reading: ColourReading) -> Recorded:
    return _report_in(conn, house, sensor, lambda stored, now: colour_facts_after(stored, reading, now))


def _doubts_in(conn: Any, house: HouseLock, source: Source, address: str | None, reason: DoubtReason) -> Recorded:
    """Under the house's lock: every named reference of `source` (at `address` only, when given) that
    has facts takes `reason`'s doubt (policy.doubted), its last word standing; for "can't read" (the
    source spoke), so does every such reference that has none yet. Only facts that change are
    written. "recorded" when the document changed."""
    house.changing_within(PgTransaction(conn))
    before = document_in(conn)
    now = _CLOCK.now_in(conn)
    facts = facts_in(conn)
    for reference in sorted(named_in(conn, source), key=lambda each: (_KIND_OF[type(each)], each.address)):
        if address is not None and reference.address != address:
            continue
        kind = _KIND_OF[type(reference)]
        stored = _kept_of(facts, kind).get(reference)
        if stored is None and reason != DoubtReason.CANT_READ:
            continue
        judged = doubted(stored, reason, now, _EMPTY_OF[kind](now))
        if judged != stored:
            _put_facts_in(conn, reference, judged, now, CENTRAL)
    return "recorded" if _changed_in(conn, before, CENTRAL) else "unchanged"


def _refused_in(conn: Any, house: HouseLock, source: Source, address: str, reason: ReportRefusal) -> None:
    """The refusal in the one log (`events`, its address cut to ADDRESS_MAX here and only here: a
    refused report's text is the source's own and may be anything); an unreadable one also doubts its
    references "can't read"."""
    _refusal_in(conn, source, address[:ADDRESS_MAX], reason)
    if reason == ReportRefusal.UNREADABLE:
        _doubts_in(conn, house, source, address[:ADDRESS_MAX], DoubtReason.CANT_READ)


def _refusal_in(conn: Any, source: Source, address: str, reason: ReportRefusal) -> None:
    refusal_in(conn, source=source, address=address, reason=reason.value)


def _take_missed_in(conn: Any, house: HouseLock) -> None:
    """Every Pi reading the host line recorded and its facts did not take (its savepoint failed, or no
    Sensor named it then), so the host line's record, not the facts, decides a Pi's reading."""
    for player_id, reading in missed_readings_in(conn):
        _reading_in(conn, house, SensorRef(source=PI_SOURCE, address=player_id), reading)


class PgRoomLights:
    """Room lights' ports over one Database."""

    def __init__(self, db: Database, house: HouseLock) -> None:
        """`house` is the house's lock and its "which Frames name it" query."""
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
        return "recorded" if _changed_in(conn, before, CENTRAL) else "unchanged"

    # ---- RoomLightsDocuments ----

    def current(self) -> RoomLightsDocument:
        """First takes every Pi reading the host line recorded and its facts did not take, so the
        host line's record, not the facts, decides a Pi's reading."""
        with self._db.transaction() as conn:
            _take_missed_in(conn, self._house)
            return document_in(conn)

    # ---- HeardEntities (the Home Assistant adapter) ----

    def record_heard(self, entity: str, kind: str, name: str) -> None:
        """Written only on change: a new entity, its kind or name changed, or when it was heard older
        than HEARD_WRITE_SECONDS. The newest HOME_ASSISTANT_ENTITIES_MAX are kept."""
        with self._db.transaction() as conn:
            row = conn.execute("SELECT kind, name, EXTRACT(EPOCH FROM clock_timestamp() - heard_at) AS age "
                               "FROM home_assistant_entities WHERE entity=%s", (entity,)).fetchone()
            if row is not None and (row["kind"], row["name"]) == (kind, name) and float(row["age"]) < HEARD_WRITE_SECONDS:
                return
            conn.execute("INSERT INTO home_assistant_entities(entity, kind, name, heard_at) "
                         "VALUES (%s, %s, %s, clock_timestamp()) ON CONFLICT (entity) DO UPDATE "
                         "SET kind=EXCLUDED.kind, name=EXCLUDED.name, heard_at=EXCLUDED.heard_at",
                         (entity, kind, name))
            if row is None:
                conn.execute("DELETE FROM home_assistant_entities WHERE entity NOT IN (SELECT entity FROM "
                             "home_assistant_entities ORDER BY heard_at DESC, entity LIMIT %s)",
                             (HOME_ASSISTANT_ENTITIES_MAX,))

    # ---- GalleriaBirths (the Home Assistant adapter) ----

    def record_galleria_birth(self, birth: GalleriaBirth) -> None:
        """Written when it changes: the same birth again keeps when it was first heard."""
        with self._db.transaction() as conn:
            stored = GALLERIA_BIRTH.read_in(conn)
            if stored is not None and stored.value.birth == birth:
                return
            GALLERIA_BIRTH.write_in(conn, _GalleriaBirthDocument(birth=birth, heard_at=_CLOCK.now_in(conn)),
                                    by=CENTRAL, times=_CLOCK)

    def galleria_birth(self) -> GalleriaBirth | None:
        with self._db.transaction() as conn:
            kept = galleria_birth_in(conn)
        return None if kept is None else kept[0]

    # ---- RoomLightSetup ----

    def lights(self) -> RoomLightsView:
        with self._db.transaction() as conn:
            return _view_in(conn, self._house)

    def drawn(self) -> DrawnView:
        """Each enrolled Player's newest `room-lights-drawn` value on its line, judged now against the
        document (`drawn_state`); its age is when Central recorded it against the database's clock in
        the same read. A removed or undecodable value lists no Player."""
        with self._db.transaction() as conn:
            digest_now = room_lights_digest(document_in(conn))
            now = _CLOCK.now_in(conn)
            latest = latest_values_in(conn, PLAYER_LINE, ROOM_LIGHTS_DRAWN_KEY)
        players = []
        for value in sorted(latest, key=lambda each: each.player_id):
            report = value.decoded(lambda data: decoded(RoomLightsDrawn, data, ROOM_LIGHTS_DRAWN_BYTES,
                                                        "room_lights_drawn"), "drawn report")
            if report is None:
                continue
            players.append(PlayerDrawnView(player_id=value.player_id, reported_at=value.recorded_at,
                                           state=drawn_state(report, now - value.recorded_at, digest_now)))
        return DrawnView(players=tuple(players))

    def refused(self) -> RefusedReportsView:
        with self._db.transaction() as conn:
            return RefusedReportsView(refused=refused_in(conn, None))

    def save_room_light(self, light_id: RoomLightId, light: RoomLight, *, by: Actor) -> RoomLightView:
        with self._db.transaction() as conn:
            tx = PgTransaction(conn)
            self._house.changing_within(tx)
            setup = setup_in(conn)
            refused = check_room_light(light_id, light, lights_of(setup), setup.sensors, setup.colour_sensors,
                                       self._house.naming_within(tx).lights.get(light_id, ()))
            if refused is not None:
                raise refused
            placed = setup.lights.get(light_id)
            position = placed.position if placed is not None else max(
                (each.position for each in setup.lights.values()), default=-1) + 1
            _setup_saved_in(conn, self._house, setup.model_copy(update={
                "lights": {**setup.lights, light_id: _PlacedLight(light=light, position=position)}}), by)
            return next(view for view in _view_in(conn, self._house).lights if view.id == light_id)

    def remove_room_light(self, light_id: RoomLightId, *, by: Actor) -> None:
        with self._db.transaction() as conn:
            tx = PgTransaction(conn)
            self._house.changing_within(tx)
            setup = setup_in(conn)
            refused = check_room_light_removal(light_id, lights_of(setup),
                                               self._house.naming_within(tx).lights.get(light_id, ()))
            if refused is not None:
                raise refused
            _setup_saved_in(conn, self._house, setup.model_copy(update={
                "lights": {key: each for key, each in setup.lights.items() if key != light_id}}), by)

    def save_sensor(self, sensor_id: SensorId, sensor: Sensor, *, by: Actor) -> SensorView:
        with self._db.transaction() as conn:
            self._house.changing_within(PgTransaction(conn))
            setup = setup_in(conn)
            refused = check_sensor(sensor_id, sensor, setup.sensors, lights_of(setup), setup.colour_sensors)
            if refused is not None:
                raise refused
            _setup_saved_in(conn, self._house, setup.model_copy(update={
                "sensors": {**setup.sensors, sensor_id: sensor}}), by)
            return next(view for view in _view_in(conn, self._house).sensors if view.id == sensor_id)

    def remove_sensor(self, sensor_id: SensorId, *, by: Actor) -> None:
        with self._db.transaction() as conn:
            self._house.changing_within(PgTransaction(conn))
            setup = setup_in(conn)
            refused = check_sensor_removal(sensor_id, setup.sensors, lights_of(setup))
            if refused is not None:
                raise refused
            _setup_saved_in(conn, self._house, setup.model_copy(update={
                "sensors": {key: each for key, each in setup.sensors.items() if key != sensor_id}}), by)

    def save_colour_sensor(self, sensor_id: ColourSensorId, sensor: ColourSensor, *, by: Actor) -> ColourSensorView:
        with self._db.transaction() as conn:
            self._house.changing_within(PgTransaction(conn))
            setup = setup_in(conn)
            refused = check_colour_sensor(sensor_id, sensor, setup.colour_sensors, lights_of(setup), setup.sensors)
            if refused is not None:
                raise refused
            _setup_saved_in(conn, self._house, setup.model_copy(update={
                "colour_sensors": {**setup.colour_sensors, sensor_id: sensor}}), by)
            return next(view for view in _view_in(conn, self._house).colour_sensors if view.id == sensor_id)

    def remove_colour_sensor(self, sensor_id: ColourSensorId, *, by: Actor) -> None:
        with self._db.transaction() as conn:
            self._house.changing_within(PgTransaction(conn))
            setup = setup_in(conn)
            refused = check_colour_sensor_removal(sensor_id, setup.colour_sensors, lights_of(setup))
            if refused is not None:
                raise refused
            _setup_saved_in(conn, self._house, setup.model_copy(update={
                "colour_sensors": {key: each for key, each in setup.colour_sensors.items() if key != sensor_id}}), by)


def _setup_saved_in(conn: Any, house: HouseLock, saved: _RoomLightSetup, by: Actor) -> None:
    """Under the house's lock, which the caller holds: store `saved` (its change raised when the
    document's lights move with it, and the WALL writer woken at commit), forget the facts no setup
    item names any more, and take a Pi reading its new Sensor names from the host line's record."""
    before = document_in(conn)
    facts = facts_in(conn)
    if dict(_document(saved, facts, _clock_in(conn)).lights) != dict(before.lights):
        saved = saved.model_copy(update={"change": saved.change + 1})
        conn.execute("SELECT pg_notify(%s, '')", (ROOM_LIGHTS_CHANNEL,))
    ROOM_LIGHT_SETUP.write_in(conn, saved, by=by, times=_CLOCK)
    _forget_unnamed_in(conn, saved)
    _take_missed_in(conn, house)


def _view_in(conn: Any, house: HouseLock) -> RoomLightsView:
    setup = setup_in(conn)
    lights, sensors, colour_sensors, facts = lights_of(setup), setup.sensors, setup.colour_sensors, facts_in(conn)
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
        sensors=tuple(sensor_view(sensor_id, sensor) for sensor_id, sensor in sorted(sensors.items())),
        colour_sensors=tuple(colour_sensor_view(sensor_id, sensor)
                             for sensor_id, sensor in sorted(colour_sensors.items())))


def refused_in(conn: Any, source: Source | None) -> tuple[RefusedReportView, ...]:
    """The newest REFUSALS_SHOWN refused reports (of `source` only, when given), newest first."""
    return tuple(RefusedReportView(source=each.source, address=each.address, reason=ReportRefusal(each.reason),
                                   at=each.recorded_at) for each in refusals_in(conn, source, REFUSALS_SHOWN))


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
    stored = GALLERIA_BIRTH.read_in(conn)
    return None if stored is None else (stored.value.birth, stored.value.heard_at)


def last_heard_in(conn: Any) -> float | None:
    """When Central last heard any of Home Assistant's states (None: never)."""
    row = conn.execute("SELECT EXTRACT(EPOCH FROM max(heard_at)) AS heard_at FROM home_assistant_entities").fetchone()
    return None if row["heard_at"] is None else float(row["heard_at"])


__all__ = ["GALLERIA_BIRTH", "HEARD_WRITE_SECONDS", "REFUSALS_SHOWN", "ROOM_LIGHTS_CHANNEL", "ROOM_LIGHT_SETUP",
           "FactsKind", "HeardEntity", "PgRoomLights", "colour_sensors_in", "document_in", "facts_in",
           "galleria_birth_in", "heard_entities_in", "last_heard_in", "named_in", "refused_in", "sensors_in"]
