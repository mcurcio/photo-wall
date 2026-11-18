"""A Frame's Position trial on the Node bus's display line (decision 0034, S4b part 3), as Central's
worker meets it: a bound Frame whose Pi is linked to Central's hub, whose Output reports connected
and whose Player's `frames` report shows it (`bound_node`), the trial document the worker's source
puts on the Output's `trial-<port>` key (`trial_document_in`), and the Pi's statement of what it
presents, through Central's real `TrialReportJudge` (`present_in`), each in the caller's
transaction or its own, as the hub's link store commits them. Every time is the registry's manual
clock (`ProcessTransactionClock`), so a test moves a lease by advancing it.
"""
from __future__ import annotations

import itertools

from test_registry import enroll, frame

from central.content_catalog.catalog import device_id_for_serial
from central.db import ProcessTransactionClock
from central.fleet.node_bus_presence import record_presence_in
from central.fleet.node_calibration import NodeCalibration, TrialReportJudge
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.display_store import REPORT_STREAM, output_documents
from central.infra.frame_lighting_store import PgFrameLighting
from contracts.node_calibration import (
    PresentedTrial,
    TrialDocument,
    TrialReport,
    decode_trial_document,
    encode_trial_report,
    trial_document_key,
    trial_report_key,
)
from contracts.node_state import (
    FRAMES_KEY,
    PLAYER_LINE,
    FrameReport,
    FrameReports,
    encode_frame_reports,
)
from nodeapi.buffers import state_prefix, state_stream
from nodeapi.epoch import Token
from nodeapi.pull import Read
from support.devices import seed_device
from support.position import PORTRAIT_ON_WIDE, save_position

SERIAL = "abcdef1234567890"                     # the Pi's box, as the fleet tests' rig names it
DEVICE = device_id_for_serial(SERIAL)
FRAME = "node-f0"
OUTPUT = "HDMI-A-1"
_SEQ = itertools.count(1)


def trials_of(registry, **lease) -> NodeCalibration:
    """The trial owner over the registry's database and manual clock (the routes' and the worker's
    `NodeCalibration`); `lease`: inactivity_seconds, hard_seconds."""
    return NodeCalibration(registry.db, registry=registry, lighting=PgFrameLighting(LUT_RECIPE_ID),
                           times=ProcessTransactionClock(registry.clock), **lease)


def bound_node(registry, *, device_id: str = DEVICE, frames: tuple[str, ...] = (FRAME,)) -> dict:
    """An enrolled Pi with each of `frames` bound to HDMI-A-1, HDMI-A-2, … and its Position saved,
    its Outputs reporting connected, its fleet link held by Central's hub, and its Player drawing
    every Frame at its binding (the `frames` report on the Player line). The Player's identity."""
    player, _, _ = enroll(registry, count=max(2, len(frames)), device_id=device_id)
    for index, frame_id in enumerate(frames):
        frame(registry, frame_id)
        registry.bind(frame_id, player["player_id"], f"HDMI-A-{index + 1}", expected_generation=0, by="console")
        save_position(registry, frame_id, PORTRAIT_ON_WIDE)
    with registry.db.transaction() as conn:
        seed_device(conn, device_id, SERIAL if device_id == DEVICE else None)
        record_presence_in(conn, {device_id: True}, registry.clock.utc())
    draw(registry, device_id)
    return player


def draw(registry, device_id: str = DEVICE, frames: tuple[str, ...] | None = None) -> None:
    """The Player's `frames` report: each of `frames` (default every Frame bound to the Pi's Player)
    at its binding's generation, as Central's link records it (`node_states`)."""
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT f.id, f.generation, f.output_id FROM frames f JOIN players p ON p.id=f.player_id "
                            "WHERE p.device_id=%s ORDER BY f.id", (device_id,)).fetchall()
        reports = FrameReports(frames=tuple(
            FrameReport(frame_id=row["id"], generation=row["generation"], output_id=row["output_id"], showing=True,
                        light_lux=None)
            for row in rows if frames is None or row["id"] in frames))
        conn.execute(
            "INSERT INTO node_states(device_id, stream, subject, epoch, seq, removed, headers, data, recorded_at) "
            "VALUES (%s, %s, %s, 'epoch', %s, false, '{}', %s, %s) ON CONFLICT (device_id, stream, subject) DO UPDATE "
            "SET seq=EXCLUDED.seq, data=EXCLUDED.data, recorded_at=EXCLUDED.recorded_at",
            (device_id, state_stream(PLAYER_LINE), state_prefix(PLAYER_LINE) + FRAMES_KEY, next(_SEQ),
             encode_frame_reports(reports), registry.clock.utc()))


def documents_in(conn, trials: NodeCalibration, device_id: str = DEVICE) -> dict[str, bytes]:
    """Every document the worker's source serves the Pi now, on the trials' clock."""
    return output_documents(conn, device_id, trials.times.now_in(conn), trials=trials)


def trial_document_in(conn, trials: NodeCalibration, device_id: str = DEVICE,
                      output_id: str = OUTPUT) -> TrialDocument:
    """The Output's trial document as the worker's source serves it now."""
    return decode_trial_document(documents_in(conn, trials, device_id)[trial_document_key(output_id)])


def trial_document(registry, trials: NodeCalibration, device_id: str = DEVICE, output_id: str = OUTPUT) -> TrialDocument:
    """`trial_document_in` in a transaction of its own."""
    with registry.db.transaction() as conn:
        return trial_document_in(conn, trials, device_id, output_id)


def present_in(conn, device_id: str, report: TrialReport, *, registry) -> None:
    """The Pi's `trial-<port>` statement as the hub's link store records it, through Central's
    `TrialReportJudge`."""
    read = Read(Token("0123456789abcdef", next(_SEQ)), "$KV.state_display." + trial_report_key(report.output_id), {},
                encode_trial_report(report))
    TrialReportJudge(times=ProcessTransactionClock(registry.clock)).judge_in(conn, device_id, REPORT_STREAM, (read,))


def present(registry, document: TrialDocument, device_id: str = DEVICE) -> None:
    """The Pi presents the document's trial (or states none when it carries none)."""
    trial = document.trial
    presented = None if trial is None else PresentedTrial(trial.trial_id, trial.generation, trial.sequence,
                                                          trial.candidate_sha256)
    with registry.db.transaction() as conn:
        present_in(conn, device_id, TrialReport(document.output_id, presented), registry=registry)


__all__ = ["DEVICE", "FRAME", "OUTPUT", "SERIAL", "bound_node", "documents_in", "draw", "present", "present_in",
           "trial_document", "trial_document_in", "trials_of"]


def worker_trials(db) -> NodeCalibration:
    """The trial projection as the worker's wiring builds it (central/node_bus_wiring.py): over
    `db`, on the database's clock."""
    from central.registry import Registry
    from contracts.time import SystemClock

    lighting = PgFrameLighting(LUT_RECIPE_ID)
    return NodeCalibration(db, registry=Registry(db, SystemClock(), lighting=lighting), lighting=lighting)
