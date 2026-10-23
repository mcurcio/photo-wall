"""Displays in PostgreSQL (roadmap 1b; run ledger .claude/runs/display-1b.md, slice C1; migration 072).

Three adapters over the pure rules in central/displays/model.py, which stay the one home of "same
display" and of the Output document's body:

- `OutputReportJudge`: the `RecordJudge` of the display line's state bucket. Inside the link
  store's commit it decodes each newly recorded Output report and records it on its Output
  (`output_displays`), recognising the Display there in the same transaction (`record_report`),
  and each picture report (`record_picture_report`).
- `DisplayDocuments`: the `DocumentSources` of every link. The fleet pipe's display desired bucket
  holds an Output document and a picture document per Output the Node has (an `outputs` or
  `output_displays` row); `output_documents.change` rises only when the body's digest differs.
  Everything else is `{}`.
- `PgDisplayQueries`: the operator API's reads (`DisplayQueries`).
- `PgDisplayCommands`: the Power tab's writes (`DisplayCommands`, slice C3; migration 074): a
  console test (`power_requests`, one per Frame, ending at `ends_at` on Central's clock, or
  deleted by Turn on) and a Display's power settings. Each wakes the worker's document source in the same transaction by
  `NOTIFY OUTPUT_DOCUMENTS_CHANNEL, '<device_id>'`, sent from app code (no trigger); the worker
  `LISTEN`s on one connection (`DisplayWakes.listen`), and the source also wakes at the earliest
  live `ends_at` of its Node, so a test leaves the document when it ends with no other write.
- Picture (roadmap 1c's Live model; decision 0022, the picture split, slice S2; migration 092):
  each Output also has a picture document on its own key (`picture_documents`), the picture in
  force on the Display last seen there (central/displays/picture.py `effective_picture`), with its
  own `picture_change` that rises only when `picture_digest` differs (never with the Output
  document's `change`); the judge records each picture report on its Output (`picture_reports`);
  `PgPictureSessions` is the Picture tab (`PictureQueries`, `PictureCommands`): the session, its
  edits, Done, Revert, Copy and Restore. Done and Copy store through `write_picture`, the one kept
  picture write, which appends the Display's kept picture history entry in the same transaction
  (configuration snapshots, migration 096; central/infra/picture_history_store.py); each rise of a
  port's `picture_change` with a Display seen there appends the port's sent entry
  (`picture_history.sent_record`: a draft, the wall put back, or the kept values), so what Central
  sent at any time stays readable. Display tuning (decision 0024; migration 094): the Display's
  signal range is stored beside its picture and a session's draft carries it; the session's test
  pattern (`picture_sessions.test_pattern`, `test_pattern_ask`) rides the picture document only
  while the session is live (`picture.session_test_pattern`). The source also wakes at the earliest idle deadline of a
  picture session on its Node's Displays, so an abandoned session's revert reaches the Pi with no
  reaper.

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
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from psycopg.types.json import Jsonb

from central.content_catalog.catalog import device_id_for_serial
from central.db import Database, DatabaseTransactionClock, TransactionClock
from central.displays.hardware import HardwareRow, hardware_rows, kept_report, offered_for
from central.displays.model import (
    DEFAULT_SETTINGS,
    TEST_SECONDS,
    DisplayKey,
    DisplaySettings,
    FramePosition,
    PowerTest,
    Readiness,
    Sighting,
    adopts,
    display_key,
    frame_region,
    live_test,
    power_result,
    power_status,
    project_output,
    readiness,
    region_digest,
    screen_px,
    serial_usable,
)
from central.displays.picture import (
    PICTURE_IDLE_SECONDS,
    DisplayPicture,
    PictureRefused,
    PictureSession,
    copied,
    default_picture,
    edited_hardware,
    edited_light,
    edited_signal_range,
    edited_software,
    edited_test_pattern,
    effective_picture,
    hardware_answered,
    kept_alive,
    kept_picture,
    picture_digest,
    picture_document,
    picture_status,
    presented,
    restore_answered,
    restore_of,
    restored,
    session_test_pattern,
    signal_answered,
    software_touch,
    test_pattern_state,
    with_befores,
    with_restore,
)
from central.displays.picture_history import (
    PictureEntryKind,
    PictureRecord,
    Port,
    copied_record,
    done_record,
    sent_record,
    unrecorded_record,
)
from central.displays.ports import DisplayRefused
from central.displays.views import (
    ChoiceView,
    CopyOutcome,
    DisplayPowerSettings,
    DisplayView,
    FrameDisplayView,
    FramePictureView,
    FramePowerView,
    HardwareEdit,
    HardwareRangeView,
    HardwareResultView,
    HardwareSettingView,
    LightEdit,
    ModeView,
    OfferedView,
    PictureCopyRequest,
    PictureCopyResult,
    PictureRestore,
    PictureRestoreResult,
    PictureSequence,
    PictureSessionView,
    PowerTestAccepted,
    PowerTestView,
    SignalRangeEdit,
    SignalView,
    SoftwareEdit,
    TestPatternEdit,
    TestPatternView,
    in_force_view,
    light_view,
    software_view,
)
from central.fleet.node_bus_presence import bus_links_in
from central.house_time.calculator import day_start, house_date
from central.infra.house_store import house_time_in
from central.infra.listen import listen
from central.infra.node_link_store import PgLinkStores
from central.infra.output_wakes import OUTPUT_DOCUMENTS_CHANNEL, notify_nodes_in
from central.infra.picture_columns import (
    PICTURE_COLUMNS,
    PICTURE_FIELDS,
    picture_fields,
    stored_picture,
)
from central.infra.picture_history_store import (
    append_kept_in,
    append_sent_in,
    latest_kept_in,
    latest_sent_in,
    picture_entry_in,
)
from central.infra.power_store import power_policy_in
from central.installation_models import FrameOutput
from central.live_session import Lease, begin_lease
from central.power.timetable import frame_timetable, unbound_timetable
from contracts.models import Calibration, frame_output, frame_size_mm
from contracts.node_link import Pipe
from contracts.node_output import (
    OUTPUT_IDS,
    DisplayIdentity,
    DisplayMode,
    FrameRegionDocument,
    OutputDocument,
    OutputReport,
    Power,
    PowerMethod,
    decode_output_document,
    decode_output_report,
    encode_output_document,
    encode_output_report,
    encode_region_document,
    output_document_key,
    output_report_key,
    region_document_key,
)
from contracts.node_picture import (
    PICTURE_LIST,
    TEST_PATTERN_SECONDS,
    DisplayLight,
    HardwareSetting,
    PictureDocument,
    PictureReport,
    TestPatternAsk,
    decode_picture_document,
    decode_picture_report,
    encode_picture_document,
    encode_picture_report,
    picture_document_key,
    picture_report_key,
)
from contracts.tuning import TestPattern
from nodeapi.buffers import KV_OPERATION_HEADER, desired_stream, state_prefix, state_stream
from nodeapi.hub import DocumentSource
from nodeapi.pull import Read

log = logging.getLogger(__name__)

REPORT_STREAM: Final = state_stream("display")       # the display line's state bucket: Output and picture reports
DOCUMENT_STREAM: Final = desired_stream("display")   # the display line's desired bucket: Output and picture documents
_REPORT_SUBJECT: Final = state_prefix("display")
_CLOCK: Final = DatabaseTransactionClock()
_LISTEN_RETRY_SECONDS: Final = 1.0
_LISTEN_APPLICATION: Final = "photo-wall-output-documents"
_CONSOLE_TEST: Final = "console-test"            # power_requests.reason of 1b's console test


# --- Readiness facts -----------------------------------------------------------------------------

# A frames row `f`, its binding `b` (LEFT JOIN or JOIN) and this join give POSITION_COLUMNS.
SEEN_DISPLAY_JOIN: Final = ("LEFT JOIN output_displays seen ON seen.player_id=b.player_id "
                            "AND seen.output_id=b.output_id")
POSITION_FACTS: Final = ("bound", "generation", "position_generation", "position_display_id",
                         "seen_display_id")
POSITION_COLUMNS: Final = ("b.player_id IS NOT NULL AS bound, f.generation, f.position_generation, "
                           "f.position_display_id, seen.display_id AS seen_display_id")


def frame_outputs_in(conn: Any) -> tuple[FrameOutput, ...]:
    """Every Frame's FrameOutput, ordered by frame id, in the caller's transaction (follow-ups
    2026-10-11, slice A1; frame box run fb-8): frames LEFT JOIN bindings LEFT JOIN outputs on
    (player_id, output_id) and the Display seen there; `display_px` is `model.screen_px` of the
    Output report's `current_mode` (output_displays.report) and that Display's modes; `turn` is `f.turn`; `output_px` is `contracts.models.frame_output` at that
    turn; `video` is `f.video`; `size_mm` is `contracts.models.frame_size_mm` of the Position's box
    and the seen Display's size (`frame_display_mm`). The one read of Frame outputs: the media
    worker's rendition choice (PgPhotoLibrary), the Planner's slots, authoring, the source preview
    and the fit analysis all ask here. Never raises for a Frame without an Output."""
    rows = conn.execute(
        f"SELECT f.id, f.video, f.calibration, f.turn, {SCREEN_COLUMNS} FROM frames f "  # noqa: S608
        f"LEFT JOIN bindings b ON b.frame_id=f.id {SCREEN_JOIN} ORDER BY f.id").fetchall()
    frames = []
    for row in rows:
        calibration = Calibration.model_validate(row["calibration"])
        display = frame_screen_px(row)
        frames.append(FrameOutput(
            frame_id=row["id"], turn=row["turn"], video=row["video"], display_px=display,
            output_px=frame_output(calibration, display, row["turn"]),
            size_mm=frame_size_mm(calibration.fit, frame_display_mm(row), row["turn"])))
    return tuple(frames)


# After SEEN_DISPLAY_JOIN, this join gives DISPLAY_SIZE_COLUMNS: the seen Display's size
# (`frame_display_mm`), which a binding carries (frame box run fb-9) and SCREEN_COLUMNS include.
DISPLAY_SIZE_JOIN: Final = "LEFT JOIN displays d ON d.id=seen.display_id"
DISPLAY_SIZE_COLUMNS: Final = "d.width_mm AS display_width_mm, d.height_mm AS display_height_mm"

# A frames row `f` and its binding `b` (LEFT JOIN) and this join give SCREEN_COLUMNS: the mode
# Weston runs on the Output, from its Output report (`seen.report`'s `current_mode`, issue #124),
# and the Display seen there (`frame_screen_px`, `frame_display_mm`).
SCREEN_JOIN: Final = f"{SEEN_DISPLAY_JOIN} {DISPLAY_SIZE_JOIN}"
SCREEN_COLUMNS: Final = ("(seen.report->'current_mode'->>0)::int AS current_width_px, "
                         "(seen.report->'current_mode'->>1)::int AS current_height_px, d.modes AS display_modes, "
                         f"{DISPLAY_SIZE_COLUMNS}")
SCREEN_FACTS: Final = ("current_width_px", "current_height_px", "display_modes", "display_width_mm",
                       "display_height_mm")


def frame_screen_px(row: Mapping[str, Any]) -> tuple[int, int] | None:
    """`model.screen_px` of a row selected with SCREEN_COLUMNS."""
    width, height = row["current_width_px"], row["current_height_px"]
    current = None if width is None or height is None else (width, height)
    return screen_px(current, tuple(DisplayMode(**mode) for mode in row["display_modes"] or ()))


def frame_display_mm(row: Mapping[str, Any]) -> tuple[int, int] | None:
    """The seen Display's size of a row selected with DISPLAY_SIZE_COLUMNS (or SCREEN_COLUMNS, which
    hold them; None: none seen, or none stated)."""
    return _size_mm(row["display_width_mm"], row["display_height_mm"])


def _size_mm(width_mm: int | None, height_mm: int | None) -> tuple[int, int] | None:
    """A displays row's size columns as (width, height); None unless both are stated."""
    return None if width_mm is None or height_mm is None else (width_mm, height_mm)


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
               modes: tuple[DisplayMode, ...], size_mm: tuple[int, int] | None, *, before: DisplayIdentity | None, recorded: UUID | None,
               now: float, present: bool = True) -> UUID | None:
    """The Display `identity` is on this Output, by the model's rules: a serial another connected
    Output reports at the same time is recorded shared (for good), and every Output already holding
    that serial's Display is keyed again (`_rekey_shared`); pending (no key) keeps what was recorded
    for the same identity and is None for a new one. "At the same time" counts only Outputs this
    Node reports and those of a Node Central's hub holds now (presence): a dead or switched-off Pi
    never says it was unplugged, so its last rows are not a sighting. `present` False (the identity
    last recorded on a disconnected Output) is no sighting at all: it marks nothing shared."""
    sighting = Sighting(player_id, output_id, identity)
    others: tuple[Sighting, ...] = ()
    if identity.serial is not None and present:
        rows = conn.execute(
            "SELECT o.player_id, o.output_id, o.identity FROM output_displays o JOIN players p ON p.id=o.player_id "
            "LEFT JOIN node_bus_presence n ON n.device_id=p.device_id "
            "WHERE p.retired_at IS NULL AND o.connected AND (o.player_id=%s OR n.linked) "
            "AND o.identity->>'maker'=%s AND (o.identity->>'product')::int=%s AND o.identity->>'serial'=%s",
            (player_id, identity.maker, identity.product, identity.serial)).fetchall()
        others = tuple(Sighting(row["player_id"], row["output_id"], _identity(row["identity"])) for row in rows)
        if not serial_usable(sighting, others, ()):   # reported elsewhere at once: never usable again
            conn.execute("INSERT INTO shared_serials(maker, product, serial, found_at) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT DO NOTHING", (identity.maker, identity.product, identity.serial, now))
            _rekey_shared(conn, identity, now)
    shared = {(row["maker"], row["product"], row["serial"]) for row in conn.execute(
        "SELECT maker, product, serial FROM shared_serials WHERE maker=%s AND product=%s",
        (identity.maker, identity.product)).fetchall()}
    bound = conn.execute("SELECT frame_id FROM bindings WHERE player_id=%s AND output_id=%s",
                         (player_id, output_id)).fetchone()
    key = display_key(identity, serial_counts=serial_usable(sighting, others, shared),
                      bound_frame_id=None if bound is None else bound["frame_id"])
    if key is None:
        return recorded if identity == before else None
    return _display_for(conn, key, identity, modes, size_mm, now)


def _rekey_shared(conn: Any, identity: DisplayIdentity, now: float) -> None:
    """`identity`'s serial was just found shared: every Output that holds the Display keyed by it is
    recognised again without the serial (tied to its bound Frame, or pending when unbound), and the
    bound Frame's Position commit that named the serial's Display names the new one. The same
    monitor on the same port is not a display change, whichever port reported first."""
    rows = conn.execute(
        "SELECT o.player_id, o.output_id, o.identity, o.display_id, o.report, b.frame_id FROM output_displays o "
        "JOIN displays d ON d.id=o.display_id LEFT JOIN bindings b ON b.player_id=o.player_id "
        "AND b.output_id=o.output_id WHERE d.maker=%s AND d.product=%s AND d.serial=%s FOR UPDATE OF o",
        (identity.maker, identity.product, identity.serial)).fetchall()
    for row in rows:
        held = _identity(row["identity"]) or identity
        key = display_key(held, serial_counts=False, bound_frame_id=row["frame_id"])
        modes, size_mm = _sighted(row["report"])
        display_id = None if key is None else _display_for(conn, key, held, modes, size_mm, now)
        conn.execute("UPDATE output_displays SET display_id=%s WHERE player_id=%s AND output_id=%s",
                     (display_id, row["player_id"], row["output_id"]))
        if display_id is not None:
            conn.execute("UPDATE frames SET position_display_id=%s WHERE id=%s AND position_display_id=%s",
                         (display_id, row["frame_id"], row["display_id"]))


def _sighted(report: Mapping[str, Any] | None) -> tuple[tuple[DisplayMode, ...], tuple[int, int] | None]:
    """The modes and the size of a stored Output report (an output_displays row's `report`)."""
    size_mm = (report or {}).get("size_mm")
    return (tuple(DisplayMode(**mode) for mode in (report or {}).get("modes", ())),
            None if size_mm is None else (size_mm[0], size_mm[1]))


def _display_for(conn: Any, key: DisplayKey, identity: DisplayIdentity, modes: tuple[DisplayMode, ...],
                 size_mm: tuple[int, int] | None, now: float) -> UUID:
    """The Display keyed `key`, made on its first sighting; its modes follow the latest sighting
    that states any, its size (width_mm, height_mm) the latest that states one."""
    where, value = ("serial", key.serial) if key.serial is not None else ("frame_id", key.frame_id)
    select = f"SELECT id FROM displays WHERE maker=%s AND product=%s AND {where}=%s"   # noqa: S608 (a column name)
    row = conn.execute(select, (key.maker, key.product, value)).fetchone()
    if row is None:
        light = default_picture().light   # a new Display's light: the placeholder until the paper match
        width, height = size_mm or (None, None)
        conn.execute("INSERT INTO displays(id, maker, product, name, serial, frame_id, modes, width_mm, height_mm, "
                     "first_seen_at, matched_lux, panel_black) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                     "ON CONFLICT DO NOTHING",
                     (uuid.uuid4(), key.maker, key.product, identity.name, key.serial, key.frame_id,
                      _modes_json(modes), width, height, now, light.matched_lux, light.black))
        row = conn.execute(select, (key.maker, key.product, value)).fetchone()
    else:
        if modes:
            conn.execute("UPDATE displays SET modes=%s WHERE id=%s", (_modes_json(modes), row["id"]))
        if size_mm is not None:
            conn.execute("UPDATE displays SET width_mm=%s, height_mm=%s WHERE id=%s", (*size_mm, row["id"]))
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
                                report.size_mm, before=before, recorded=display_id, now=now)
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
    """After a bind of this Output, in the binding's transaction: the identity recorded on it is
    recognised again under the new binding (a pending identity resolves; a display tied to another
    Frame starts over on this one), whether or not it is connected now: a display switched off at
    the bind is the same display when it comes back, so the Position commit made meanwhile names the
    Display it will be seen as. A disconnected Output's identity is no sighting (it marks no serial
    shared). Times are PostgreSQL's, as the judge's."""
    row = conn.execute("SELECT identity, display_id, report, connected FROM output_displays WHERE player_id=%s "
                       "AND output_id=%s FOR UPDATE", (player_id, output_id)).fetchone()
    identity = None if row is None else _identity(row["identity"])
    if identity is None:
        return
    modes, size_mm = _sighted(row["report"])
    display_id = _recognise(conn, player_id, output_id, identity, modes, size_mm, before=identity,
                            recorded=row["display_id"], now=_CLOCK.now_in(conn), present=row["connected"])
    conn.execute("UPDATE output_displays SET display_id=%s WHERE player_id=%s AND output_id=%s",
                 (display_id, player_id, output_id))


def _report_json(report: OutputReport) -> dict[str, Any]:
    return json.loads(encode_output_report(report))


def _player_of(conn: Any, device_id: str, lock: str) -> str | None:
    """The Node's non-retired player, its row locked (`lock`); None when it has none."""
    row = conn.execute(f"SELECT id FROM players WHERE device_id=%s AND retired_at IS NULL {lock}",  # noqa: S608
                       (device_id,)).fetchone()
    return None if row is None else row["id"]


# --- The Display's picture (decision 0022, the picture split, slice S2; migration 092) -----------

def write_picture(conn: Any, display_id: UUID, record: PictureRecord) -> None:
    """Store `record.picture` on the Display and append `record` as its next kept picture history
    entry, in the caller's transaction (a failed append fails the Done or Copy): the one kept picture
    write. First, when the stored values differ from the Display's newest kept entry, the change
    made outside Photo Wall is recorded (`picture_history.unrecorded_record`).
    ValueError("picture_history_kind") unless `record` is KEPT_DONE or KEPT_COPIED."""
    if record.kind not in (PictureEntryKind.KEPT_DONE, PictureEntryKind.KEPT_COPIED):
        raise ValueError("picture_history_kind")
    row = conn.execute(f"SELECT {PICTURE_COLUMNS} FROM displays d WHERE d.id=%s FOR NO KEY UPDATE",  # noqa: S608
                       (display_id,)).fetchone()
    unrecorded = unrecorded_record(latest_kept_in(conn, display_id), stored_picture(row))
    if unrecorded is not None:
        append_kept_in(conn, display_id, unrecorded)
    fields = {**picture_fields(record.picture), "hardware_picture": Jsonb(dict(record.picture.hardware))}
    conn.execute(f"UPDATE displays SET {', '.join(f + '=%s' for f in PICTURE_FIELDS)} WHERE id=%s",  # noqa: S608
                 (*(fields[f] for f in PICTURE_FIELDS), display_id))
    append_kept_in(conn, display_id, record)


# A picture_sessions row `ps` gives these; `picture_session` reads them.
_SESSION_COLUMNS: Final = (
    "ps.id AS session_id, ps.display_id AS session_display_id, ps.frame_id AS session_frame_id, "
    "ps.sequence AS session_sequence, ps.draft AS session_draft, ps.began_at_change, ps.hardware_before, "
    "ps.touched_hardware, ps.touched_software, ps.idle_until AS session_idle_until, "
    "ps.test_pattern AS session_test_pattern, ps.test_pattern_ask AS session_test_pattern_ask, "
    "ps.restored_from AS session_restored_from")


def picture_session(row: Mapping[str, Any]) -> PictureSession | None:
    """The picture session of a row selected with `_SESSION_COLUMNS` (None when it has none). It has
    no hard deadline: the console's keepalive holds it, and its idle deadline ends it."""
    if row["session_id"] is None:
        return None
    return PictureSession(row["session_id"], row["session_display_id"], row["session_sequence"],
                          stored_picture(row["session_draft"]), row["began_at_change"],
                          {code: int(value) for code, value in row["hardware_before"].items()},
                          frozenset(row["touched_hardware"]),
                          frozenset(software_touch(name) for name in row["touched_software"]),
                          Lease(row["session_idle_until"], None), _test_pattern_ask(row),
                          row["session_restored_from"])


def _test_pattern_ask(row: Mapping[str, Any]) -> TestPatternAsk | None:
    """The session's test pattern ask of a row selected with `_SESSION_COLUMNS` (None: off)."""
    pattern = row["session_test_pattern"]
    return None if pattern is None else TestPatternAsk(TestPattern(pattern), row["session_test_pattern_ask"])


def record_picture_report(conn: Any, player_id: str, report: PictureReport, now: float) -> None:
    """Record `report` as its Output's latest picture report (`picture_reports`), as
    `hardware.kept_report` of the one recorded before (a "not known yet" never replaces a known list
    for the same display, nor while none is connected); then the session on the Display recorded on
    that Output, if any, keeps the befores the report brings (`picture.with_befores`), and an ended
    one goes once the report answers the document that puts the display back
    (`picture.restore_answered`). Locks after the player row (the judge's): the Output's picture
    report row, then the session row and its Display."""
    previous = conn.execute("SELECT report AS picture_report FROM picture_reports WHERE player_id=%s "
                            "AND output_id=%s FOR UPDATE", (player_id, report.output_id)).fetchone()
    report = kept_report(None if previous is None else _stored_picture_report(previous), report)
    conn.execute(
        "INSERT INTO picture_reports(player_id, output_id, report, reported_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (player_id, output_id) DO UPDATE SET report=EXCLUDED.report, reported_at=EXCLUDED.reported_at",
        (player_id, report.output_id, Jsonb(json.loads(encode_picture_report(report))), now))
    row = conn.execute(f"SELECT {_SESSION_COLUMNS}, {PICTURE_COLUMNS}, pd.document AS picture_document "  # noqa: S608
                       "FROM output_displays o JOIN picture_sessions ps ON ps.display_id=o.display_id "
                       "JOIN displays d ON d.id=ps.display_id LEFT JOIN picture_documents pd "
                       "ON pd.player_id=o.player_id AND pd.output_id=o.output_id "
                       "WHERE o.player_id=%s AND o.output_id=%s FOR UPDATE OF ps, d",
                       (player_id, report.output_id)).fetchone()
    session = None if row is None else picture_session(row)
    if session is not None:
        kept = with_befores(session, report)
        if kept.hardware_before != session.hardware_before:
            conn.execute("UPDATE picture_sessions SET hardware_before=%s WHERE id=%s",
                         (Jsonb(dict(kept.hardware_before)), session.session_id))
        session = kept
    display = conn.execute(f"SELECT d.id AS d_id, {PICTURE_COLUMNS}, d.hardware_restore, "  # noqa: S608
                           "pd.document AS picture_document FROM output_displays o JOIN displays d ON d.id=o.display_id "
                           "LEFT JOIN picture_documents pd ON pd.player_id=o.player_id AND pd.output_id=o.output_id "
                           "WHERE o.player_id=%s AND o.output_id=%s FOR UPDATE OF d",
                           (player_id, report.output_id)).fetchone()
    if display is None:
        return
    stored, restore = stored_picture(display), dict(display["hardware_restore"])
    if session is not None and not session.lease.live(now):
        restore = {**restore, **_fold_session_in(conn, stored, session)}   # ended unseen: fold it now
        session = None
    in_force = stored if session is None else session.draft
    if restore and restore_answered(restore, in_force, _stored_picture_document(display), report):
        conn.execute("UPDATE displays SET hardware_restore='{}'::jsonb WHERE id=%s", (display["d_id"],))
        notify_nodes_in(conn, _NODES_SEEING, (display["d_id"],))


# --- The Output report judge ---------------------------------------------------------------------

class OutputReportJudge:
    """`RecordJudge` for REPORT_STREAM: each newly recorded `output-…` report is recorded on its
    Output (`record_report`), and each `picture-…` report as its Output's picture report
    (`record_picture_report`), in the commit that records it; a record that is not a valid report
    for its key is logged with its coordinates and skipped. A Node with no player yet is only
    recorded. `wakes` tells this process's document source that the Node's facts changed."""

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
            if not key.startswith(("output-", "picture-")) or (item.headers or {}).get(KV_OPERATION_HEADER):
                continue
            try:
                report = _decoded_report(key, item.data)
            except ValueError as refused:
                log.warning("display report refused: device=%s stream=%s epoch=%s seq=%s key=%s: %s",
                            device_id, stream, item.token.epoch, item.token.seq, key, refused)
                continue
            if isinstance(report, OutputReport):
                record_report(conn, player_id, report, now)
            else:
                record_picture_report(conn, player_id, report, now)
            judged = True
        if judged:
            self._wakes.wake(device_id)


def _decoded_report(key: str, data: bytes) -> OutputReport | PictureReport:
    """The report a state-bucket record holds under `key` ("output-…" or "picture-…");
    ValueError for a record that is not a valid report for that key."""
    if key.startswith("output-"):
        report = decode_output_report(data)
        if output_report_key(report.output_id) != key:
            raise ValueError("output_report_key")
        return report
    picture = decode_picture_report(data)
    if picture_report_key(picture.output_id) != key:
        raise ValueError("picture_report_key")
    return picture


# --- The Output document source ------------------------------------------------------------------

class DisplayWakes:
    """Which Nodes' display facts changed: `wake` from any thread (the judge runs in a store worker
    thread), `after` on the event loop; `listen` carries other processes' commands (the operator
    API's NOTIFY). A count per Node, plus one for all, so a waiter never misses a wake that came
    between two waits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._all = 0
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}

    def count(self, device_id: str) -> int:
        with self._lock:
            return self._counts.get(device_id, 0) + self._all

    def wake(self, device_id: str) -> None:
        with self._lock:
            self._counts[device_id] = self._counts.get(device_id, 0) + 1
            waiters = self._waiters.pop(device_id, [])
        self._set(waiters)

    def wake_all(self) -> None:
        """Every Node's facts may have changed (notifications may have been missed)."""
        with self._lock:
            self._all += 1
            waiters = [waiter for waiting in self._waiters.values() for waiter in waiting]
            self._waiters.clear()
        self._set(waiters)

    @staticmethod
    def _set(waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]) -> None:
        for loop, event in waiters:
            with contextlib.suppress(RuntimeError):   # that loop is closed: nobody waits there any more
                loop.call_soon_threadsafe(event.set)

    async def listen(self, dsn: str) -> None:
        """Until cancelled: `LISTEN OUTPUT_DOCUMENTS_CHANNEL` on one connection of its own and wake
        each notified Node. Every (re)connect wakes all, since notifications sent while no
        connection listened are lost; a lost connection is logged and retried."""
        await listen(dsn, OUTPUT_DOCUMENTS_CHANNEL, application=_LISTEN_APPLICATION, connected=self.wake_all,
                     notified=self.wake, retry_seconds=_LISTEN_RETRY_SECONDS)

    async def after(self, device_id: str, seen: int) -> int:
        """The Node's count, once it differs from `seen`."""
        loop = asyncio.get_running_loop()
        while True:
            event = asyncio.Event()
            with self._lock:
                count = self._counts.get(device_id, 0) + self._all
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

    def __init__(self, stores: PgLinkStores, wakes: DisplayWakes, *, times: TransactionClock) -> None:
        self._stores = stores
        self._wakes = wakes
        self._times = times

    def source(self, serial: str, pipe: Pipe) -> DocumentSource:
        device_id = device_id_for_serial(serial)
        if device_id is None:
            raise ValueError("node_link_serial")
        if Pipe(pipe) is not Pipe.FLEET:
            return _NoDocuments()
        return _OutputDocuments(self._stores, self._wakes, device_id, self._times)


class _NoDocuments:
    async def documents(self, stream: str) -> Mapping[str, bytes]:
        return {}

    async def changed(self) -> None:
        await asyncio.Event().wait()   # nothing here ever changes


class _OutputDocuments:
    def __init__(self, stores: PgLinkStores, wakes: DisplayWakes, device_id: str, times: TransactionClock) -> None:
        self._stores = stores
        self._wakes = wakes
        self._device_id = device_id
        self._times = times
        self._seen = wakes.count(device_id)

    async def documents(self, stream: str) -> Mapping[str, bytes]:
        if stream != DOCUMENT_STREAM:
            return {}
        return await self._stores.run(
            lambda conn: output_documents(conn, self._device_id, self._times.now_in(conn)))

    async def changed(self) -> None:
        """When this Node's facts are woken, or at its next deadline (`_until_next_deadline`,
        measured from Central's clock, slept on this process's), so an ended test, an abandoned
        session's revert and the timetable's roll forward reach the Pi with no other write and no
        reaper."""
        seen = self._seen
        remaining = await self._stores.run(
            lambda conn: _until_next_deadline(conn, self._device_id, self._times.now_in(conn)))
        try:
            self._seen = await asyncio.wait_for(self._wakes.after(self._device_id, seen), remaining)
        except TimeoutError:
            return


def _until_next_deadline(conn: Any, device_id: str, now: float) -> float:
    """Seconds from Central's `now` until the earliest of: a live console test on the Node's
    Outputs ends, a picture session's idle deadline on a Display last seen on them, the Node's
    timetables' first change after now (a hold's end is one when it changes the power; one that
    does not changes no document), and the next house midnight (the horizon moves), which always
    comes."""
    row = conn.execute(
        "SELECT least((SELECT min(r.ends_at) FROM players p JOIN bindings b ON b.player_id=p.id "
        "JOIN power_requests r ON r.frame_id=b.frame_id WHERE p.device_id=%(device)s "
        "AND p.retired_at IS NULL AND r.ends_at > %(now)s), "
        "(SELECT min(ps.idle_until) FROM players p JOIN output_displays o ON o.player_id=p.id "
        "JOIN picture_sessions ps ON ps.display_id=o.display_id WHERE p.device_id=%(device)s "
        "AND p.retired_at IS NULL AND ps.idle_until > %(now)s)) AS deadline",
        {"device": device_id, "now": now}).fetchone()
    deadlines = [] if row["deadline"] is None else [row["deadline"]]
    house = house_time_in(conn)
    policy = power_policy_in(conn, now)
    deadlines.append(day_start(house, house_date(house, now) + timedelta(days=1)))
    for frame_id in _bound_frames(conn, device_id):
        following = frame_timetable(frame_id, policy, house, now).next_change_after(now)
        if following is not None:
            deadlines.append(following.at)
    return max(0.0, min(deadlines) - now)


def _bound_frames(conn: Any, device_id: str) -> tuple[str, ...]:
    return tuple(row["frame_id"] for row in conn.execute(
        "SELECT b.frame_id FROM players p JOIN bindings b ON b.player_id=p.id "
        "WHERE p.device_id=%s AND p.retired_at IS NULL ORDER BY b.frame_id", (device_id,)).fetchall())


def _power_test(row: Mapping[str, Any]) -> PowerTest | None:
    """The console test of a row selected with `r.id AS test_id, r.power AS test_power,
    r.for_seconds, r.ends_at` from power_requests `r` (None when the row has none)."""
    if row["test_id"] is None:
        return None
    return PowerTest(row["test_id"], Power(row["test_power"]), row["for_seconds"], row["ends_at"])


_TEST_COLUMNS: Final = "r.id AS test_id, r.power AS test_power, r.for_seconds, r.ends_at"
_TEST_JOIN: Final = f"LEFT JOIN power_requests r ON r.frame_id=b.frame_id AND r.reason='{_CONSOLE_TEST}'"


def output_documents(conn: Any, device_id: str, now: float) -> dict[str, bytes]:
    """key -> encoded document for each Output of the Node at Central's `now`: its Output document,
    raising `change` only when its body differs from the one recorded, and its picture document,
    raising `picture_change` only when `picture_digest` differs (each recorded on its own row, so
    neither number moves with the other). A bound Output carries its Frame's timetable
    (`frame_timetable` over `power_policy_in` and `house_time_in`), an unbound one
    `unbound_timetable`; each carries its bound Frame's turn (frames.turn), 0 unbound, so a turn
    raises `change` like a power edit; an Output with no Display seen gets `default_picture()` and no test
    pattern, one seen carries its live session's test pattern (`picture.session_test_pattern`). Its
    Frame region document (issue #124) carries `model.frame_region` of the bound Frame's Position,
    its screen (`frame_screen_px`) and its turn, null unbound or with no screen known, raising
    `region_change` only when `region_digest` differs: a saved Position or a new mode never raises
    the Output document's `change`."""
    player_id = _player_of(conn, device_id, "FOR SHARE")
    if player_id is None:
        return {}
    house = house_time_in(conn)
    policy = power_policy_in(conn, now)
    rows = conn.execute(
        "SELECT o.output_id, b.frame_id, d.id AS d_id, d.power_method, d.switch_input_on_power_on, d.never_off_on_other_input, "
        f"{PICTURE_COLUMNS}, d.hardware_restore, {_SESSION_COLUMNS}, w.change, w.digest, w.region_change, "
        "w.region_digest, pd.picture_change, f.calibration, "
        f"pd.digest AS picture_digest, COALESCE(f.turn, 0) AS turn, {_TEST_COLUMNS}, {SCREEN_COLUMNS} "
        "FROM (SELECT output_id FROM outputs WHERE player_id=%(player)s "
        "UNION SELECT output_id FROM output_displays WHERE player_id=%(player)s) o "
        "LEFT JOIN output_displays seen ON seen.player_id=%(player)s AND seen.output_id=o.output_id "
        "LEFT JOIN displays d ON d.id=seen.display_id LEFT JOIN picture_sessions ps ON ps.display_id=d.id "
        "LEFT JOIN output_documents w ON w.player_id=%(player)s AND w.output_id=o.output_id "
        "LEFT JOIN picture_documents pd ON pd.player_id=%(player)s AND pd.output_id=o.output_id "
        f"LEFT JOIN bindings b ON b.player_id=%(player)s AND b.output_id=o.output_id {_TEST_JOIN} "
        "LEFT JOIN frames f ON f.id=b.frame_id ORDER BY o.output_id", {"player": player_id}).fetchall()
    documents: dict[str, bytes] = {}
    for row in rows:
        output_id = row["output_id"]
        if output_id not in OUTPUT_IDS:
            continue
        timetable = (unbound_timetable(house, now) if row["frame_id"] is None
                     else frame_timetable(row["frame_id"], policy, house, now))
        projected = project_output(output_id, timetable=timetable, test=_power_test(row),
                                   settings=_settings(row), turn=row["turn"], now=now)
        digest = projected.digest()
        change = (row["change"] or 0) + (row["digest"] != digest)
        document = projected.at_change(change)
        if change != row["change"]:
            conn.execute(
                "INSERT INTO output_documents(player_id, output_id, change, digest, document) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (player_id, output_id) DO UPDATE SET "
                "change=EXCLUDED.change, digest=EXCLUDED.digest, document=EXCLUDED.document",
                (player_id, output_id, change, digest, Jsonb(_document_json(document))))
        documents[output_document_key(output_id)] = encode_output_document(document)

        region = None if row["frame_id"] is None else frame_region(
            row["frame_id"], Calibration.model_validate(row["calibration"]).fit, frame_screen_px(row), row["turn"])
        digest = region_digest(region)
        region_change = (row["region_change"] or 0) + (row["region_digest"] != digest)
        if region_change != row["region_change"]:
            conn.execute("UPDATE output_documents SET region_change=%s, region_digest=%s "
                         "WHERE player_id=%s AND output_id=%s", (region_change, digest, player_id, output_id))
        documents[region_document_key(output_id)] = encode_region_document(
            FrameRegionDocument(output_id, region_change, region))

        picture = _picture_in_force(row, now)
        pattern = None if _no_display(row) else session_test_pattern(picture_session(row), now)
        digest = picture_digest(picture, pattern)
        picture_change = (row["picture_change"] or 0) + (row["picture_digest"] != digest)
        wanted = picture_document(output_id, picture_change, picture, pattern)
        if picture_change != row["picture_change"]:
            conn.execute(
                "INSERT INTO picture_documents(player_id, output_id, picture_change, digest, document) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (player_id, output_id) DO UPDATE SET "
                "picture_change=EXCLUDED.picture_change, digest=EXCLUDED.digest, document=EXCLUDED.document",
                (player_id, output_id, picture_change, digest,
                 Jsonb(json.loads(encode_picture_document(wanted)))))
            if not _no_display(row):
                port = Port(player_id, output_id)
                append_sent_in(conn, port, picture_change, display_id=row["d_id"], frame_id=row["frame_id"],
                               record=sent_record(session=picture_session(row), now=now,
                                                  previous=latest_sent_in(conn, port), picture=picture,
                                                  test_pattern=pattern, display_id=row["d_id"]))
        documents[picture_document_key(output_id)] = encode_picture_document(wanted)
    return documents


def _picture_in_force(row: Mapping[str, Any], now: float) -> DisplayPicture:
    """What the picture document carries for the Display last seen on the Output: the picture in
    force (its open session's draft while live, `reverted` once the lease ran out with no Done;
    `picture.effective_picture`) with a Revert's pending instruction beside it
    (`picture.with_restore`), or a new Display's (`default_picture`) when none was seen."""
    if _no_display(row):
        return default_picture()
    return with_restore(effective_picture(stored_picture(row), picture_session(row), now),
                        {code: int(value) for code, value in row["hardware_restore"].items()})


def _no_display(row: Mapping[str, Any]) -> bool:
    """No Display was seen on the Output of an `output_documents` row (its displays columns are null)."""
    return row["colour_temperature_kelvin"] is None


def _settings(row: Mapping[str, Any]) -> DisplaySettings | None:
    """The power settings of a displays row's columns (None when the row has none)."""
    if row["switch_input_on_power_on"] is None:
        return None
    return DisplaySettings(None if row["power_method"] is None else PowerMethod(row["power_method"]),
                           row["switch_input_on_power_on"], row["never_off_on_other_input"])


def _document_json(document: OutputDocument) -> dict[str, Any]:
    return json.loads(encode_output_document(document))


# --- The operator API's reads --------------------------------------------------------------------

def _display_view(row: Mapping[str, Any]) -> DisplayView:
    return DisplayView(
        id=row["id"], maker=row["maker"], product=row["product"], name=row["name"], serial=row["serial"],
        tied_to_frame=row["serial"] is None, modes=tuple(ModeView(**mode) for mode in row["modes"]),
        power_method=row["power_method"], switch_input_on_power_on=row["switch_input_on_power_on"],
        never_off_on_other_input=row["never_off_on_other_input"],
        size_mm=_size_mm(row["width_mm"], row["height_mm"]))


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

    def frame_power(self, frame_id: str) -> FramePowerView:
        with self._db.transaction() as conn:
            frame = conn.execute(
                f"SELECT b.player_id, b.output_id, p.device_id, seen.display_id, seen.report, seen.reported_at, "  # noqa: S608
                f"d.power_method, d.switch_input_on_power_on, d.never_off_on_other_input, w.document, "
                f"{_TEST_COLUMNS} FROM frames f LEFT JOIN bindings b ON b.frame_id=f.id "
                f"LEFT JOIN players p ON p.id=b.player_id {SEEN_DISPLAY_JOIN} "
                f"LEFT JOIN displays d ON d.id=seen.display_id LEFT JOIN output_documents w "
                f"ON w.player_id=b.player_id AND w.output_id=b.output_id "
                f"LEFT JOIN power_requests r ON r.frame_id=f.id AND r.reason='{_CONSOLE_TEST}' WHERE f.id=%s",
                (frame_id,)).fetchone()
            if frame is None:
                raise DisplayRefused("unknown_frame", 404)
            now = _CLOCK.now_in(conn)
            house = house_time_in(conn)
            presence = None if frame["device_id"] is None else (
                bus_links_in(conn, (frame["device_id"],))[frame["device_id"]])
        test = _power_test(frame)
        live = live_test(test, now)
        linked = None if presence is None else presence["linked"]
        document, report = _stored_document(frame), _stored_report(frame)
        status = power_status(bound=frame["player_id"] is not None, test=test, document=document,
                              report=report, now=now)
        settings = _settings(frame) or DEFAULT_SETTINGS
        return FramePowerView(
            frame_id=frame_id, status=status, player_id=frame["player_id"], output_id=frame["output_id"],
            display_id=frame["display_id"], power_method=settings.power_method,
            switch_input_on_power_on=settings.switch_input_on_power_on,
            never_off_on_other_input=settings.never_off_on_other_input,
            answers=() if report is None else report.answers,
            method_in_use=None if report is None else report.method,
            test=None if live is None else PowerTestView(request_id=live.request_id, power=live.power,
                                                         for_seconds=live.for_seconds, ends_at=live.ends_at),
            result=power_result(test=test, document=document, report=report, now=now),
            in_force=in_force_view(report, house, live, now), linked=linked,
            # The link's presence row changed to unlinked when Central's hub last saw the Pi's leaf
            # go: the last time Central heard it. A report time would be the last change the Pi
            # put, hours old on a quiet display.
            last_heard_at=presence["changed_at"] if linked is False else None)


@dataclass(frozen=True)
class FramePowerFacts:
    """What Central holds about a Frame's display power, read in one transaction: whether it is
    bound to an Output, its 1b console test, its Output's latest document and the Pi's latest
    report (None when there is none, or under an older schema major)."""
    bound: bool
    test: PowerTest | None
    document: OutputDocument | None
    report: OutputReport | None


def power_facts_in(conn: Any, frame_id: str) -> FramePowerFacts | None:
    """The Frame's power facts in the caller's transaction; None for an unknown Frame."""
    frame = conn.execute(
        f"SELECT b.player_id, seen.report, w.document, {_TEST_COLUMNS} FROM frames f "  # noqa: S608
        f"LEFT JOIN bindings b ON b.frame_id=f.id {SEEN_DISPLAY_JOIN} "
        f"LEFT JOIN output_documents w ON w.player_id=b.player_id AND w.output_id=b.output_id "
        f"LEFT JOIN power_requests r ON r.frame_id=f.id AND r.reason='{_CONSOLE_TEST}' WHERE f.id=%s",
        (frame_id,)).fetchone()
    if frame is None:
        return None
    return FramePowerFacts(bound=frame["player_id"] is not None, test=_power_test(frame),
                           document=_stored_document(frame), report=_stored_report(frame))


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    """A wire record stored as JSONB, back to bytes its strict decoder reads."""
    return json.dumps(value, ensure_ascii=False).encode()


class PgDisplayCommands:
    """`DisplayCommands` over PostgreSQL. Each command wakes the worker's document source of every
    Node it changes, in its own transaction (`notify_nodes_in`)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def request_power_test(self, frame_id: str) -> PowerTestAccepted:
        request_id = uuid.uuid4()
        with self._db.transaction() as conn:
            frame = conn.execute("SELECT b.player_id FROM frames f LEFT JOIN bindings b ON b.frame_id=f.id "
                                 "WHERE f.id=%s FOR NO KEY UPDATE OF f", (frame_id,)).fetchone()
            if frame is None:
                raise DisplayRefused("unknown_frame", 404)
            if frame["player_id"] is None:
                raise DisplayRefused("frame_unbound", 409)
            now = _CLOCK.now_in(conn)
            conn.execute(
                "INSERT INTO power_requests(id, frame_id, power, reason, for_seconds, created_at, ends_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (frame_id, reason) DO UPDATE SET "
                "id=EXCLUDED.id, power=EXCLUDED.power, for_seconds=EXCLUDED.for_seconds, "
                "created_at=EXCLUDED.created_at, ends_at=EXCLUDED.ends_at",
                (request_id, frame_id, Power.OFF.value, _CONSOLE_TEST, TEST_SECONDS, now,
                 now + TEST_SECONDS))
            notify_nodes_in(conn, "SELECT device_id FROM players WHERE id=%s", (frame["player_id"],))
        return PowerTestAccepted(request_id=request_id, for_seconds=TEST_SECONDS)

    def end_power_test(self, frame_id: str) -> None:
        with self._db.transaction() as conn:
            frame = conn.execute("SELECT b.player_id FROM frames f LEFT JOIN bindings b ON b.frame_id=f.id "
                                 "WHERE f.id=%s FOR NO KEY UPDATE OF f", (frame_id,)).fetchone()
            if frame is None:
                raise DisplayRefused("unknown_frame", 404)
            ended = conn.execute("DELETE FROM power_requests WHERE frame_id=%s AND reason=%s RETURNING id",
                                 (frame_id, _CONSOLE_TEST)).fetchone()
            if ended is not None and frame["player_id"] is not None:
                notify_nodes_in(conn, "SELECT device_id FROM players WHERE id=%s", (frame["player_id"],))

    def set_power_settings(self, display_id: UUID, settings: DisplayPowerSettings) -> DisplayPowerSettings:
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE displays SET power_method=%s, switch_input_on_power_on=%s, never_off_on_other_input=%s "
                "WHERE id=%s RETURNING power_method, switch_input_on_power_on, never_off_on_other_input",
                (None if settings.power_method is None else PowerMethod(settings.power_method).value,
                 settings.switch_input_on_power_on, settings.never_off_on_other_input, display_id)).fetchone()
            if row is None:
                raise DisplayRefused("unknown_display", 404)
            notify_nodes_in(conn, "SELECT DISTINCT p.device_id FROM output_displays o JOIN players p ON p.id=o.player_id "
                          "WHERE o.display_id=%s", (display_id,))
        return DisplayPowerSettings(**row)


# --- The Picture tab (roadmap 1c's Live model; the picture split, slice S2) ----------------------

# The Output's recorded identity, latest picture document and picture report, joined to a binding
# `b` and SEEN_DISPLAY_JOIN.
_PICTURE_FACTS: Final = (
    "seen.identity AS seen_identity, pd.picture_change AS output_picture_change, "
    "pd.document AS picture_document, pr.report AS picture_report")
_PICTURE_FACTS_JOIN: Final = (
    "LEFT JOIN picture_documents pd ON pd.player_id=b.player_id AND pd.output_id=b.output_id "
    "LEFT JOIN picture_reports pr ON pr.player_id=b.player_id AND pr.output_id=b.output_id")

# A Frame `f` with its binding, the Display last seen on its Output, that Display's open session
# and the Output's picture facts.
_PICTURE_FRAME: Final = (
    f"SELECT f.id AS frame_id, b.player_id, seen.display_id, seen.connected, {_PICTURE_FACTS}, "  # noqa: S608
    f"d.id AS d_id, {PICTURE_COLUMNS}, {_SESSION_COLUMNS} FROM frames f LEFT JOIN bindings b ON b.frame_id=f.id "
    f"{SEEN_DISPLAY_JOIN} LEFT JOIN displays d ON d.id=seen.display_id "
    f"LEFT JOIN picture_sessions ps ON ps.display_id=d.id {_PICTURE_FACTS_JOIN}")

# A session `ps` with its Display and the picture facts of the Output of the Frame it was opened on.
_PICTURE_SESSION: Final = (
    f"SELECT {_SESSION_COLUMNS}, {PICTURE_COLUMNS}, {_PICTURE_FACTS} FROM picture_sessions ps "  # noqa: S608
    "JOIN displays d ON d.id=ps.display_id LEFT JOIN bindings b ON b.frame_id=ps.frame_id "
    f"{SEEN_DISPLAY_JOIN} {_PICTURE_FACTS_JOIN} WHERE ps.id=%s FOR UPDATE OF ps, d")

_NODES_SEEING: Final = ("SELECT DISTINCT p.device_id FROM output_displays o JOIN players p ON p.id=o.player_id "
                        "WHERE o.display_id=%s")


def _stored_document(row: Mapping[str, Any]) -> OutputDocument | None:
    """The recorded document; None when there is none, or it was written under an older schema
    major (the source rewrites it at its next read: no backwards compatibility)."""
    try:
        return None if row["document"] is None else decode_output_document(_json_bytes(row["document"]))
    except ValueError:
        return None


def _stored_report(row: Mapping[str, Any]) -> OutputReport | None:
    """The recorded report; None when there is none, or it was written under an older schema major."""
    try:
        return None if row["report"] is None else decode_output_report(_json_bytes(row["report"]))
    except ValueError:
        return None


def _stored_picture_document(row: Mapping[str, Any]) -> PictureDocument | None:
    """The Output's recorded picture document; None when there is none, or it is the placeholder
    migration 092 left (the source rewrites it at its next read)."""
    try:
        return None if row["picture_document"] is None else decode_picture_document(
            _json_bytes(row["picture_document"]))
    except ValueError:
        return None


def _stored_picture_report(row: Mapping[str, Any]) -> PictureReport | None:
    """The Output's recorded picture report; None when there is none."""
    try:
        return None if row["picture_report"] is None else decode_picture_report(_json_bytes(row["picture_report"]))
    except ValueError:
        return None


def _offered(row: Mapping[str, Any]) -> tuple[HardwareSetting, ...] | None:
    """What the display recorded on the Output offers (`hardware.offered_for` of the Output's
    recorded identity and picture report); None: not known."""
    return offered_for(_identity(row["seen_identity"]), _stored_picture_report(row))


def _hardware_view(row: HardwareRow) -> HardwareSettingView:
    offered, result = row.offered, row.result
    return HardwareSettingView(
        code=row.code, name=PICTURE_LIST[row.code], value=row.value,
        offered=None if offered is None else OfferedView(
            range=HardwareRangeView(kind=offered.range.kind, maximum=offered.range.maximum,
                                    choices=tuple(ChoiceView(value=choice.value, name=choice.name)
                                                  for choice in offered.range.choices)),
            current=offered.current),
        result=None if result is None else HardwareResultView(result=result.result, before=result.before,
                                                              after=result.after),
        changed_on_buttons=row.changed_on_buttons)


class PgPictureSessions:
    """`PictureQueries` and `PictureCommands` over PostgreSQL (displays, picture_sessions,
    picture_documents, picture_reports). The rules are central/displays/picture.py's and
    hardware.py's; every write locks the Display row and wakes the document source of every Node
    that last saw that Display, in its own transaction (`notify_nodes_in`)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def frame_picture(self, frame_id: str) -> FramePictureView:
        with self._db.transaction() as conn:
            row = conn.execute(_PICTURE_FRAME + " WHERE f.id=%s", (frame_id,)).fetchone()
            if row is None:
                raise DisplayRefused("unknown_frame", 404)
            now = _CLOCK.now_in(conn)
            session = picture_session(row)
            picture = default_picture() if row["d_id"] is None else kept_picture(stored_picture(row), session, now)
        document, report = _stored_picture_document(row), _stored_picture_report(row)
        offered = _offered(row)
        answered = report is not None and hardware_answered(picture.hardware, document, report)
        live = session is not None and session.lease.live(now)
        status = picture_status(bound=row["player_id"] is not None and row["display_id"] is not None,
                                picture=picture, document=document, report=report)
        signal = None if report is None or report.signal is None else SignalView(
            in_force=report.signal.in_force, result=report.signal.result,
            answered=signal_answered(document, report))
        ask = session_test_pattern(session, now)
        return FramePictureView(
            frame_id=frame_id, display_id=row["display_id"], status=status, connected=row["connected"],
            offered_known=offered is not None,
            hardware=tuple(_hardware_view(hardware) for hardware in hardware_rows(
                picture.hardware, offered, report.results if answered and report is not None else None,
                connected=row["connected"] is True)),
            software=software_view(picture.software), light=light_view(picture.light),
            signal_range=picture.signal_range, signal=signal,
            test_pattern=None if ask is None else TestPatternView(
                pattern=ask.pattern, state=test_pattern_state(ask, document, report)),
            test_pattern_seconds=TEST_PATTERN_SECONDS,
            session=_session_view(session) if live and session is not None else None)

    def begin_picture(self, frame_id: str) -> PictureSessionView:
        with self._db.transaction() as conn:
            frame = conn.execute(_PICTURE_FRAME + " WHERE f.id=%s FOR NO KEY UPDATE OF f", (frame_id,)).fetchone()
            if frame is None:
                raise DisplayRefused("unknown_frame", 404)
            if frame["player_id"] is None:
                raise DisplayRefused("frame_unbound", 409)
            display_id = frame["display_id"]
            if display_id is None:
                raise DisplayRefused("no_display", 409)
            now = _CLOCK.now_in(conn)
            stored = _fold_in(conn, display_id, now)
            if stored is None:
                raise DisplayRefused("session_open", 409)
            lease = begin_lease(now, idle_seconds=PICTURE_IDLE_SECONDS, hard_seconds=None)
            session = PictureSession(uuid.uuid4(), display_id, 1, stored, frame["output_picture_change"] or 0, {},
                                     frozenset(), frozenset(), lease, None, None)
            conn.execute(
                "INSERT INTO picture_sessions(id, display_id, frame_id, sequence, draft, began_at_change, "
                "idle_until, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (session.session_id, display_id, frame_id, session.sequence, Jsonb(picture_fields(stored)),
                 session.began_at_change, lease.idle_until, now))
            notify_nodes_in(conn, _NODES_SEEING, (display_id,))
        return _session_view(session)

    def edit_hardware(self, session_id: UUID, edit: HardwareEdit) -> PictureSequence:
        with self._db.transaction() as conn:
            row, session, now = self._session_in(conn, session_id)
            changed = edited_hardware(session, edit.code, edit.value, offered=_offered(row),
                                      expected_sequence=edit.expected_sequence, now=now)
            self._write_session_in(conn, changed)
        return PictureSequence(sequence=changed.sequence)

    def edit_software(self, session_id: UUID, edit: SoftwareEdit) -> PictureSequence:
        with self._db.transaction() as conn:
            _row, session, now = self._session_in(conn, session_id)
            changed = edited_software(session, edit.setting, edit.value, expected_sequence=edit.expected_sequence,
                                      now=now)
            self._write_session_in(conn, changed)
        return PictureSequence(sequence=changed.sequence)

    def edit_light(self, session_id: UUID, edit: LightEdit) -> PictureSequence:
        """The Display's light, live on the Display. unknown_session, session_ended, stale_sequence."""
        with self._db.transaction() as conn:
            _row, session, now = self._session_in(conn, session_id)
            changed = edited_light(session, DisplayLight(matched_lux=edit.matched_lux, black=edit.black),
                                   expected_sequence=edit.expected_sequence, now=now)
            self._write_session_in(conn, changed)
        return PictureSequence(sequence=changed.sequence)

    def edit_signal_range(self, session_id: UUID, edit: SignalRangeEdit) -> PictureSequence:
        with self._db.transaction() as conn:
            _row, session, now = self._session_in(conn, session_id)
            changed = edited_signal_range(session, edit.signal_range, expected_sequence=edit.expected_sequence,
                                          now=now)
            self._write_session_in(conn, changed)
        return PictureSequence(sequence=changed.sequence)

    def edit_test_pattern(self, session_id: UUID, edit: TestPatternEdit) -> PictureSequence:
        with self._db.transaction() as conn:
            _row, session, now = self._session_in(conn, session_id)
            changed = edited_test_pattern(session, edit.test_pattern, expected_sequence=edit.expected_sequence,
                                          now=now)
            self._write_session_in(conn, changed)
        return PictureSequence(sequence=changed.sequence)

    def keep_picture_alive(self, session_id: UUID) -> PictureSessionView:
        with self._db.transaction() as conn:
            _row, session, now = self._session_in(conn, session_id)
            kept = kept_alive(session, now)
            conn.execute("UPDATE picture_sessions SET idle_until=%s WHERE id=%s",
                         (kept.lease.idle_until, session_id))
        return _session_view(kept)

    def picture_done(self, session_id: UUID) -> None:
        with self._db.transaction() as conn:
            row, session, now = self._session_in(conn, session_id)
            _require_live(session, now)
            if not presented(session.draft, _stored_picture_document(row), _stored_picture_report(row)):
                raise PictureRefused("not_presented_yet", 409)
            self._end_in(conn, session)

    def revert_picture(self, session_id: UUID) -> None:
        """End the session: the stored values stay, and the display is put back once
        (`picture.restore_of`), until the Pi answers it (`record_picture_report`)."""
        with self._db.transaction() as conn:
            row, session, now = self._session_in(conn, session_id)
            _require_live(session, now)
            _fold_session_in(conn, stored_picture(row), session)
            notify_nodes_in(conn, _NODES_SEEING, (session.display_id,))

    def restore_picture(self, session_id: UUID, request: PictureRestore) -> PictureRestoreResult:
        """`PictureCommands.restore_picture`: the entry through picture_history_store
        `picture_entry_in`, the rule `picture.restored` (offered: `_offered(row)`; light_recorded:
        the entry's), the session written as an edit's (`_write_session_in`, with
        `restored_from`)."""
        with self._db.transaction() as conn:
            row, session, now = self._session_in(conn, session_id)
            entry = picture_entry_in(conn, request.entry_id)
            if entry is None:
                raise DisplayRefused("unknown_entry", 404)
            result = restored(session, entry_id=entry.entry_id, entry_display_id=entry.display_id,
                              picture=entry.record.picture, light_recorded=entry.record.light_recorded,
                              offered=_offered(row), expected_sequence=request.expected_sequence, now=now)
            self._write_session_in(conn, result.session)
        return PictureRestoreResult(sequence=result.session.sequence, hardware_skipped=result.hardware_skipped,
                                    light_kept=result.light_kept)

    def copy_picture(self, frame_id: str, request: PictureCopyRequest) -> tuple[PictureCopyResult, ...]:
        with self._db.transaction() as conn:
            frames = {row["frame_id"]: row for row in conn.execute(
                _PICTURE_FRAME + " WHERE f.id=ANY(%s)", ([frame_id, *request.to_frames],)).fetchall()}
            if frame_id not in frames or any(target not in frames for target in request.to_frames):
                raise DisplayRefused("unknown_frame", 404)
            source = frames[frame_id]
            if source["display_id"] is None:
                raise DisplayRefused("no_display", 409)
            values, source_offered = stored_picture(source), _offered(source)
            now = _CLOCK.now_in(conn)
            outcomes = []
            for target in request.to_frames:
                display_id = frames[target]["display_id"]
                skipped: tuple[str, ...] = ()
                if display_id is None:
                    outcome = CopyOutcome.NO_DISPLAY
                elif display_id == source["display_id"]:
                    outcome = CopyOutcome.SAME_DISPLAY
                elif (stored := _fold_in(conn, display_id, now)) is None:
                    outcome = CopyOutcome.SESSION_OPEN
                else:
                    result, skipped = copied(values, stored, source_offered=source_offered,
                                             target_offered=_offered(frames[target]))
                    write_picture(conn, display_id, copied_record(result))
                    notify_nodes_in(conn, _NODES_SEEING, (display_id,))
                    outcome = CopyOutcome.COPIED
                outcomes.append(PictureCopyResult(frame_id=target, outcome=outcome, hardware_skipped=skipped))
        return tuple(outcomes)

    @staticmethod
    def _session_in(conn: Any, session_id: UUID) -> tuple[Mapping[str, Any], PictureSession, float]:
        """The session's row (its Display locked) and rules' view; unknown_session when there is none."""
        row = conn.execute(_PICTURE_SESSION, (session_id,)).fetchone()
        if row is None:
            raise DisplayRefused("unknown_session", 404)
        session = picture_session(row)
        assert session is not None
        return row, session, _CLOCK.now_in(conn)

    @staticmethod
    def _write_session_in(conn: Any, session: PictureSession) -> None:
        pattern = session.test_pattern
        conn.execute("UPDATE picture_sessions SET sequence=%s, draft=%s, touched_hardware=%s, touched_software=%s, "
                     "idle_until=%s, test_pattern=%s, test_pattern_ask=%s, restored_from=%s WHERE id=%s",
                     (session.sequence, Jsonb(picture_fields(session.draft)), sorted(session.touched_hardware),
                      sorted(setting.value for setting in session.touched_software), session.lease.idle_until,
                      None if pattern is None else pattern.pattern.value, None if pattern is None else pattern.ask,
                      session.restored_from, session.session_id))
        notify_nodes_in(conn, _NODES_SEEING, (session.display_id,))

    @staticmethod
    def _end_in(conn: Any, session: PictureSession) -> None:
        """Done: store the session's draft on the Display (its kept-done entry beside it) and end the
        session, in one transaction."""
        write_picture(conn, session.display_id, done_record(session))
        conn.execute("DELETE FROM picture_sessions WHERE id=%s", (session.session_id,))
        notify_nodes_in(conn, _NODES_SEEING, (session.display_id,))


def _fold_session_in(conn: Any, stored: DisplayPicture, session: PictureSession) -> dict[str, int]:
    """End `session` without Done: its one-time instruction (`picture.restore_of`) joins the
    Display's pending one, the stored values unchanged, and the session row goes. The instruction."""
    restore = restore_of(stored, session)
    if restore:
        conn.execute("UPDATE displays SET hardware_restore = hardware_restore || %s WHERE id=%s",
                     (Jsonb(restore), session.display_id))
    conn.execute("DELETE FROM picture_sessions WHERE id=%s", (session.session_id,))
    return restore


def _fold_in(conn: Any, display_id: UUID, now: float) -> DisplayPicture | None:
    """The Display's stored picture with its row locked, after folding a session whose lease ran out
    (`_fold_session_in`: the stored values unchanged); None while a live session holds it."""
    row = conn.execute(f"SELECT {PICTURE_COLUMNS}, {_SESSION_COLUMNS} FROM displays d "  # noqa: S608
                       "LEFT JOIN picture_sessions ps ON ps.display_id=d.id WHERE d.id=%s FOR NO KEY UPDATE OF d",
                       (display_id,)).fetchone()
    stored, session = stored_picture(row), picture_session(row)
    if session is None:
        return stored
    if session.lease.live(now):
        return None
    _fold_session_in(conn, stored, session)
    return stored


def _require_live(session: PictureSession, now: float) -> None:
    if not session.lease.live(now):
        raise PictureRefused("session_ended", 410)


def _session_view(session: PictureSession) -> PictureSessionView:
    return PictureSessionView(session_id=session.session_id, sequence=session.sequence,
                              idle_until=session.lease.idle_until)


__all__ = ["DOCUMENT_STREAM", "OUTPUT_DOCUMENTS_CHANNEL", "POSITION_COLUMNS", "POSITION_FACTS", "REPORT_STREAM", "SEEN_DISPLAY_JOIN",
           "DisplayDocuments", "DisplayWakes", "FramePowerFacts", "OutputReportJudge", "PgDisplayCommands", "PgDisplayQueries",
           "PgPictureSessions",
           "commit_position", "frame_position", "frame_readiness", "frame_readiness_in",
           "output_documents",
           "power_facts_in", "record_picture_report", "record_report", "resolve_output", "write_picture"]
