"""A Frame's status over PostgreSQL (decision 0036): every input read in one transaction through
its owner's own read, the `frame_status` table, and Home's document.

- `PgStatusReading` is central/frame_status/ports.py `StatusReading` over one connection at one
  `now`: each port's answer comes from the read its owner already has (named on each method), so
  every answer is of the same committed state. It judges nothing: an owner's rule is called, never
  restated (the two exceptions are plain table look-ups, said where they are).
- `PgFrameStatusLog` is the one repository of `frame_status` (migration 132) and the writer of its
  records in Central's event log (central/infra/events.py, kind 'frame-status').
- `PgFrameStatus` composes: `record` is the scheduler's `status` step, the table's one writer, and
  writes only the Frames whose key changed (decision 0033); `home` is Home's read and `frame` the
  Frame page's, each one read-only snapshot that never writes.

`now` is Central's clock (`contracts.time.Clock`), the clock every `since` is on. Many of the
facts are stamped by the database's clock (`TransactionClock`: a recorded line, an event, a hold,
a singleton's change). Clocks compare only to themselves, so the reading asks the database's clock
for its own now once, judges those facts against that (the power timetable and the holds among
them), and hands the policy each such time at its age before Central's `now` (`_central`; where
the two clocks are one wall clock, as deployed, that is the time as stamped). Audit
times are the database's.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from functools import cached_property
from typing import Any, Final, Protocol

from central.actor import Actor
from central.central_errors.compose import impediment
from central.central_errors.views import FrameImpediment
from central.db import Database, TransactionClock
from central.displays.model import Readiness, live_test
from central.everyday.ports import FrameFacts as EverydayFacts
from central.everyday.ports import Showing
from central.fleet.node_bus_presence import HUB_REACHABILITY
from central.fleet.node_observations import fleet_hosts_in, host_births_in
from central.fleet.pi_reboot import newest_reboots_in
from central.frame_status.model import (
    FrameStatus,
    Override,
    OverrideKind,
    ProblemCode,
    Row,
    SetupStep,
    StatusWord,
)
from central.frame_status.policy import (
    REBOOT_ALLOWED_SECONDS,
    everyday_facts,
    frame_status,
    house_status,
)
from central.frame_status.ports import (
    Dark,
    DisplayPower,
    FailingStep,
    FrameStatusLog,
    Hub,
    KeptStatus,
    OnTheWay,
    Picture,
    PiHealth,
    PiHearing,
    PlanningTrouble,
    Preparation,
    Reboot,
    SourceTrouble,
    StatusChange,
    StatusKey,
    UnfilledNow,
    facts_of,
    key_of,
)
from central.frame_status.views import (
    FramePlace,
    FrameStatusView,
    HomeView,
    NowNext,
    Playing,
    StatusChangeView,
    Upcoming,
    WallView,
    home_view,
    wall_can,
)
from central.infra.central_errors_store import impeded_reports_in
from central.infra.display_store import power_facts_of_frames_in
from central.infra.events import frame_status_in, frame_statuses_in
from central.infra.everyday_store import showing_in
from central.infra.frame_report_store import frame_reports_in
from central.infra.photo_library_store import PgPhotoLibrary
from central.infra.planning_outcome_store import PgPlanningOutcome
from central.infra.player_contact_store import player_reports_in
from central.infra.player_versions import installs_in
from central.infra.power_store import power_policy_in
from central.infra.transactions import PgTransaction, pg_connection
from central.installation_models import FrameInventory, InstallationInventory, PlayerInventory
from central.kernel.transactions import Transaction
from central.photo_library.model import NeedsALook
from central.power.model import PowerPolicy
from central.power.timetable import frame_timetable, hold_in_force
from central.runtime import OperatorProjection, Program, Runtime, Scene
from central.why.explain import content_why, power_why
from central.why.model import ContentRequest
from contracts.models import Layer
from contracts.node_observation import HOST_METRIC_BANDS
from contracts.node_output import Power
from contracts.time import Clock

_BY: Final[Actor] = "scheduler"       # the status step is the scheduler's
_TARGET: Final = "frame:"             # a planning issue's target and a Runtime target of a Frame
RECENT_CHANGES: Final = 32            # the Frame page's recent changes
# The host's flags that mean "throttled or under-voltage now": the rows of the host's own band
# table (contracts/node_observation.py HOST_METRIC_BANDS) that alarm on a flag. A look-up of that
# table, not a second rule.
_NOW_FLAGS: Final[Mapping[str, float]] = {band.name: band.alarm_at for band in HOST_METRIC_BANDS
                                         if band.unit == "boolean" and band.alarm_at is not None}


class UnknownFrame(LookupError):
    """No such Frame (the Frame page's read)."""


class InventoryReader(Protocol):
    """The Registry's inventory read (central/registry.py `Registry.inventory_in`)."""

    def __call__(self, conn: Any, now: float) -> InstallationInventory: ...


class PlanReader(Protocol):
    """Coordination's read of each Frame's plan (central/coordination.py `Coordinator.frame_plans_in`):
    Frame id -> an object with `unfilled`, `layers`, `prepared` and `failed`."""

    def __call__(self, conn: Any, now: float) -> Mapping[str, Any]: ...


class LayerReader(Protocol):
    """Coordination's read of each Frame's offered layers (central/coordination.py
    `Coordinator.frame_layers_in`)."""

    def __call__(self, conn: Any, now: float) -> Mapping[str, tuple[Layer, ...]]: ...


class PgStatusReading:
    """`StatusReading` over `conn` at `now`. Each owner's read runs once, when first asked."""

    def __init__(self, conn: Any, now: float, *, times: TransactionClock, inventory: InventoryReader,
                 plans: PlanReader, runtime: Callable[[Any], Runtime],
                 failing_steps: Callable[[], tuple[FailingStep, ...]],
                 layers: LayerReader | None = None) -> None:
        """`layers`: Coordination's `frame_layers_in`, read only when given (Home's and the Frame
        page's reads, and the everyday commands): without it nothing is offered (`showing`)."""
        self._conn, self._now, self._times = conn, now, times
        self._inventory_in, self._plans_in, self._runtime_in = inventory, plans, runtime
        self._failing_steps = failing_steps
        self._layers_in = layers

    @property
    def now(self) -> float:
        return self._now

    @cached_property
    def _db_now(self) -> float:
        """The database's clock at this reading: what a time it stamped is compared with."""
        return self._times.now_in(self._conn)

    @cached_property
    def _ahead(self) -> int:
        """How far Central's clock is ahead of the database's, in whole seconds: 0 wherever the two
        are one wall clock, so a time is then served exactly as stamped and a document that did
        not change reads the same twice (Home's change number)."""
        return round(self._now - self._db_now)

    def _central(self, at: float | None) -> float | None:
        """A time the database's clock stamped, at the same age (to the second) before Central's `now`."""
        return None if at is None else at + self._ahead

    # --- What several ports read ---------------------------------------------------------------

    @cached_property
    def inventory(self) -> InstallationInventory:
        """The Registry's inventory: every Frame with its readiness, binding and dark verdict."""
        return self._inventory_in(self._conn, self._now)

    @cached_property
    def _players(self) -> dict[str, PlayerInventory]:
        return {player.id: player for player in self.inventory.players if player.retired_at is None}

    @cached_property
    def bound(self) -> tuple[tuple[FrameInventory, PlayerInventory], ...]:
        """Each Frame bound to an enrolled Player, with that Player."""
        return tuple((frame, self._players[frame.player_id]) for frame in self.inventory.frames
                     if frame.player_id in self._players)

    @cached_property
    def _hosts(self) -> dict[str, dict]:
        """Every Pi's host read (central/fleet/node_observations.py `fleet_hosts_in`), by device id."""
        return {host["device_id"]: host for host in fleet_hosts_in(self._conn)["devices"]}

    @cached_property
    def runtime(self) -> Runtime:
        return self._runtime_in(self._conn)

    @cached_property
    def projection(self) -> OperatorProjection:
        return self.runtime.operator_projection(self._now)

    @cached_property
    def programs(self) -> dict[str, Program]:
        return {program_id: Program.model_validate(program)
                for program_id, program in self.runtime.export_state()["programs"].items()}

    @cached_property
    def _policy(self) -> PowerPolicy:
        return power_policy_in(self._conn, self._db_now)

    @cached_property
    def _plans(self) -> Mapping[str, Any]:
        return self._plans_in(self._conn, self._now)

    @cached_property
    def winners(self) -> dict[str, ContentRequest]:
        """The request that wins each Frame now (central/why/explain.py `content_why`)."""
        found = {}
        for frame in self.inventory.frames:
            winner = content_why(self.projection.current, frame.id, programs=self.programs, refused={}).winner
            if winner is not None:
                found[frame.id] = winner
        return found

    # --- The ports -----------------------------------------------------------------------------

    def readiness(self) -> Mapping[str, Readiness]:
        return {frame.id: frame.readiness for frame in self.inventory.frames}

    def pis(self) -> Mapping[str, str]:
        return {frame.id: player.device_id for frame, player in self.bound}

    def power(self) -> Mapping[str, DisplayPower]:
        """`power_why` (central/why/explain.py) of each bound Frame's timetable and power facts."""
        facts, house, found = power_facts_of_frames_in(self._conn), self.runtime.house, {}
        for frame in self.inventory.frames:
            held = facts.get(frame.id)
            if held is None or not held.bound:
                continue
            now = self._db_now       # the timetable, the holds and the test are the database's clock's
            timetable = frame_timetable(frame.id, self._policy, house, now)
            why = power_why(timetable=timetable, hold=hold_in_force(frame.id, self._policy, now),
                            test=held.test, document=held.document, report=held.report, house=house, now=now)
            test = live_test(held.test, now)
            due_on = (test.ends_at if test is not None and test.power is Power.OFF else
                      next((change.at for change in timetable.changes if change.at > now and change.power is Power.ON), None))
            found[frame.id] = DisplayPower(wanted=why.wanted, because=why.because, resting=why.resting,
                                           result=why.result, due_on_at=self._central(due_on))
        return found

    def hub(self) -> Hub:
        """The hub's state as presence judges it (central/fleet/node_bus_presence.py); `since` is
        when its reachability last changed, the one time presence keeps."""
        state = next((host["bus_link"]["hub"] for host in self._hosts.values()), None)
        if state is None:      # no Pi enrolled: no link to read the state beside
            return Hub("no_hub")
        stored = HUB_REACHABILITY.read_in(self._conn)
        return Hub(state, self._central(stored.changed_at) if state == "unreachable" and stored is not None else None)

    def hearing(self) -> Mapping[str, PiHearing]:
        heard = player_reports_in(self._conn, self._now).reports
        found = {}
        for frame, player in self.bound:
            link = self._hosts.get(player.device_id, {}).get("bus_link") or {}
            found[frame.id] = PiHearing(linked=link.get("linked"), linked_since=link.get("changed_at"),
                                        heard_at=heard.get(player.id), enrolled_at=player.last_seen)
        return found

    def dark(self) -> Mapping[str, Dark]:
        return {frame.id: Dark(code=frame.dark.code, pi_since=frame.dark.since)
                for frame in self.inventory.frames if frame.dark is not None}

    @cached_property
    def _reports(self) -> Mapping[str, Any]:
        return frame_reports_in(self._conn)

    def pictures(self) -> Mapping[str, Picture]:
        found = {}
        for frame, player in self.bound:
            recorded = self._reports.get(player.id)
            report = None if recorded is None else next(
                (each for each in recorded.reports.frames if (each.frame_id, each.generation, each.output_id)
                 == (frame.id, frame.generation, frame.output_id)), None)
            if report is not None:
                found[frame.id] = Picture(showing=report.showing, continuing_since=report.continuing_since,
                                          reported_at=self._central(recorded.recorded_at))
        return found

    def reports_at(self) -> Mapping[str, float]:
        return {frame.id: self._central(self._reports[player.id].recorded_at) for frame, player in self.bound
                if player.id in self._reports}

    def impediments(self) -> Mapping[str, FrameImpediment]:
        reports = impeded_reports_in(self._conn)
        return {frame.id: impediment(frame.id, frame.output_id, reports.get(player.device_id))
                for frame, player in self.bound if frame.output_id is not None}

    def on_the_way(self) -> Mapping[str, OnTheWay]:
        installs = installs_in(self._conn)
        reboots = newest_reboots_in(self._conn, self._db_now - REBOOT_ALLOWED_SECONDS)
        births = host_births_in(self._conn) if reboots else {}
        return {device_id: OnTheWay(
            install=(install := installs.get(device_id, (None, None)))[0], install_since=install[1],
            reboot=None if (reboot := reboots.get(device_id)) is None else Reboot(
                self._central(reboot.asked_at), self._central(reboot.answered_at), reboot.delivered),
            host_born_at=self._central(births.get(device_id))) for device_id in {*installs, *reboots}}

    def preparation(self) -> Mapping[str, Preparation]:
        return {frame_id: Preparation(planned=plan.layers, prepared=plan.prepared, failed=plan.failed)
                for frame_id, plan in self._plans.items()}

    def planning(self) -> Mapping[str | None, tuple[PlanningTrouble, ...]]:
        outcome = PgPlanningOutcome.read_in(self._conn)
        found: dict[str | None, list[PlanningTrouble]] = {}
        for issue in outcome.issues:
            named = issue.target is not None and issue.target.startswith(_TARGET)
            found.setdefault(issue.target.removeprefix(_TARGET) if named else None, []).append(
                PlanningTrouble(code=issue.code, scene_id=issue.scene_id, since=self._central(outcome.since)))
        return {frame_id: tuple(issues) for frame_id, issues in found.items()}

    def unfilled(self) -> Mapping[str, tuple[UnfilledNow, ...]]:
        return {frame_id: tuple(UnfilledNow(span.start, span.end) for span in plan.unfilled)
                for frame_id, plan in self._plans.items() if plan.unfilled}

    def wanted(self) -> frozenset[str]:
        """Each Frame whose visible Intent is media (central/runtime.py `RuntimeView.for_target`)."""
        return frozenset(frame.id for frame in self.inventory.frames
                         if (intent := self.projection.current.for_target(_TARGET + frame.id)) is not None
                         and intent.kind == "media")

    def playing_scenes(self) -> Mapping[str, str]:
        return {frame_id: winner.scene_id for frame_id, winner in self.winners.items()}

    @cached_property
    def _health(self) -> Mapping[str, Any]:
        """Every connection's health (central/infra/photo_library_store.py), by its ref."""
        return PgPhotoLibrary.connection_health_in(self._conn)

    @cached_property
    def _library(self) -> dict[str, SourceTrouble]:
        """Each connection that needs a look, by its ref."""
        return {ref: SourceTrouble(connection_ref=ref, problem=health.problem.kind, since=self._central(health.since),
                                   checked_at=self._central(health.checked_at))
                for ref, health in sorted(self._health.items()) if isinstance(health, NeedsALook)}

    def sources(self) -> Mapping[str, tuple[SourceTrouble, ...]]:
        """The Sources of each Frame's visible Intent (central/runtime.py `RuntimeView.for_target`),
        each one's connection, and that connection's health."""
        if not self._library:
            return {}
        refs = {frame.id: intent.source_refs for frame in self.inventory.frames
                if (intent := self.projection.current.for_target(_TARGET + frame.id)) is not None}
        connections = PgPhotoLibrary.source_connections_in(
            self._conn, {ref for frame_refs in refs.values() for ref in frame_refs})
        found = {}
        for frame_id, frame_refs in refs.items():
            troubled = sorted({connections[ref] for ref in frame_refs if connections.get(ref) in self._library})
            if troubled:
                found[frame_id] = tuple(self._library[ref] for ref in troubled)
        return found

    def libraries(self) -> tuple[tuple[SourceTrouble, ...], int]:
        return tuple(self._library.values()), len(self._health)

    def pi_health(self) -> Mapping[str, PiHealth]:
        """Each Pi's flags by the host's own bands (`_NOW_FLAGS`) and the kernel's verdict on its
        wall clock (`wall_clock`, contracts/node_host_facts.py). Nothing states "storage low" as a
        fact today (it is a boot stage's refusal), so it is never set here."""
        found = {}
        for device_id, host in self._hosts.items():
            metrics, facts = host.get("host") or {}, host.get("facts") or {}
            throttled = any(metric["unit"] == "boolean" and metric["name"] in _NOW_FLAGS
                            and metric["value"] >= _NOW_FLAGS[metric["name"]] for metric in metrics.get("metrics", ()))
            clock_in_doubt = facts.get("wall_clock") == "unsynchronised"
            if throttled or clock_in_doubt:
                at = metrics.get("received_at") if throttled else facts.get("first_received_at")
                found[device_id] = PiHealth(throttled=throttled, clock_in_doubt=clock_in_doubt, at=self._central(at))
        return found

    def overrides(self) -> tuple[Override, ...]:
        """The power holds in force (central/power/timetable.py `hold_in_force` per Frame), then the
        takeovers: each Show now Run that wins a Frame (central/why/explain.py's kinds rule). Who
        set a hold is not in the hold's own shape yet (`HoldSource`), so `by` is None; a Show now
        Run has no end time yet, so its `until` is None."""
        frame_ids = [frame.id for frame in self.inventory.frames]
        holds: dict[tuple, list[str]] = {}
        for frame_id in frame_ids:
            hold = hold_in_force(frame_id, self._policy, self._db_now)
            if hold is not None:
                holds.setdefault((str(hold.hold_id), hold.power, hold.started_at, hold.ends_at), []).append(frame_id)
        found = [Override(kind=OverrideKind.POWER_HOLD, id=hold_id, frame_ids=tuple(sorted(frames)), power=power,
                          started_at=self._central(started_at), until=self._central(ends_at))
                 for (hold_id, power, started_at, ends_at), frames in sorted(holds.items(), key=lambda each: each[0][0])]
        takeovers: dict[str, list[str]] = {}
        for frame_id in frame_ids:
            winner = self.winners.get(frame_id)
            if winner is not None and winner.kind == "show-now" and winner.run_id is not None:
                takeovers.setdefault(winner.run_id, []).append(frame_id)
        started = {run.run_id: run.started_at for run in self.projection.current.runs}
        found.extend(Override(kind=OverrideKind.TAKEOVER, id=run_id, frame_ids=tuple(sorted(frames)),
                              name=self.winners[frames[0]].name, started_at=started.get(run_id))
                     for run_id, frames in sorted(takeovers.items()))
        return tuple(found)

    def showing(self) -> Mapping[str, Showing] | None:
        """Decision 0036 (slice 3): what each Frame shows and has shown (central/infra/
        everyday_store.py `showing_in`); None without Coordination's layer read."""
        if self._layers_in is None:
            return None
        return showing_in(self._conn, self._now, [frame.id for frame in self.inventory.frames],
                          runtime=self.runtime, layers=self._layers_in(self._conn, self._now))

    def failing_steps(self) -> tuple[FailingStep, ...]:
        return self._failing_steps()

    # --- Home's own reads (what plays; no input of the status) ------------------------------------

    def playing(self, frame_id: str) -> Playing | None:
        """What plays on `frame_id`: its winner, until its Program's current window ends."""
        winner = self.winners.get(frame_id)
        if winner is None:
            return None
        windows = None if winner.program_id is None else self.projection.program_windows.get(winner.program_id)
        current = None if windows is None else windows.current
        return Playing(kind=winner.kind, scene_id=winner.scene_id, program_id=winner.program_id, name=winner.name,
                       until=None if current is None else current.end_at)

    def now_next(self, surface_id: str, frame_ids: tuple[str, ...]) -> NowNext:
        """A Wall's now (what plays on most of its Frames; a tie by name) and next (the Program,
        among those whose Scene has one of its Frames, whose next window starts first)."""
        playing = [found for frame_id in frame_ids if (found := self.playing(frame_id)) is not None]
        counted = Counter((each.kind, each.scene_id, each.program_id) for each in playing)
        now = min(playing, key=lambda each: (-counted[(each.kind, each.scene_id, each.program_id)], each.name,
                                             each.scene_id), default=None)
        scenes = self.runtime.export_state()["scenes"]
        targets = {_TARGET + frame_id for frame_id in frame_ids}
        upcoming = []
        for program_id, windows in self.projection.program_windows.items():
            program, scene = self.programs.get(program_id), None
            if program is not None and windows.next is not None:
                scene = scenes.get(program.scene_id)
            if scene is not None and targets & Scene.model_validate(scene).participants:
                upcoming.append(Upcoming(program_id=program_id, scene_id=program.scene_id, name=program.name,
                                         at=windows.next.start_at))
        return NowNext(surface_id=surface_id, now=now,
                       next=min(upcoming, key=lambda each: (each.at, each.program_id), default=None))


class PgFrameStatusLog:
    """`FrameStatusLog`: the one repository of `frame_status`, and the writer and reader of its
    records in the event log."""

    def __init__(self, times: TransactionClock) -> None:
        """`times` stamps the audit times (the database's clock in production)."""
        self._times = times

    def kept_in(self, tx: Transaction) -> Mapping[str, KeptStatus]:
        rows = pg_connection(tx).execute("SELECT frame_id, word, setup, because, problem, since FROM frame_status").fetchall()
        return {row["frame_id"]: KeptStatus(_key(row), float(row["since"])) for row in rows}

    def record_in(self, tx: Transaction, frame_id: str, key: StatusKey, since: float) -> None:
        conn = pg_connection(tx)
        at = self._times.now_in(conn)
        word, setup, problem = (None if value is None else value.value for value in (key.word, key.setup, key.problem))
        conn.execute(
            "INSERT INTO frame_status (frame_id, word, setup, because, problem, since, created_at, created_by,"
            " changed_at, changed_by) VALUES (%(frame)s, %(word)s, %(setup)s, %(row)s, %(problem)s, %(since)s,"
            " %(at)s, %(by)s, %(at)s, %(by)s) ON CONFLICT (frame_id) DO UPDATE SET word=EXCLUDED.word,"
            " setup=EXCLUDED.setup, because=EXCLUDED.because, problem=EXCLUDED.problem, since=EXCLUDED.since,"
            " changed_at=EXCLUDED.changed_at, changed_by=EXCLUDED.changed_by",
            {"frame": frame_id, "word": word, "setup": setup, "row": key.row.value, "problem": problem,
             "since": since, "at": at, "by": _BY})
        frame_status_in(conn, frame_id, {"word": word, "setup": setup, "row": key.row.value, "problem": problem,
                                         "since": since})

    def recent_in(self, tx: Transaction, frame_id: str, limit: int) -> tuple[StatusChange, ...]:
        return tuple(StatusChange(frame_id=frame_id, key=_key({**event.body, "because": event.body["row"]}),
                                  since=float(event.body["since"]), recorded_at=event.recorded_at)
                     for event in frame_statuses_in(pg_connection(tx), frame_id, limit))


def _key(row: Mapping[str, Any]) -> StatusKey:
    """A kept row's key. Its vocabularies are code's: a value this Central no longer has fails here,
    never reads as another word."""
    return StatusKey(word=None if row["word"] is None else StatusWord(row["word"]),
                     setup=None if row["setup"] is None else SetupStep(row["setup"]),
                     row=Row(row["because"]),
                     problem=None if row["problem"] is None else ProblemCode(row["problem"]))


class PgFrameStatus:
    """A Frame's status over one Database: the scheduler's `status` step, Home's read and the Frame
    page's."""

    def __init__(self, db: Database, clock: Clock, *, times: TransactionClock, inventory: InventoryReader,
                 plans: PlanReader, runtime: Callable[[Any], Runtime],
                 failing_steps: Callable[[], tuple[FailingStep, ...]] = lambda: (),
                 layers: LayerReader | None = None) -> None:
        """`inventory`: the Registry's `inventory_in`; `plans`: Coordination's `frame_plans_in`;
        `runtime`: the Runtime's `read_in`; `failing_steps`: the scheduler's steps failing now;
        `layers`: Coordination's `frame_layers_in` (slice 3's everyday actions; None offers none)."""
        self._db, self._clock = db, clock
        self._log: FrameStatusLog = PgFrameStatusLog(times)
        self._readers = {"times": times, "inventory": inventory, "plans": plans, "runtime": runtime,
                         "failing_steps": failing_steps}
        self._layers = layers

    def everyday_in(self, conn: Any, now: float, frame_ids: Sequence[str] | None = None,
                    ) -> dict[str, EverydayFacts]:
        """central/everyday/ports.py `FrameFactsReader`: each Frame's everyday facts in the caller's
        transaction (Coordination's, under its lock), judged as Home's `can` judges them."""
        reading = PgStatusReading(conn, now, **self._readers, layers=self._layers)
        frames, house = facts_of(reading)
        found = {}
        for frame_id, facts in frames.items():
            if frame_ids is not None and frame_id not in frame_ids:
                continue
            status = frame_status(facts, house, now)
            everyday = everyday_facts(facts, house, now, status.setup)
            if everyday is not None:
                found[frame_id] = everyday
        return found

    def _composed(self, conn: Any, *, everyday: bool = False,
                  ) -> tuple[PgStatusReading, dict[str, FrameStatus], Mapping[str, KeptStatus], Any]:
        """One reading of `conn`, every Frame's status composed against its kept row; `everyday`:
        with what each Frame shows, so its everyday actions are offered (Home, the Frame page)."""
        reading = PgStatusReading(conn, self._clock.utc(), **self._readers,
                                  layers=self._layers if everyday else None)
        frames, house = facts_of(reading)
        kept = self._log.kept_in(PgTransaction(conn))
        statuses = {frame_id: frame_status(facts, house, reading.now, kept.get(frame_id))
                    for frame_id, facts in frames.items()}
        return reading, statuses, kept, house

    def record(self) -> int:
        """The `status` step: compose every Frame and keep each whose key (word, setup step, row,
        problem code) differs from its kept row's, with one event-log record each; a tick that
        changes nothing writes nothing. Answers how many it kept."""
        with self._db.transaction() as conn:
            _, statuses, kept, _ = self._composed(conn)
            tx = PgTransaction(conn)
            changed = 0
            for frame_id, status in sorted(statuses.items()):
                key = key_of(status.word, status.setup, status.because,
                             None if status.problem is None else status.problem.code)
                held = kept.get(frame_id)
                if held is None or held.key != key:
                    self._log.record_in(tx, frame_id, key, status.since)
                    changed += 1
        return changed

    def home(self) -> HomeView:
        """Home's document, from one read-only snapshot."""
        with self._db.read_snapshot() as conn:
            reading, statuses, _, house = self._composed(conn, everyday=True)
            devices = reading.pis()
            walls, now_next = [], []
            for wall in reading.inventory.walls:
                frames = tuple(FramePlace(
                    frame_id=frame.id, name=frame.name, x_mm=frame.x_mm, y_mm=frame.y_mm, size_mm=frame.size_mm,
                    player_id=frame.player_id, device_id=devices.get(frame.id), playing=reading.playing(frame.id),
                    status=statuses[frame.id]) for frame in reading.inventory.frames if frame.surface_id == wall.surface_id)
                walls.append(WallView(surface_id=wall.surface_id, name=wall.name, room=wall.room, frames=frames,
                                      can=wall_can(frame.status for frame in frames)))
                now_next.append(reading.now_next(wall.surface_id, tuple(frame.frame_id for frame in frames)))
            return home_view(house=house_status(tuple(statuses.values()), house, reading.now), walls=tuple(walls),
                             overrides=reading.overrides(), now_next=tuple(now_next))

    def frame(self, frame_id: str) -> FrameStatusView:
        """The Frame page's status: the Frame's status now and its recent changes. UnknownFrame for
        no such Frame."""
        with self._db.read_snapshot() as conn:
            _, statuses, _, _ = self._composed(conn, everyday=True)
            if frame_id not in statuses:
                raise UnknownFrame(frame_id)
            recent = self._log.recent_in(PgTransaction(conn), frame_id, RECENT_CHANGES)
        return FrameStatusView(status=statuses[frame_id], recent=tuple(
            StatusChangeView(word=change.key.word, setup=change.key.setup, row=change.key.row,
                             problem=change.key.problem, since=change.since, recorded_at=change.recorded_at)
            for change in recent))


__all__ = ["RECENT_CHANGES", "InventoryReader", "PgFrameStatus", "PgFrameStatusLog", "PgStatusReading", "PlanReader",
           "UnknownFrame"]
