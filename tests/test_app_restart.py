"""Restart app (decision 0036, owner decision O1), through Central's real routes: the route fences
the device, queues `RestartApp` with the console's request id and writes its ask in that
transaction; the worker's real `RestartAppHandler` asks the Pi's broker once (a recording
`AppRestarter` here) and records the outcome; the progress read folds the ask, the broker's own
records as Central's real link store writes them, and the Player's report: requested -> app stopped
-> app started -> showing, each with its time.

The broker's side (one stop, one start, never repeated) is tests/node/apps/test_app_restart.py's;
the method across a real bus and leaf is tests/integration/test_node_bus_apps.py's.

Mutation probes: count a showing report recorded before "app stopped" -> a restart reads done
while the old Player's report is the newest, fails; drop the route's pending-copy check -> a second
request while one is queued answers 200 and queues nothing, fails.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support.pi_reboots import reboot_queue
from test_node_boot import enroll as enroll_device
from test_registry import ADMIN, enroll

from central.app import create_app
from central.fleet.app_restart import (
    APP_RESTART_SECONDS,
    APP_RESTARTS_PATH,
    AppRestartView,
    RestartAppHandler,
    RestartFacts,
    RestartFailure,
    RestartState,
    restart_progress,
)
from central.fleet.link_asks import Ask
from central.infra.job_queue import pending_in
from central.infra.node_link_store import PgLinkStores
from central.infra.transactions import PgTransactions
from central.kernel.job_types import CATALOG, RebootPi, RestartApp
from contracts.node_apps import (
    APP_RESTART_KEY,
    APP_RESTART_RECORD,
    APPS_LINE,
    AppRestartOutcome,
    AppRestartState,
    AppRestartStep,
    encode_app_restart_state,
)
from contracts.node_link import Pipe
from contracts.node_state import (
    FRAMES_KEY,
    PLAYER_LINE,
    FrameReport,
    FrameReports,
    encode_frame_reports,
)
from nodeapi.buffers import state_prefix, state_stream
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

HEADERS = {"Authorization": "Bearer " + ADMIN}
SERIAL = "c0ffee02"
RECORDS = "RECORD_apps"


class RecordingRestarter:
    """An AppRestarter that records each ask and answers `outcome` (None: not delivered)."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.outcome: AppRestartOutcome | None = AppRestartOutcome.ACCEPTED

    async def restart(self, device_id: str, request_id: str) -> AppRestartOutcome | None:
        self.asked.append((device_id, request_id))
        return self.outcome


class Pi:
    """One enrolled Pi with a Player, Central's app, the worker's run of the queued restart with
    the real handler, and what the Pi's lines state, committed by the real link store."""

    def __init__(self, registry, client: TestClient) -> None:
        self.registry, self.client = registry, client
        self.device = enroll_device(registry.db, SERIAL, registry.clock.utc())
        enroll(registry, device_id=self.device)
        self.broker = RecordingRestarter()
        self.path = APP_RESTARTS_PATH.format(device_id=self.device)
        self._seq = 0

    def ask(self, request_id, device: str | None = None):
        path = self.path if device is None else APP_RESTARTS_PATH.format(device_id=device)
        return self.client.post(path, headers=HEADERS, json={"request_id": str(request_id)})

    def progress(self) -> AppRestartView | None:
        read = self.client.get(self.path, headers=HEADERS)
        assert read.status_code == 200, read.text
        restart = read.json()["restart"]
        return None if restart is None else AppRestartView.model_validate(restart)

    def run_queued(self, outcome: AppRestartOutcome | None = AppRestartOutcome.ACCEPTED) -> RestartApp:
        """The worker runs the Pi's one queued restart with the real handler, then finishes it."""
        self.broker.outcome = outcome
        db = self.registry.db
        with db.transaction() as conn:
            [(job, status)] = pending_in(conn, RestartApp(request_id="x", device_id=self.device, by="central"))
        assert status == "todo"
        with ThreadPoolExecutor(1) as worker:   # on its own thread, as the worker's loop is
            worker.submit(asyncio.run, RestartAppHandler(self.broker, PgTransactions(db)).handle(job)).result(30)
        with db.transaction() as conn:
            conn.execute("UPDATE procrastinate_jobs SET status='succeeded' WHERE status='todo' "
                         "AND args->>'request_id'=%s", (job.request_id,))
        return job

    def _commit(self, pipe: Pipe, stream: str, subject: str, value: bytes) -> None:
        self._seq += 1
        read = Read(Token("epoch", self._seq), subject, {}, value)
        asyncio.run(PgLinkStores(self.registry.db).store(SERIAL, pipe).commit(
            stream, Batch((read,), Token("epoch", self._seq), False)))

    def broker_states(self, request_id, step: AppRestartStep, *, record: bool = True, state: bool = True) -> None:
        """The broker's step as its line carries it: a record, and the line's newest state."""
        value = encode_app_restart_state(AppRestartState(request_id=str(request_id), step=step))
        if record:
            self._commit(Pipe.FLEET, RECORDS, f"{APPS_LINE}.{APP_RESTART_RECORD}", value)
        if state:
            self._commit(Pipe.FLEET, state_stream(APPS_LINE), state_prefix(APPS_LINE) + APP_RESTART_KEY, value)

    def player_reports(self, *, showing: bool) -> None:
        report = FrameReport(frame_id="hall", generation=1, output_id="HDMI-A-1", showing=showing, light_lux=None)
        self._commit(Pipe.SHOW, state_stream(PLAYER_LINE), state_prefix(PLAYER_LINE) + FRAMES_KEY,
                     encode_frame_reports(FrameReports(frames=(report,))))


@pytest.fixture
def pi(registry):
    reboot_queue(registry.db)
    with TestClient(create_app(registry.db, registry.clock, ADMIN)) as client:
        yield Pi(registry, client)


def _steps(view: AppRestartView) -> dict[str, float | None]:
    return {step.step.value: step.at for step in view.steps}


def test_restart_app_is_a_job_of_its_own_queued_once_per_pi():
    assert RestartApp in CATALOG and RestartApp.queued_by == ("device_id",) == RebootPi.queued_by
    assert RestartApp.delivery.retry == ()                  # a person's request is never repeated
    assert RestartApp.job_name != RebootPi.job_name


def test_a_restart_is_requested_asked_once_and_read_step_by_step(pi):
    """requested -> app stopped -> app started -> showing, each with its time, in order."""
    assert pi.progress() is None
    pi.player_reports(showing=True)                         # the Player as it was before the restart
    request = uuid4()
    answer = pi.ask(request)
    assert answer.status_code == 200 and answer.json() == {"duplicate": False}, answer.text
    view = pi.progress()
    assert (view.request_id, view.by, view.state, view.failure) == (
        str(request), "api-token", RestartState.IN_PROGRESS, None)
    assert [step.step.value for step in view.steps] == ["requested", "app_stopped", "app_started", "showing"]
    requested = _steps(view)["requested"]
    assert requested is not None and list(_steps(view).values())[1:] == [None, None, None]
    # A double tap is one request; another while it is queued is refused and leaves no ask.
    assert pi.ask(request).json() == {"duplicate": True}
    refused = pi.ask(uuid4())
    assert refused.status_code == 409 and refused.json()["error"] == "node_app_restart_outstanding"
    job = pi.run_queued()
    assert (job.request_id, job.by) == (str(request), "api-token")
    assert pi.broker.asked == [(pi.device, str(request))]   # asked once
    assert _steps(pi.progress()) == {"requested": requested, "app_stopped": None, "app_started": None, "showing": None}

    pi.broker_states(request, AppRestartStep.APP_STOPPED)
    stopped = _steps(pi.progress())
    assert stopped["app_stopped"] >= requested and stopped["app_started"] is None
    # The old Player's report is still the newest: nothing is showing yet.
    pi.broker_states(request, AppRestartStep.APP_STARTED)
    started = pi.progress()
    assert started.state is RestartState.IN_PROGRESS
    assert _steps(started)["app_started"] >= stopped["app_stopped"] and _steps(started)["showing"] is None
    assert _steps(started)["app_stopped"] == stopped["app_stopped"]
    pi.player_reports(showing=False)                        # the new Player, before its first picture
    assert pi.progress().state is RestartState.IN_PROGRESS
    pi.player_reports(showing=True)
    done = pi.progress()
    assert (done.state, done.failure) == (RestartState.DONE, None)
    times = list(_steps(done).values())
    assert all(at is not None for at in times) and times == sorted(times)
    assert pi.broker.asked == [(pi.device, str(request))]   # reading never asks again
    # Once run, a new request is a new restart, and the read is the newest's.
    again = uuid4()
    assert pi.ask(again).json() == {"duplicate": False}
    newest = pi.progress()
    assert (newest.request_id, newest.state) == (str(again), RestartState.IN_PROGRESS)
    assert _steps(newest)["app_stopped"] is None            # the earlier restart's steps are not this one's


def test_a_step_whose_record_was_lost_reads_from_the_lines_newest_state(pi):
    """A bus restart starts empty: the broker states its newest step again as the line's state."""
    request = uuid4()
    pi.ask(request)
    pi.run_queued()
    pi.broker_states(request, AppRestartStep.APP_STARTED, record=False)
    steps = _steps(pi.progress())
    assert steps["app_started"] is not None and steps["app_stopped"] == steps["app_started"]
    # Another request's state says nothing of this one.
    other = uuid4()
    pi.ask(other)
    pi.run_queued()
    assert _steps(pi.progress())["app_started"] is None


@pytest.mark.parametrize(("outcome", "step", "failure"), [
    (None, None, RestartFailure.NOT_DELIVERED),
    (AppRestartOutcome.IN_FLIGHT, None, RestartFailure.IN_FLIGHT),
    (AppRestartOutcome.ACCEPTED, AppRestartStep.NOT_STOPPED, RestartFailure.NOT_STOPPED),
    (AppRestartOutcome.ACCEPTED, AppRestartStep.NOT_STARTED, RestartFailure.NOT_STARTED),
])
def test_a_restart_that_fails_says_why_and_is_not_asked_again(pi, outcome, step, failure):
    request = uuid4()
    pi.ask(request)
    pi.run_queued(outcome)
    if step is not None:
        pi.broker_states(request, step)
    view = pi.progress()
    assert (view.state, view.failure) == (RestartState.FAILED, failure)
    if step is AppRestartStep.NOT_STARTED:
        assert _steps(view)["app_stopped"] is not None      # it was stopped, then its start failed
    assert pi.broker.asked == [(pi.device, str(request))]
    with pi.registry.db.transaction() as conn:              # nothing is queued behind it
        assert pending_in(conn, RestartApp(request_id="x", device_id=pi.device, by="central")) == ()


def test_a_request_the_worker_reaches_too_late_is_not_carried_out(pi):
    """The worker was stopped or its queue stalled: past APP_RESTART_SECONDS the read already says
    the restart did not happen and nobody watches, so the Pi is not asked hours later; the job ends
    and a new request can be made. Mutation probe: drop the handler's age check -> the broker is asked."""
    request = uuid4()
    pi.ask(request)
    with pi.registry.db.transaction() as conn:
        conn.execute("UPDATE events SET recorded_at = recorded_at - %s WHERE kind='link-action' "
                     "AND body->'body'->>'request_id' = %s", (APP_RESTART_SECONDS + 1, str(request)))
    assert pi.ask(uuid4()).status_code == 409                # still queued: no second copy
    pi.run_queued()
    assert pi.broker.asked == []
    view = pi.progress()
    assert (view.state, view.failure) == (RestartState.FAILED, RestartFailure.NOT_DELIVERED)
    again = uuid4()
    assert pi.ask(again).status_code == 200
    pi.run_queued()
    assert pi.broker.asked == [(pi.device, str(again))]


def test_a_duplicate_answer_from_the_broker_reads_as_delivered(pi):
    """The broker already served this request (the worker's job ran twice): its steps stand."""
    request = uuid4()
    pi.ask(request)
    pi.run_queued(AppRestartOutcome.DUPLICATE)
    pi.broker_states(request, AppRestartStep.APP_STOPPED)
    view = pi.progress()
    assert (view.state, view.failure) == (RestartState.IN_PROGRESS, None)


def test_a_restart_needs_an_active_device_a_request_id_and_the_operator(pi):
    unknown = "device-" + "ef" * 32
    refused = pi.ask(uuid4(), device=unknown)
    assert refused.status_code == 403 and refused.json()["error"] == "node_device_unavailable"
    assert pi.client.get(APP_RESTARTS_PATH.format(device_id=unknown), headers=HEADERS).status_code == 403
    for body in ({}, {"request_id": "not-a-uuid"}, {"request_id": str(uuid4()), "reason": "x"}, {"command_id": str(uuid4())}):
        bad = pi.client.post(pi.path, headers=HEADERS, json=body)
        assert bad.status_code == 422 and bad.json()["error"] == "invalid_operator_app_restart", bad.text
    assert pi.client.post(pi.path, json={"request_id": str(uuid4())}).status_code in (401, 403)
    assert pi.client.get(pi.path).status_code in (401, 403)
    assert pi.progress() is None and pi.broker.asked == []


def test_a_pis_reboot_and_its_app_restart_are_queued_apart(pi):
    """One pending copy per Pi and per kind: a queued reboot does not hold Restart app back."""
    queued = pi.client.post(f"/v1/operator/node/devices/{pi.device}/reboots", headers=HEADERS,
                            json={"command_id": str(uuid4()), "reason": "console/2026-11-22"})
    assert queued.status_code == 200, queued.text
    assert pi.ask(uuid4()).json() == {"duplicate": False}
    device = pi.client.get(f"/v1/operator/node/devices/{pi.device}", headers=HEADERS).json()
    assert len(device["reboot_commands"]) == 1              # the restart is no reboot record


# -- the rule, on its own clock -------------------------------------------------------------------


def _ask(outcome: dict | None = None, requested_at: float = 1000.0) -> Ask:
    return Ask("r1", {"request_id": "r1", "by": "person:matt"}, requested_at,
               outcome, None if outcome is None else requested_at + 1, outstanding=outcome is None)


ACCEPTED = {"request_id": "r1", "by": "person:matt", "delivered": True, "outcome": "accepted"}


def test_a_restart_not_showing_in_time_reads_timed_out_and_showing_late_is_done():
    started = RestartFacts({AppRestartStep.APP_STOPPED: 1002.0, AppRestartStep.APP_STARTED: 1003.0}, None)
    assert restart_progress(_ask(ACCEPTED), started, now=1000.0 + APP_RESTART_SECONDS).state is RestartState.IN_PROGRESS
    late = restart_progress(_ask(ACCEPTED), started, now=1000.5 + APP_RESTART_SECONDS)
    assert (late.state, late.failure, late.by) == (RestartState.FAILED, RestartFailure.TIMED_OUT, "person:matt")
    assert [step.at for step in late.steps] == [1000.0, 1002.0, 1003.0, None]   # how far it got stays
    never_run = restart_progress(_ask(), RestartFacts({}, None), now=2000.0)
    assert never_run.failure is RestartFailure.TIMED_OUT
    shown = RestartFacts(started.steps, 1900.0)
    assert restart_progress(_ask(ACCEPTED), shown, now=5000.0).state is RestartState.DONE


def test_showing_counts_only_after_the_brokers_start_from_a_report_after_the_stop():
    """Mutation probe: count a showing report recorded before "app stopped" -> the first case is done."""
    before_stop = RestartFacts({AppRestartStep.APP_STOPPED: 1002.0, AppRestartStep.APP_STARTED: 1003.0}, 1001.0)
    assert restart_progress(_ask(ACCEPTED), before_stop, now=1010.0).state is RestartState.IN_PROGRESS
    not_started = RestartFacts({AppRestartStep.APP_STOPPED: 1002.0}, 1004.0)
    view = restart_progress(_ask(ACCEPTED), not_started, now=1010.0)
    assert view.state is RestartState.IN_PROGRESS and view.steps[-1].at is None
    shown = RestartFacts({AppRestartStep.APP_STOPPED: 1002.0, AppRestartStep.APP_STARTED: 1003.0}, 1002.0)
    assert restart_progress(_ask(ACCEPTED), shown, now=1010.0).state is RestartState.DONE
