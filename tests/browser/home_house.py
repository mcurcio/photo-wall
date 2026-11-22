"""A house for Home's browser tests (decision 0036): Pis, Frames and the facts a Frame's status is
composed from, each recorded through the store its owner records it with, never a raw status row.

- A Pi (`House.pi`) is a seeded device with an enrolled Player, its Frames bound and positioned,
  its session socket open (Central hears it), its fleet link held by Central's hub, its host
  line reporting and a Scene shown now on its Frames (`plays`: a picture is to play on each). Central clears every open socket when it starts (player_contact_store.py), so a
  house is furnished after the server is up (`home_server`).
- What the Pi says goes through Central's real link store: the Player line's Frame reports
  (`shows`), the health line's verdict (`verdict`), the host line's metrics (`host_reports`), the
  display line's Output report (`display_answers`).
- What Central knows by itself goes through its owner: the hub's reachability and the worker's
  heartbeat (`hub`), the Player's contact (`silent`), a power hold (`all_off`), a library
  connection's contact (`library_unreachable`), a reboot's ask (`reboot_asked`).

Central's clock is the registry's manual clock, set to the wall clock when the house is made: the
harness's Central stamps some facts with the database's clock (a recorded line, a link action) and
judges them against its own, so the two must be one timeline, as they are when deployed. A test
therefore never asserts a time of day, only that one is said.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from operator_harness import operator_server
from support.devices import seed_device
from support.frames import frame_create
from support.house_time import UTC_HOUSE, set_house
from support.photo_pools import fixture_connection, library_of, seed_source
from support.pi_reboots import RecordingReboot, reboot_queue, run_queued
from support.position import save_position
from support.trial_bus import worker_trials
from test_display_identity import NO_SERIAL
from test_registry import enroll, hello

from central.content_catalog.catalog import device_id_for_serial
from central.coordination import Coordinator, OutputLossLedger
from central.db import DatabaseTransactionClock, ProcessTransactionClock
from central.fleet.app_restart import RestartAppHandler
from central.fleet.node_bus_presence import record_hub_reachable_in, record_presence_in
from central.fleet.pi_reboot import REBOOT_ASKED_ACTION
from central.infra.display_store import (
    DOCUMENT_STREAM,
    REPORT_STREAM,
    DisplayDocuments,
    DisplayWakes,
    OutputReportJudge,
)
from central.infra.events import link_action_in
from central.infra.health_report_store import HEALTH_REPORT_SUBJECT, HEALTH_STATE_STREAM
from central.infra.job_queue import pending_in
from central.infra.node_link_store import PgLinkStores
from central.infra.player_contact_store import closed_in, opened_in
from central.infra.power_store import PgPower
from central.infra.transactions import PgTransactions
from central.kernel.job_types import RestartApp
from central.photo_library.model import ContactRefused, SourceQuery, Unreachable
from central.runtime import Contribution, Scene
from contracts.node_apps import (
    APP_RESTART_KEY,
    APP_RESTART_RECORD,
    APPS_LINE,
    AppRestartOutcome,
    AppRestartState,
    AppRestartStep,
    encode_app_restart_state,
)
from contracts.node_faults import catalogue_digest
from contracts.node_health import HealthReport, OutputHealth, encode_health_report
from contracts.node_link import Pipe
from contracts.node_observation import HostMetricsState, HostMetricV2, encode_host_metrics_state
from contracts.node_output import (
    InForce,
    InForceKind,
    OutputReport,
    Power,
    PowerMethod,
    PowerResult,
    decode_output_document,
    encode_output_report,
    output_document_key,
    output_report_key,
)
from contracts.node_state import (
    BIRTH_KEY,
    FRAMES_KEY,
    HOST_LINE,
    HOST_METRICS_KEY,
    PLAYER_LINE,
    Birth,
    FrameReport,
    FrameReports,
    encode_birth,
    encode_frame_reports,
)
from nodeapi.buffers import state_prefix, state_stream
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

_EPOCH = "0123456789abcdef"
_SEQ = itertools.count(1)
_RESTART_RECORDS = "RECORD_apps"
# The Pi's own clock in a verdict (`OutputHealth.since`): evidence for Details, never Central's "since".
PI_SINCE = "2026-11-22T08:10:00Z"


def off_thread(call, *args, **kwargs):
    """Run `call` off Playwright's thread, whose event loop is running (a link store's commit runs
    its own loop)."""
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(call, *args, **kwargs).result(30)


class RecordingRestarter:
    """The Pi's app broker as the worker's `RestartApp` job meets it (`AppRestarter`): it records
    each ask and answers `accepted`."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def restart(self, device_id: str, request_id: str) -> AppRestartOutcome | None:
        self.asked.append((device_id, request_id))
        return AppRestartOutcome.ACCEPTED


@dataclass(frozen=True)
class Pi:
    """One Pi of the house: its box, its Player's identity and its Frames by HDMI port."""
    serial: str
    device_id: str
    player: dict
    frames: dict[str, str]          # output id -> Frame id

    @property
    def handle(self) -> str:
        """The Pi as the console names it: "Pi <last six of its serial>" (health.js `playerHandle`)."""
        return f"Pi {self.serial[-6:]}"

    def output_of(self, frame_id: str) -> str:
        return next(output for output, each in self.frames.items() if each == frame_id)


class House:
    """The house over `registry`. Every method records one fact as its owner does."""

    def __init__(self, registry) -> None:
        self.registry, self.db = registry, registry.db
        registry.clock.wall = float(int(time.time()))        # one timeline with the database's clock
        self.times = ProcessTransactionClock(registry.clock)
        set_house(self.db, UTC_HOUSE)
        reboot_queue(self.db)                                # the reboot and restart routes queue a job
        self.broker = RecordingRestarter()
        self._x = 0.0

    # --- Equipment -------------------------------------------------------------------------------

    def pi(self, serial: str, *frames: tuple[str, str]) -> Pi:
        """A Pi whose box is `serial`, feeding each of `frames` (id, name) from HDMI-A-1, HDMI-A-2:
        bound, positioned, heard by Central and linked to its hub."""
        device_id = device_id_for_serial(serial)
        with self.db.transaction() as conn:
            seed_device(conn, device_id, serial)
        player = enroll(self.registry, device_id=device_id, count=2)[0]
        hello(self.registry, player)
        bound = {}
        for index, (frame_id, name) in enumerate(frames):
            output_id = f"HDMI-A-{index + 1}"
            self.frame(frame_id, name)
            self.registry.bind(frame_id, player["player_id"], output_id, expected_generation=0, by="console")
            save_position(self.registry, frame_id)
            bound[output_id] = frame_id
        with self.db.transaction() as conn:
            opened_in(conn, player["player_id"], player["authority_epoch"], self.registry.clock.utc())
            record_presence_in(conn, {device_id: True}, self.registry.clock.utc())
        pi = Pi(serial, device_id, player, bound)
        self.host_reports(pi)
        if bound:
            self.plays(f"daily-{serial[-6:]}", *bound.values())
        return pi

    def plays(self, scene_id: str, *frame_ids: str) -> None:
        """A Scene shown now on `frame_ids`: a picture is to play on each (the Runtime's own fact;
        without it a Frame with no picture is dark on purpose, so Resting). Nothing plans it here
        (the harness runs no scheduler), so its Photo source is only a name."""
        runtime = Coordinator(self.db, self.registry.clock).runtime
        runtime.command("set_scene", Scene(scene_id=scene_id, loop=True, cycle_seconds=60, contributions=tuple(
            Contribution(target=f"frame:{frame_id}", kind="media", source_refs=("daily:1",))
            for frame_id in frame_ids)), by="console")
        runtime.command("activate", scene_id, f"now-{scene_id}", self.registry.clock.utc(), by="console")

    def position(self, frame_id: str) -> None:
        """The Frame's Position checked (its last setup step)."""
        save_position(self.registry, frame_id)

    def feeds(self, pi: Pi, frame_id: str, output_id: str = "HDMI-A-1") -> Pi:
        """`pi` as feeding `frame_id` too (bound by the test itself, so no Scene shows on it)."""
        return Pi(pi.serial, pi.device_id, pi.player, {**pi.frames, output_id: frame_id})

    def frame(self, frame_id: str, name: str) -> None:
        """A Frame on the wall, beside the last one; unbound until a Pi feeds it."""
        self.registry.create_frame(frame_create(frame_id, name=name, x_mm=self._x), by="console")
        self._x += 800.0

    # --- Central's own facts -----------------------------------------------------------------------

    def hub(self, reachable: bool = True) -> None:
        """Central's hub as FleetHub's last look found it, with the worker (whose task looks) running."""
        with self.db.transaction() as conn:
            record_hub_reachable_in(conn, reachable, times=self.times)
            conn.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now())")

    def silent(self, pi: Pi) -> None:
        """The Pi's session socket closed now: past the silence limit Central has not heard it."""
        with self.db.transaction() as conn:
            closed_in(conn, pi.player["player_id"], pi.player["authority_epoch"], self.registry.clock.utc())

    def unlinked(self, pi: Pi) -> None:
        """Central's hub no longer holds the Pi's link."""
        with self.db.transaction() as conn:
            record_presence_in(conn, {pi.device_id: False}, self.registry.clock.utc())

    def all_off(self) -> UUID:
        """All off: a power hold on every Frame there is now."""
        hold_id = uuid4()
        PgPower(self.db, times=self.times).all_off(hold_id, by="console")
        return hold_id

    def resume(self, *frame_ids: str) -> None:
        """Resume schedule on `frame_ids`: their holds end."""
        PgPower(self.db, times=self.times).resume_schedule(tuple(frame_ids))

    def reboot_asked(self, pi: Pi) -> None:
        """A reboot of the Pi asked and not yet run, as central/fleet/pi_reboot.py records the ask."""
        with self.db.transaction() as conn:
            link_action_in(conn, pi.device_id, Pipe.FLEET, REBOOT_ASKED_ACTION,
                           {"request_id": str(uuid4()), "reason": "console/home", "by": "console"})

    def scene_reading(self, scene_id: str, source: str, *frame_ids: str) -> str:
        """A Scene of the fixture library connection's Photo source `source`, shown now on
        `frame_ids`. The connection's ref."""
        connection = fixture_connection(self.registry)
        with self.db.transaction() as conn:
            seed_source(conn, source, SourceQuery(connection_ref=connection))
        coordinator = Coordinator(self.db, self.registry.clock)
        coordinator.runtime.command("set_scene", Scene(scene_id=scene_id, loop=True, cycle_seconds=60, contributions=tuple(
            Contribution(target=f"frame:{frame_id}", kind="media", source_refs=(f"{source}:1",))
            for frame_id in frame_ids)), by="console")
        coordinator.runtime.command("activate", scene_id, f"now-{scene_id}", self.registry.clock.utc(), by="console")
        return connection

    def library_unreachable(self, connection: str) -> None:
        """The library connection's newest contact was refused: Immich did not answer."""
        library = library_of(self.registry)
        assert library.record_contact(library.begin_contact(connection), ContactRefused(problem=Unreachable()))

    # --- What a Pi says, through Central's link store ------------------------------------------------

    def _commit(self, pi: Pi, pipe: Pipe, stream: str, subject: str, value: bytes, judges=None) -> None:
        token = Token(_EPOCH, next(_SEQ))
        stores = PgLinkStores(self.db, judges or {})
        off_thread(asyncio.run, stores.store(pi.serial, pipe).commit(
            stream, Batch((Read(token, subject, {}, value),), token, False)))

    def shows(self, pi: Pi, **showing: bool) -> None:
        """The Player's Frame reports: each of the Pi's Frames presenting its picture, but those
        named False (`shows(pi, hall=False)`)."""
        with self.db.transaction() as conn:
            generations = {row["id"]: row["generation"] for row in conn.execute(
                "SELECT id, generation FROM frames WHERE id = ANY(%s)", (list(pi.frames.values()),)).fetchall()}
        reports = FrameReports(frames=tuple(
            FrameReport(frame_id=frame_id, generation=generations[frame_id], output_id=output_id,
                        showing=showing.get(frame_id, True), light_lux=None)
            for output_id, frame_id in pi.frames.items()))
        self._commit(pi, Pipe.SHOW, state_stream(PLAYER_LINE), state_prefix(PLAYER_LINE) + FRAMES_KEY,
                     encode_frame_reports(reports))

    def verdict(self, pi: Pi, **dark: str) -> None:
        """The Pi's health verdict: each named Frame's Output dark with that code
        (`verdict(pi, hall="app_absent")`), the rest not dark."""
        outputs = tuple(OutputHealth(output_id, frame_id in dark, dark.get(frame_id), PI_SINCE if frame_id in dark else None)
                        for output_id, frame_id in pi.frames.items())
        self._commit(pi, Pipe.FLEET, HEALTH_STATE_STREAM, HEALTH_REPORT_SUBJECT,
                     encode_health_report(HealthReport(catalogue_digest(), outputs)))

    def host_reports(self, pi: Pi, *, throttled: bool = False) -> None:
        """The Pi's host metrics on its host line: cool and at full speed, or `throttled` now."""
        state = HostMetricsState((HostMetricV2("throttled_now", int(throttled), "boolean"),
                                  HostMetricV2("soc_temperature", 91 if throttled else 48, "celsius")))
        self._commit(pi, Pipe.FLEET, state_stream(HOST_LINE), state_prefix(HOST_LINE) + HOST_METRICS_KEY,
                     encode_host_metrics_state(state))

    def display_answers(self, pi: Pi, frame_id: str, result: PowerResult, power: Power) -> None:
        """The Pi's Output report for the Frame's standing Output document: what it carries out
        (`power`, by the timetable the document holds) and how the display answered (`result`)."""
        output_id = pi.output_of(frame_id)
        source = DisplayDocuments(PgLinkStores(self.db), DisplayWakes(), times=DatabaseTransactionClock(),
                                  trials=worker_trials(self.db)).source(pi.serial, Pipe.FLEET)
        document = decode_output_document(
            off_thread(asyncio.run, source.documents(DOCUMENT_STREAM))[output_document_key(output_id)])
        report = OutputReport(output_id, True, NO_SERIAL, (), (PowerMethod.DDC_CI,), PowerMethod.DDC_CI, document.change,
                              result, InForce(InForceKind.TIMETABLE, power, None, None, None), None,
                              current_mode=(1920, 1080))
        self._commit(pi, Pipe.FLEET, REPORT_STREAM, "$KV.state_display." + output_report_key(output_id),
                     encode_output_report(report),
                     {REPORT_STREAM: OutputReportJudge(DisplayWakes(), losses=OutputLossLedger())})

    # --- Reboot the Pi against the fake Pi --------------------------------------------------------------

    def run_reboot(self, pi: Pi, *, delivered: bool = True) -> str:
        """The worker runs the Pi's one queued `RebootPi` with the real handler (the Pi's host
        answers `delivered`), then finishes the job. The request's id."""
        return run_queued(self.db, pi.device_id, delivered=delivered, pi=RecordingReboot()).request_id

    def host_starts(self, pi: Pi) -> None:
        """The Pi's host states its birth on its host line: it started (again)."""
        birth = Birth(component="host", version="1.0.0", pipe="fleet", release_digest="release",
                      schema_majors={"host": 1}, slice_digest="0" * 64)
        self._commit(pi, Pipe.FLEET, state_stream(HOST_LINE), state_prefix(HOST_LINE) + BIRTH_KEY, encode_birth(birth))

    # --- Restart app against the fake Pi ---------------------------------------------------------------

    def run_restart(self, pi: Pi) -> str:
        """The worker runs the Pi's one queued `RestartApp` with the real handler (the broker
        answers `accepted`), then finishes the job. The request's id."""
        with self.db.transaction() as conn:
            [(job, status)] = pending_in(conn, RestartApp(request_id="x", device_id=pi.device_id, by="central"))
        assert status == "todo"
        off_thread(asyncio.run, RestartAppHandler(self.broker, PgTransactions(self.db)).handle(job))
        with self.db.transaction() as conn:
            conn.execute("UPDATE procrastinate_jobs SET status='succeeded' WHERE status='todo' "
                         "AND args->>'request_id'=%s", (job.request_id,))
        return job.request_id

    def broker_states(self, pi: Pi, request_id: str, step: AppRestartStep) -> None:
        """The broker's step of the restart as its line carries it: a record and the newest state."""
        value = encode_app_restart_state(AppRestartState(request_id=request_id, step=step))
        self._commit(pi, Pipe.FLEET, _RESTART_RECORDS, f"{APPS_LINE}.{APP_RESTART_RECORD}", value)
        self._commit(pi, Pipe.FLEET, state_stream(APPS_LINE), state_prefix(APPS_LINE) + APP_RESTART_KEY, value)


@contextmanager
def home_server(registry):
    """Central on the browser harness with a `House` to furnish: yields `(origin, house)`. The
    server is up first, so a Pi made in the block keeps the open socket Central hears it by."""
    house = House(registry)
    with operator_server(registry.db, registry.clock) as origin:
        yield origin, house
