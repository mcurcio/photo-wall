"""Restart app: a person's restart of one Pi's photo app, asked of its app broker over the Pi's
fleet-pipe link (decision 0036, "Restart app"; owner decision O1: built as a person's request,
never repeated).

It is a job, as a Pi's reboot is (central/fleet/pi_reboot.py; the queueing and the fold of an ask
with its outcome are central/fleet/link_asks.py's, shared with it): the route fences the device,
defers `RestartApp` with the console's request id and writes an ask row (kind 'app-restart-asked',
body {"request_id", "by"}) in that transaction; the worker's `RestartAppHandler` asks the broker
(contracts/node_apps.py APP_RESTART_METHOD) through `AppRestarter`, once, and records the outcome as
one `events` row (kind 'app-restart', body {"request_id", "by", "delivered", "outcome"}). Nothing
here asks a Pi twice: no retry, no timer. A second request while one is queued is refused (409);
one that reaches the broker while its restart is in flight is answered `in_flight` and does nothing.
A request the worker reaches only after APP_RESTART_SECONDS (its queue was stalled, or it was
stopped) is not carried out: by then the read has said `timed_out` and nobody is watching, so the
handler asks no Pi and records it not delivered.

Progress (`AppRestartView`, the console's StepProgress) is worked out on read, never stored:
requested -> app stopped -> app started -> showing, each with the time Central recorded it (the
database's clock, never a Pi's):

- requested: the ask row;
- app stopped, app started: the broker's own records on its bus line (`apps.record.restart`,
  contracts/node_apps.py `AppRestartState`), which Central's link writes into `events` like every
  Pi record; a step whose record was lost reads from the line's newest state (`restart`), at the
  time Central recorded that;
- showing: the Player's report (contracts/node_state.py `FrameReports`): its newest value says a
  Frame's picture is on screen and Central recorded it at or after "app stopped" (the stopped
  Player says nothing more). Central keeps only the newest report, so this time is the newest
  showing report's and moves when the Player reports again.

A restart fails with one closed code (`RestartFailure`); `timed_out` is judged on read, on the
database's clock, when it is not showing APP_RESTART_SECONDS after it was requested. The read
answers the Pi's newest restart only.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol
from uuid import UUID

from central.actor import Actor
from central.db import Database, DatabaseTransactionClock
from central.fleet.device_fence import standing_in
from central.fleet.link_asks import Ask, AskKinds, asked_at_in, asks_in, queue_ask_in
from central.fleet.models import NodeControlError
from central.infra.events import link_action_in
from central.infra.node_link_latest import latest_values_in
from central.infra.node_links import NodeLinks
from central.infra.transactions import PgTransactions, pg_connection
from central.kernel.job_types import RestartApp
from central.kernel.publishing import Publisher
from contracts.models import Instant, Model
from contracts.node_apps import (
    APP_RESTART_KEY,
    APP_RESTART_METHOD,
    APP_RESTART_RECORD,
    APPS_LINE,
    AppRestart,
    AppRestartOutcome,
    AppRestartState,
    AppRestartStep,
    decode_app_restart_answer,
    decode_app_restart_state,
    encode_app_restart,
)
from contracts.node_link import Pipe
from contracts.node_state import FRAMES_KEY, PLAYER_LINE, decode_frame_reports

log = logging.getLogger(__name__)

# Where Restart app is asked (POST) and its progress read (GET): the one home of the address
# (central/fleet/node_routes.py mounts it; central/home_routes.py names it for a Frame's `can`).
APP_RESTARTS_PATH: Final = "/v1/operator/node/devices/{device_id}/app-restarts"
APP_RESTART_CALL_SECONDS: Final = 5.0             # how long the broker has to answer `restart`
APP_RESTART_ACTION: Final = "app-restart"         # the `events` link action's kind: the outcome
APP_RESTART_ASKED_ACTION: Final = "app-restart-asked"   # the operator's ask, written with its job
APP_RESTART_KINDS: Final = AskKinds(asked=APP_RESTART_ASKED_ACTION, outcome=APP_RESTART_ACTION)
# Not showing this long after it was requested, a restart reads `timed_out`: the unit's stop may
# take its whole timeout (appliance/apps/stop_linux.py, 30 s) and the Player then starts, fetches
# and draws. Judged on read; nothing is asked again when it passes.
APP_RESTART_SECONDS: Final = 180.0
# The newest restart is picked among this many: an outcome recorded after a newer ask (the worker
# was still asking the Pi when the next request was queued) must fold with its own ask first.
_NEWEST_AMONG: Final = 4
_RECORD_SUBJECT: Final = f"{APPS_LINE}.{APP_RESTART_RECORD}"


class AppRestartRefusal(StrEnum):
    """Why Central did not take a Restart app request; the console words each."""
    OUTSTANDING = "node_app_restart_outstanding"      # 409: another restart of this Pi's app is queued
    UNAVAILABLE = "node_app_restart_unavailable"      # 503: Central has no job queue
    INVALID = "invalid_operator_app_restart"          # 422: the body is not {"request_id": UUID}
    DEVICE_UNAVAILABLE = "node_device_unavailable"    # 403: the device is not active (the device fence's code)


class AppRestarter(Protocol):
    async def restart(self, device_id: str, request_id: str) -> AppRestartOutcome | None:
        """Ask the Pi's app broker to restart the app over its fleet-pipe link, once: its answer,
        or None when the Pi has no link now, does not answer within the call's timeout, or answers
        what does not decode."""
        ...


class BusAppRestart:
    """AppRestarter over the worker's fleet-pipe NodeLinks (built by NodeBus.app_restart())."""

    def __init__(self, links: NodeLinks) -> None:
        self._links = links

    async def restart(self, device_id: str, request_id: str) -> AppRestartOutcome | None:
        reply = await self._links.call(device_id, APPS_LINE, APP_RESTART_METHOD,
                                       encode_app_restart(AppRestart(request_id=request_id)),
                                       timeout=APP_RESTART_CALL_SECONDS)
        if reply is None:
            return None
        try:
            return decode_app_restart_answer(reply).outcome
        except ValueError:
            log.warning("app restart on %s: the broker's answer does not decode", device_id)
            return None


class NoHubAppRestart:
    """AppRestarter with no hub deployed: no Pi is asked."""

    async def restart(self, device_id: str, request_id: str) -> AppRestartOutcome | None:
        return None


class RestartAppHandler:
    """Handler[RestartApp]: ask the Pi's broker through `AppRestarter`, once, then record the
    outcome in `events`. A request older than APP_RESTART_SECONDS when the worker reaches it is
    not asked of the Pi at all (recorded not delivered): a person's request is not carried out
    long after they stopped watching it."""

    def __init__(self, restarter: AppRestarter, transactions: PgTransactions) -> None:
        self._restarter = restarter
        self._transactions = transactions

    async def handle(self, job: RestartApp) -> None:
        if await asyncio.to_thread(self._too_late, job):
            log.warning("app restart %s on %s (by %s): reached too late, the Pi is not asked",
                        job.request_id, job.device_id, job.by)
            outcome = None
        else:
            outcome = await self._restarter.restart(job.device_id, job.request_id)
        log.info("app restart %s on %s (by %s): %s", job.request_id, job.device_id, job.by,
                 "not delivered" if outcome is None else outcome.value)
        await asyncio.to_thread(self._record, job, outcome)

    def _too_late(self, job: RestartApp) -> bool:
        """Whether the request was asked more than APP_RESTART_SECONDS ago, on the database's clock
        (the ask row's); a job with no ask row has no age."""
        with self._transactions.begin() as tx:
            conn = pg_connection(tx)
            asked_at = asked_at_in(conn, job.device_id, APP_RESTART_KINDS, job.request_id)
            return asked_at is not None and _CLOCK.now_in(conn) - asked_at > APP_RESTART_SECONDS

    def _record(self, job: RestartApp, outcome: AppRestartOutcome | None) -> None:
        with self._transactions.begin() as tx:
            link_action_in(pg_connection(tx), job.device_id, Pipe.FLEET, APP_RESTART_ACTION,
                           {"request_id": job.request_id, "by": job.by, "delivered": outcome is not None,
                            "outcome": None if outcome is None else outcome.value})


# -- the progress, worked out on read -----------------------------------------------------------


class RestartStepName(StrEnum):
    """The four steps of a restart, in order (decision 0036)."""
    REQUESTED = "requested"
    APP_STOPPED = "app_stopped"
    APP_STARTED = "app_started"
    SHOWING = "showing"


class RestartState(StrEnum):
    IN_PROGRESS = "in_progress"
    DONE = "done"            # showing
    FAILED = "failed"        # `failure` says why


class RestartFailure(StrEnum):
    """Why a restart failed; the console words each."""
    NOT_DELIVERED = "not_delivered"   # the Pi had no link, or its broker did not answer
    IN_FLIGHT = "in_flight"           # the broker was already restarting the app for another request
    NOT_STOPPED = "not_stopped"       # the Pi refused to stop the app; nothing started
    NOT_STARTED = "not_started"       # the app was stopped and its start failed
    TIMED_OUT = "timed_out"           # not showing APP_RESTART_SECONDS after it was requested


class RestartStepView(Model):
    """One step and when Central recorded it (None: not reached, or not known)."""
    step: RestartStepName
    at: Instant | None


class AppRestartView(Model):
    """One restart: who asked, how it stands, and its four steps in order."""
    request_id: str
    by: str
    state: RestartState
    failure: RestartFailure | None
    steps: tuple[RestartStepView, ...]


class AppRestartsView(Model):
    """The device read of Restart app: the Pi's newest restart still kept, None when there is none."""
    restart: AppRestartView | None


@dataclass(frozen=True, slots=True)
class RestartFacts:
    """What Central recorded of one restart besides its ask (`link_asks.Ask`): the broker's steps
    with the time each was recorded (the first record of a step counts), and when the Player's
    newest report that says a picture is on screen was recorded (None: its newest report says none)."""
    steps: Mapping[AppRestartStep, float]
    showing_at: float | None


def restart_progress(ask: Ask, facts: RestartFacts, *, now: float) -> AppRestartView:
    """PURE. The one rule of a restart's progress (this module's docstring). A failure is, in order:
    the worker ran it and the Pi was not asked (`not_delivered`); the broker answered `in_flight`;
    the broker stated `not_stopped` or `not_started`; else `timed_out` once `now` is more than
    APP_RESTART_SECONDS past the request with nothing showing. `showing` counts only after the
    broker's `app_started`, from a report recorded at or after `app_stopped`."""
    stopped = facts.steps.get(AppRestartStep.APP_STOPPED)
    started = facts.steps.get(AppRestartStep.APP_STARTED)
    showing = (facts.showing_at if started is not None and stopped is not None
               and facts.showing_at is not None and facts.showing_at >= stopped else None)
    outcome = None if ask.outcome is None else ask.outcome.get("outcome")
    failure: RestartFailure | None = None
    if ask.outcome is not None and ask.outcome.get("delivered") is not True:
        failure = RestartFailure.NOT_DELIVERED
    elif outcome == AppRestartOutcome.IN_FLIGHT.value:
        failure = RestartFailure.IN_FLIGHT
    elif AppRestartStep.NOT_STOPPED in facts.steps:
        failure = RestartFailure.NOT_STOPPED
    elif AppRestartStep.NOT_STARTED in facts.steps:
        failure = RestartFailure.NOT_STARTED
    elif showing is None and now - ask.requested_at > APP_RESTART_SECONDS:
        failure = RestartFailure.TIMED_OUT
    state = (RestartState.FAILED if failure is not None
             else RestartState.DONE if showing is not None else RestartState.IN_PROGRESS)
    times = ((RestartStepName.REQUESTED, ask.requested_at), (RestartStepName.APP_STOPPED, stopped),
             (RestartStepName.APP_STARTED, started), (RestartStepName.SHOWING, showing))
    return AppRestartView(request_id=ask.request_id, by=str(ask.body.get("by")), state=state, failure=failure,
                          steps=tuple(RestartStepView(step=step, at=at) for step, at in times))


# The device's records of the broker's restart steps since a time, oldest first (one `%s` each for
# the device, the record's subject and the time).
_STEPS_SQL: Final = ("SELECT data, recorded_at FROM events WHERE kind = 'record' AND device_id = %s "
                     "AND subject = %s AND recorded_at >= %s ORDER BY recorded_at, id")
# What a later step says of the ones before it, for a step whose own record was lost.
_IMPLIES: Final[Mapping[AppRestartStep, tuple[AppRestartStep, ...]]] = {
    AppRestartStep.APP_STARTED: (AppRestartStep.APP_STOPPED,),
    AppRestartStep.NOT_STARTED: (AppRestartStep.APP_STOPPED,),
}
_CLOCK: Final = DatabaseTransactionClock()


def _decoded_step(raw: bytes) -> AppRestartState | None:
    try:
        return decode_app_restart_state(raw)
    except ValueError:
        return None


def restart_facts_in(conn: Any, device_id: str, ask: Ask) -> RestartFacts:
    """`ask`'s `RestartFacts` in the caller's transaction: the broker's records of this request
    recorded since it was asked (a record that does not decode is no step), the line's newest state
    for a step no record holds (and the step it implies), and the Player's newest report."""
    steps: dict[AppRestartStep, float] = {}
    for row in conn.execute(_STEPS_SQL, (device_id, _RECORD_SUBJECT, ask.requested_at)).fetchall():
        stated = _decoded_step(bytes(row["data"] or b""))
        if stated is not None and stated.request_id == ask.request_id:
            steps.setdefault(stated.step, float(row["recorded_at"]))
    for latest in latest_values_in(conn, APPS_LINE, APP_RESTART_KEY, device_id=device_id, of_players=False):
        stated = None if latest.removed else _decoded_step(latest.data)
        if stated is not None and stated.request_id == ask.request_id and latest.recorded_at >= ask.requested_at:
            for step in (*_IMPLIES.get(stated.step, ()), stated.step):
                steps.setdefault(step, latest.recorded_at)
    showing_at = None
    for latest in latest_values_in(conn, PLAYER_LINE, FRAMES_KEY, device_id=device_id):
        reports = latest.decoded(decode_frame_reports, "frame reports")
        if reports is not None and any(frame.showing for frame in reports.frames):
            showing_at = latest.recorded_at
    return RestartFacts(steps, showing_at)


@dataclass(frozen=True, slots=True)
class OperatorAppRestart:
    """The operator's Restart app body: {"request_id": UUID}, so a double tap is one request."""

    request_id: UUID

    def __post_init__(self) -> None:
        if type(self.request_id) is not UUID:
            raise ValueError(AppRestartRefusal.INVALID.value)


def parse_operator_app_restart(value: object) -> OperatorAppRestart:
    """The body as strict JSON gave it, exactly its one field; ValueError for anything else."""
    if not isinstance(value, dict) or set(value) != {"request_id"} or not isinstance(value["request_id"], str):
        raise ValueError(AppRestartRefusal.INVALID.value)
    try:
        return OperatorAppRestart(UUID(value["request_id"]))
    except ValueError as exc:
        raise ValueError(AppRestartRefusal.INVALID.value) from exc


class OperatorAppRestarts:
    """The operator's Restart app of one Pi: the `RestartApp` job deferred with its ask row, and
    the read of its progress."""

    def __init__(self, db: Database, publisher: Publisher | None) -> None:
        self._db = db
        self._publisher = publisher

    def request(self, device_id: str, request: OperatorAppRestart, *, by: Actor) -> dict:
        """Queue the restart: {"duplicate": True} for a request id already asked of this device;
        403 `node_device_unavailable` unless the device is active; 409
        `node_app_restart_outstanding` while another restart of the Pi's app is queued; 503
        `node_app_restart_unavailable` without a job queue."""
        if self._publisher is None:
            raise NodeControlError(AppRestartRefusal.UNAVAILABLE.value, 503)
        request_id = str(request.request_id)
        job = RestartApp(request_id=request_id, device_id=device_id, by=by)
        with self._db.transaction() as conn:
            queued = queue_ask_in(conn, self._publisher, job, APP_RESTART_KINDS, device_id=device_id,
                                  request_id=request_id, outstanding=AppRestartRefusal.OUTSTANDING.value,
                                  body={"request_id": request_id, "by": by})
        return {"duplicate": not queued}

    def newest(self, device_id: str) -> AppRestartsView:
        """The Pi's newest restart and its progress, under one read-only snapshot (no lock); 403
        `node_device_unavailable` unless the device is active, as the device read."""
        with self._db.read_snapshot() as conn:
            standing_in(conn, device_id).active_generation()
            return newest_restart_in(conn, device_id)


def newest_restart_in(conn: Any, device_id: str) -> AppRestartsView:
    """`device_id`'s newest restart still kept, with its progress now (the database's clock)."""
    probe = RestartApp(request_id="read", device_id=device_id, by="central")
    asks = asks_in(conn, device_id, APP_RESTART_KINDS, probe, _NEWEST_AMONG)
    if not asks:
        return AppRestartsView(restart=None)
    ask = asks[0]
    return AppRestartsView(restart=restart_progress(ask, restart_facts_in(conn, device_id, ask),
                                                    now=_CLOCK.now_in(conn)))


__all__ = [
    "APP_RESTARTS_PATH", "APP_RESTART_ACTION", "APP_RESTART_ASKED_ACTION", "APP_RESTART_KINDS", "APP_RESTART_SECONDS",
    "AppRestartRefusal",
    "AppRestartView",
    "AppRestarter", "AppRestartsView", "BusAppRestart", "NoHubAppRestart", "OperatorAppRestart", "OperatorAppRestarts",
    "RestartAppHandler", "RestartFacts", "RestartFailure", "RestartState", "RestartStepName", "RestartStepView",
    "newest_restart_in", "parse_operator_app_restart", "restart_progress",
]
