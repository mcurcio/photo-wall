"""Displays in PostgreSQL (roadmap 1b; run ledger .claude/runs/display-1b.md, slice C1; migration 072).

Three adapters over the pure rules in central/displays/model.py, which stay the one home of "same
display" and of the Output document's body:

- `OutputReportJudge`: the `RecordJudge` of the display line's state bucket. Inside the link
  store's commit it decodes each newly recorded Output report and records it on its Output
  (`output_displays`), recognising the Display there in the same transaction (`record_report`).
- `DisplayDocuments`: the `DocumentSources` of every link. The fleet pipe's display desired bucket
  holds one Output document per Output the Node has (an `outputs` or `output_displays` row);
  `output_documents.change` rises only when the body's digest differs. Everything else is `{}`.
- `PgDisplayQueries`: the operator API's reads (`DisplayQueries`).

Readiness (slice C2; migration 073) is worked out by `model.readiness` from facts read here and
nowhere else: `POSITION_COLUMNS` over `SEEN_DISPLAY_JOIN` gives every reader (the Registry's
configuration and inventory, the installation repository, the Hardware tab) the same
`FramePosition`; `commit_position` is what both Position commits write; the judge adopts a first
Display (`model.adopts`) on the Frame bound where it is recorded.

`resolve_output` is what `Registry.bind` calls in its own transaction: a binding can turn the
identity recorded on the Output (pending, or tied to another Frame) into a Display.

Ordering. Both the judge and the document source first lock the Node's player row (the judge
`FOR NO KEY UPDATE`, the source `FOR SHARE`), so a source woken by the judge (`DisplayWakes`, from
the judge's worker thread) reads only after the judge's transaction committed: a wake is never
read ahead of its facts. `Registry.bind` holds the same row `FOR UPDATE`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import uuid
from collections.abc import Mapping
from typing import Any, Final
from uuid import UUID

from psycopg.types.json import Jsonb

from central.content_catalog.catalog import device_id_for_serial
from central.db import Database, DatabaseTransactionClock
from central.displays.model import (
    DisplayKey,
    DisplaySettings,
    FramePosition,
    Readiness,
    Sighting,
    adopts,
    display_key,
    project_output,
    readiness,
    serial_usable,
)
from central.displays.ports import DisplayRefused
from central.displays.views import (
    DisplayPowerSettings,
    DisplayView,
    FrameDisplayView,
    ModeView,
    PowerTestAccepted,
)
from central.infra.node_link_store import PgLinkStores
from contracts.node_link import Pipe
from contracts.node_output import (
    OUTPUT_IDS,
    DisplayIdentity,
    DisplayMode,
    OutputDocument,
    OutputReport,
    Power,
    PowerMethod,
    decode_output_report,
    encode_output_document,
    encode_output_report,
    output_document_key,
    output_report_key,
)
from nodeapi.hub import DocumentSource
from nodeapi.pull import Read

log = logging.getLogger(__name__)

REPORT_STREAM: Final = "KV_state_display"       # the display line's state bucket: Output reports
DOCUMENT_STREAM: Final = "KV_desired_display"   # the display line's desired bucket: Output documents
_REPORT_SUBJECT: Final = "$KV.state_display."
_KV_OPERATION: Final = "KV-Operation"            # a delete or purge marker carries no value
_CLOCK: Final = DatabaseTransactionClock()


# --- Readiness facts -----------------------------------------------------------------------------

# A frames row `f`, its binding `b` (LEFT JOIN or JOIN) and this join give POSITION_COLUMNS.
SEEN_DISPLAY_JOIN: Final = ("LEFT JOIN output_displays seen ON seen.player_id=b.player_id "
                            "AND seen.output_id=b.output_id")
POSITION_FACTS: Final = ("bound", "generation", "position_generation", "position_display_id",
                         "seen_display_id")
POSITION_COLUMNS: Final = ("b.player_id IS NOT NULL AS bound, f.generation, f.position_generation, "
                           "f.position_display_id, seen.display_id AS seen_display_id")


def frame_position(row: Mapping[str, Any]) -> FramePosition:
    """The readiness facts of a row selected with POSITION_COLUMNS."""
    return FramePosition(bool(row["bound"]), row["generation"], row["position_generation"],
                         row["position_display_id"], row["seen_display_id"])


def frame_readiness(row: Mapping[str, Any]) -> Readiness:
    """`model.readiness` of a row selected with POSITION_COLUMNS."""
    return readiness(frame_position(row))


def frame_readiness_in(conn: Any, frame_id: str) -> Readiness | None:
    """The Frame's readiness in the caller's transaction; None for an unknown Frame."""
    row = conn.execute(f"SELECT {POSITION_COLUMNS} FROM frames f LEFT JOIN bindings b ON b.frame_id=f.id "  # noqa: S608
                       f"{SEEN_DISPLAY_JOIN} WHERE f.id=%s", (frame_id,)).fetchone()
    return None if row is None else frame_readiness(row)


def commit_position(conn: Any, frame_id: str) -> None:
    """A Position commit, in the committing transaction: it is made at the Frame's current
    generation, against the Display last seen on its bound Output (NULL when none)."""
    conn.execute("UPDATE frames f SET position_generation=f.generation, position_display_id=("
                 "SELECT seen.display_id FROM bindings b " + SEEN_DISPLAY_JOIN + " WHERE b.frame_id=f.id) "
                 "WHERE f.id=%s", (frame_id,))


def _adopt(conn: Any, player_id: str, output_id: str, seen_before: UUID | None, display_id: UUID) -> None:
    """The Frame bound to this Output takes `display_id`, newly recorded there, as its Position's
    Display when `model.adopts` says so (a first sighting, and its Position commit names none)."""
    frame = conn.execute(f"SELECT f.id, {POSITION_COLUMNS} FROM bindings b JOIN frames f ON f.id=b.frame_id "  # noqa: S608
                         f"{SEEN_DISPLAY_JOIN} WHERE b.player_id=%s AND b.output_id=%s FOR NO KEY UPDATE OF f",
                         (player_id, output_id)).fetchone()
    if frame is not None and adopts(frame_position(frame), seen_before, display_id):
        conn.execute("UPDATE frames SET position_display_id=%s WHERE id=%s", (display_id, frame["id"]))


# --- Recognising a Display -----------------------------------------------------------------------

def _identity(value: Mapping[str, Any] | None) -> DisplayIdentity | None:
    return None if value is None else DisplayIdentity(value["maker"], value["product"], value["name"], value["serial"])


def _identity_json(identity: DisplayIdentity) -> Jsonb:
    return Jsonb({"maker": identity.maker, "product": identity.product, "name": identity.name,
                  "serial": identity.serial})


def _modes_json(modes: tuple[DisplayMode, ...]) -> Jsonb:
    return Jsonb([{"width": mode.width, "height": mode.height, "refresh_millihertz": mode.refresh_millihertz,
                   "preferred": mode.preferred} for mode in modes])


def _recognise(conn: Any, player_id: str, output_id: str, identity: DisplayIdentity,
               modes: tuple[DisplayMode, ...], *, before: DisplayIdentity | None, recorded: UUID | None,
               now: float) -> UUID | None:
    """The Display `identity` is on this Output, by the model's rules: a serial another connected
    Output reports at the same time is recorded shared (for good); pending (no key) keeps what was
    recorded for the same identity and is None for a new one."""
    sighting = Sighting(player_id, output_id, identity)
    others: tuple[Sighting, ...] = ()
    if identity.serial is not None:
        rows = conn.execute(
            "SELECT o.player_id, o.output_id, o.identity FROM output_displays o JOIN players p ON p.id=o.player_id "
            "WHERE p.retired_at IS NULL AND o.connected AND o.identity->>'maker'=%s "
            "AND (o.identity->>'product')::int=%s AND o.identity->>'serial'=%s",
            (identity.maker, identity.product, identity.serial)).fetchall()
        others = tuple(Sighting(row["player_id"], row["output_id"], _identity(row["identity"])) for row in rows)
        if not serial_usable(sighting, others, ()):   # reported elsewhere at once: never usable again
            conn.execute("INSERT INTO shared_serials(maker, product, serial, found_at) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT DO NOTHING", (identity.maker, identity.product, identity.serial, now))
    shared = {(row["maker"], row["product"], row["serial"]) for row in conn.execute(
        "SELECT maker, product, serial FROM shared_serials WHERE maker=%s AND product=%s",
        (identity.maker, identity.product)).fetchall()}
    bound = conn.execute("SELECT frame_id FROM bindings WHERE player_id=%s AND output_id=%s",
                         (player_id, output_id)).fetchone()
    key = display_key(identity, serial_counts=serial_usable(sighting, others, shared),
                      bound_frame_id=None if bound is None else bound["frame_id"])
    if key is None:
        return recorded if identity == before else None
    return _display_for(conn, key, identity, modes, now)


def _display_for(conn: Any, key: DisplayKey, identity: DisplayIdentity, modes: tuple[DisplayMode, ...],
                 now: float) -> UUID:
    """The Display keyed `key`, made on its first sighting; its modes follow the latest sighting."""
    where, value = ("serial", key.serial) if key.serial is not None else ("frame_id", key.frame_id)
    select = f"SELECT id FROM displays WHERE maker=%s AND product=%s AND {where}=%s"   # noqa: S608 (a column name)
    row = conn.execute(select, (key.maker, key.product, value)).fetchone()
    if row is None:
        conn.execute("INSERT INTO displays(id, maker, product, name, serial, frame_id, modes, first_seen_at) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                     (uuid.uuid4(), key.maker, key.product, identity.name, key.serial, key.frame_id,
                      _modes_json(modes), now))
        row = conn.execute(select, (key.maker, key.product, value)).fetchone()
    elif modes:
        conn.execute("UPDATE displays SET modes=%s WHERE id=%s", (_modes_json(modes), row["id"]))
    return row["id"]


def record_report(conn: Any, player_id: str, report: OutputReport, now: float) -> None:
    """Record `report` on its Output: the latest report and connection always; the identity and the
    Display only from a readable identity (None, an unplug or a blip, keeps both)."""
    row = conn.execute("SELECT identity, display_id FROM output_displays WHERE player_id=%s AND output_id=%s "
                       "FOR UPDATE", (player_id, report.output_id)).fetchone()
    before = None if row is None else _identity(row["identity"])
    seen_before = display_id = None if row is None else row["display_id"]
    if report.identity is not None:
        display_id = _recognise(conn, player_id, report.output_id, report.identity, report.modes,
                                before=before, recorded=display_id, now=now)
    if display_id is not None and display_id != seen_before:   # a Display newly recorded here
        _adopt(conn, player_id, report.output_id, seen_before, display_id)
    identity = report.identity or before
    conn.execute(
        "INSERT INTO output_displays(player_id, output_id, connected, identity, display_id, report, reported_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (player_id, output_id) DO UPDATE SET "
        "connected=EXCLUDED.connected, identity=EXCLUDED.identity, display_id=EXCLUDED.display_id, "
        "report=EXCLUDED.report, reported_at=EXCLUDED.reported_at",
        (player_id, report.output_id, report.connected, None if identity is None else _identity_json(identity),
         display_id, Jsonb(_report_json(report)), now))


def resolve_output(conn: Any, player_id: str, output_id: str) -> None:
    """After a bind of this Output, in the binding's transaction: the identity recorded on it, when a
    display is connected there, is recognised again under the new binding (a pending identity
    resolves; a display tied to another Frame starts over on this one). Times are PostgreSQL's, as
    the judge's."""
    row = conn.execute("SELECT identity, display_id, report FROM output_displays WHERE player_id=%s "
                       "AND output_id=%s AND connected FOR UPDATE", (player_id, output_id)).fetchone()
    identity = None if row is None else _identity(row["identity"])
    if identity is None:
        return
    modes = tuple(DisplayMode(**mode) for mode in row["report"].get("modes", ()))
    display_id = _recognise(conn, player_id, output_id, identity, modes, before=identity,
                            recorded=row["display_id"], now=_CLOCK.now_in(conn))
    conn.execute("UPDATE output_displays SET display_id=%s WHERE player_id=%s AND output_id=%s",
                 (display_id, player_id, output_id))


def _report_json(report: OutputReport) -> dict[str, Any]:
    return json.loads(encode_output_report(report))


def _player_of(conn: Any, device_id: str, lock: str) -> str | None:
    """The Node's non-retired player, its row locked (`lock`); None when it has none."""
    row = conn.execute(f"SELECT id FROM players WHERE device_id=%s AND retired_at IS NULL {lock}",  # noqa: S608
                       (device_id,)).fetchone()
    return None if row is None else row["id"]


# --- The Output report judge ---------------------------------------------------------------------

class OutputReportJudge:
    """`RecordJudge` for REPORT_STREAM: each newly recorded `output-…` report is recorded on its
    Output in the commit that records it; a record that is not a valid report is logged with its
    coordinates and skipped. A Node with no player yet is only recorded. `wakes` tells this
    process's document source that the Node's facts changed."""

    def __init__(self, wakes: DisplayWakes) -> None:
        self._wakes = wakes

    def judge_in(self, conn: Any, device_id: str, stream: str, items: tuple[Read, ...]) -> None:
        if stream != REPORT_STREAM or not items:
            return
        player_id = _player_of(conn, device_id, "FOR NO KEY UPDATE")
        if player_id is None:
            log.info("output reports of %s: no enrolled player", device_id)
            return
        now = _CLOCK.now_in(conn)
        judged = False
        for item in items:
            key = item.subject.removeprefix(_REPORT_SUBJECT)
            if not key.startswith("output-") or (item.headers or {}).get(_KV_OPERATION):
                continue
            try:
                report = decode_output_report(item.data)
                if output_report_key(report.output_id) != key:
                    raise ValueError("output_report_key")
            except ValueError as refused:
                log.warning("output report refused: device=%s stream=%s epoch=%s seq=%s key=%s: %s",
                            device_id, stream, item.token.epoch, item.token.seq, key, refused)
                continue
            record_report(conn, player_id, report, now)
            judged = True
        if judged:
            self._wakes.wake(device_id)


# --- The Output document source ------------------------------------------------------------------

class DisplayWakes:
    """Which Nodes' display facts changed in this process: `wake` from any thread (the judge runs in
    a store worker thread), `after` on the event loop. A count per Node, so a waiter never misses a
    wake that came between two waits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}

    def count(self, device_id: str) -> int:
        with self._lock:
            return self._counts.get(device_id, 0)

    def wake(self, device_id: str) -> None:
        with self._lock:
            self._counts[device_id] = self._counts.get(device_id, 0) + 1
            waiters = self._waiters.pop(device_id, [])
        for loop, event in waiters:
            with contextlib.suppress(RuntimeError):   # that loop is closed: nobody waits there any more
                loop.call_soon_threadsafe(event.set)

    async def after(self, device_id: str, seen: int) -> int:
        """The Node's count, once it differs from `seen`."""
        loop = asyncio.get_running_loop()
        while True:
            event = asyncio.Event()
            with self._lock:
                count = self._counts.get(device_id, 0)
                if count != seen:
                    return count
                self._waiters.setdefault(device_id, []).append((loop, event))
            try:
                await event.wait()
            finally:
                with self._lock:
                    waiting = self._waiters.get(device_id, [])
                    if (loop, event) in waiting:
                        waiting.remove((loop, event))


class DisplayDocuments:
    """`DocumentSources` for both pipes: the fleet pipe's display desired bucket holds each Output's
    document; every other stream, and the show pipe, is `{}`. Reads run behind the link stores'
    gate, in one transaction each."""

    def __init__(self, stores: PgLinkStores, wakes: DisplayWakes) -> None:
        self._stores = stores
        self._wakes = wakes

    def source(self, serial: str, pipe: Pipe) -> DocumentSource:
        device_id = device_id_for_serial(serial)
        if device_id is None:
            raise ValueError("node_link_serial")
        if Pipe(pipe) is not Pipe.FLEET:
            return _NoDocuments()
        return _OutputDocuments(self._stores, self._wakes, device_id)


class _NoDocuments:
    async def documents(self, stream: str) -> Mapping[str, bytes]:
        return {}

    async def changed(self) -> None:
        await asyncio.Event().wait()   # nothing here ever changes


class _OutputDocuments:
    def __init__(self, stores: PgLinkStores, wakes: DisplayWakes, device_id: str) -> None:
        self._stores = stores
        self._wakes = wakes
        self._device_id = device_id
        self._seen = wakes.count(device_id)

    async def documents(self, stream: str) -> Mapping[str, bytes]:
        if stream != DOCUMENT_STREAM:
            return {}
        return await self._stores.run(lambda conn: output_documents(conn, self._device_id))

    async def changed(self) -> None:
        self._seen = await self._wakes.after(self._device_id, self._seen)


def output_documents(conn: Any, device_id: str) -> dict[str, bytes]:
    """key -> encoded Output document for each Output of the Node, raising an Output's `change` only
    when its body differs from the one recorded."""
    player_id = _player_of(conn, device_id, "FOR SHARE")
    if player_id is None:
        return {}
    now = _CLOCK.now_in(conn)
    rows = conn.execute(
        "SELECT o.output_id, d.power_method, d.switch_input_on_power_on, d.never_off_on_other_input, "
        "w.change, w.digest FROM (SELECT output_id FROM outputs WHERE player_id=%(player)s "
        "UNION SELECT output_id FROM output_displays WHERE player_id=%(player)s) o "
        "LEFT JOIN output_displays s ON s.player_id=%(player)s AND s.output_id=o.output_id "
        "LEFT JOIN displays d ON d.id=s.display_id "
        "LEFT JOIN output_documents w ON w.player_id=%(player)s AND w.output_id=o.output_id "
        "ORDER BY o.output_id", {"player": player_id}).fetchall()
    documents: dict[str, bytes] = {}
    for row in rows:
        if row["output_id"] not in OUTPUT_IDS:
            continue
        settings = None if row["switch_input_on_power_on"] is None else DisplaySettings(
            None if row["power_method"] is None else PowerMethod(row["power_method"]),
            row["switch_input_on_power_on"], row["never_off_on_other_input"])
        projected = project_output(row["output_id"], test=None, settings=settings, now=now)
        digest = projected.digest()
        change = row["change"] or 0
        if row["digest"] != digest:
            change += 1
            document = projected.at_change(change)
            conn.execute(
                "INSERT INTO output_documents(player_id, output_id, change, digest, document) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (player_id, output_id) DO UPDATE SET "
                "change=EXCLUDED.change, digest=EXCLUDED.digest, document=EXCLUDED.document",
                (player_id, row["output_id"], change, digest, Jsonb(_document_json(document))))
        documents[output_document_key(row["output_id"])] = encode_output_document(projected.at_change(change))
    return documents


def _document_json(document: OutputDocument) -> dict[str, Any]:
    return json.loads(encode_output_document(document))


# --- The operator API's reads --------------------------------------------------------------------

def _display_view(row: Mapping[str, Any]) -> DisplayView:
    return DisplayView(
        id=row["id"], maker=row["maker"], product=row["product"], name=row["name"], serial=row["serial"],
        tied_to_frame=row["serial"] is None, modes=tuple(ModeView(**mode) for mode in row["modes"]),
        power_method=row["power_method"], switch_input_on_power_on=row["switch_input_on_power_on"],
        never_off_on_other_input=row["never_off_on_other_input"])


class PgDisplayQueries:
    """`DisplayQueries` over PostgreSQL."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def frame_display(self, frame_id: str) -> FrameDisplayView:
        with self._db.transaction() as conn:
            frame = conn.execute(f"SELECT {POSITION_COLUMNS}, b.player_id, b.output_id, seen.connected, "  # noqa: S608
                                 f"seen.reported_at FROM frames f LEFT JOIN bindings b ON b.frame_id=f.id "
                                 f"{SEEN_DISPLAY_JOIN} WHERE f.id=%s", (frame_id,)).fetchone()
            if frame is None:
                raise DisplayRefused("unknown_frame", 404)
            displays = {row["id"]: row for row in conn.execute(
                "SELECT * FROM displays WHERE id=ANY(%s)",
                ([i for i in (frame["seen_display_id"], frame["position_display_id"]) if i is not None],)).fetchall()}
        display, position_display = displays.get(frame["seen_display_id"]), displays.get(frame["position_display_id"])
        return FrameDisplayView(
            frame_id=frame_id, readiness=frame_readiness(frame), player_id=frame["player_id"],
            output_id=frame["output_id"], display=None if display is None else _display_view(display),
            position_display=None if position_display is None else _display_view(position_display),
            connected=frame["connected"], reported_at=frame["reported_at"])


class PgDisplayCommands:
    """`DisplayCommands` over PostgreSQL. Slice C3 implements both and mounts their routes; until
    then no route reaches them."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def request_power_test(self, frame_id: str, power: Power) -> PowerTestAccepted:
        raise NotImplementedError

    def set_power_settings(self, display_id: UUID, settings: DisplayPowerSettings) -> DisplayPowerSettings:
        raise NotImplementedError


__all__ = ["DOCUMENT_STREAM", "POSITION_COLUMNS", "POSITION_FACTS", "REPORT_STREAM", "SEEN_DISPLAY_JOIN",
           "DisplayDocuments", "DisplayWakes", "OutputReportJudge", "PgDisplayCommands", "PgDisplayQueries",
           "commit_position", "frame_position", "frame_readiness", "frame_readiness_in", "output_documents",
           "record_report", "resolve_output"]
