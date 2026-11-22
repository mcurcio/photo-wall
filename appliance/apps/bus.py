"""The app broker on its Node's bus (decision 0036, "Restart app"; contracts/node_apps.py).

The broker declares the `apps` store line (contracts.node_link.STORE_LINES: fleet pipe): a circular
record buffer and its state bucket, which holds `birth`, `outbox` and APP_RESTART_KEY. It says
nothing else to Central: the run facts stay on its node feed (decision 0034, S4c).

The session serves the broker's one method, `restart` (APP_RESTART_METHOD): Central asks it over
the Pi's fleet-pipe link when a person presses Restart app (central/fleet/app_restart.py). The
method answers from `RestartAsks` alone, on the session's thread, and stops or starts nothing: the
broker's loop takes the accepted request (appliance/apps/broker_runner.py). An ask that does not
decode is answered as an error and restarts nothing.

The session connects forever on its own thread and attaches at every connect, so after a bus restart
(which starts empty) the line, `birth` and the newest restart's step are back with no help from
Central. The broker never waits on the bus: `BusRestartLine.state` only hands the step to the
session's queue.

The slice is built at import, so a key table that does not fit the line fails here, on every build.
"""
from __future__ import annotations

from typing import Final

from appliance.apps.restart import RestartAsks
from contracts.node_apps import (
    APP_RESTART_BYTES,
    APP_RESTART_KEY,
    APP_RESTART_METHOD,
    APP_RESTART_RECORD,
    APPS_COMPONENT,
    APPS_LINE,
    APPS_SCHEMA_MAJOR,
    AppRestartAnswer,
    AppRestartOutcome,
    AppRestartState,
    AppRestartStep,
    decode_app_restart,
    encode_app_restart_answer,
    encode_app_restart_state,
)
from contracts.node_link import NODE_BUS_URL, STORE_LINES
from nodeapi.buffers import KeyTable, Slice, event_buffer, state_bucket
from nodeapi.envelope import MethodCall, MethodHandler
from nodeapi.node import NodeSession, Release

APPS_VERSION: Final = "1.0.0"                     # the broker component's release version (birth)
APPS_STATE: Final = KeyTable({APP_RESTART_KEY: APP_RESTART_BYTES})


def _apps_slice() -> Slice:
    """The record buffer takes every byte of the apps line the state bucket leaves."""
    state = state_bucket(APPS_LINE, APPS_STATE)
    records = event_buffer(APPS_LINE, "record", STORE_LINES[APPS_LINE].max_bytes - state.max_bytes)
    return Slice(APPS_LINE, (records, state))


APPS_SLICE: Final[Slice] = _apps_slice()


def _restart_method(asks: RestartAsks) -> MethodHandler:
    """`restart`: decode the ask (ValueError, answered as an error, for one that does not decode)
    and answer what `asks` says of it. Nothing is stopped or started here."""
    async def answer(call: MethodCall) -> bytes:
        request_id = decode_app_restart(call.payload).request_id
        return encode_app_restart_answer(AppRestartAnswer(outcome=AppRestartOutcome(asks.ask(request_id))))
    return answer


def apps_session(release_digest: str, asks: RestartAsks, *, url: str = NODE_BUS_URL) -> NodeSession:
    """The broker's session, not started: it declares the apps line with APPS_SLICE, writes birth
    (Release(APPS_VERSION, digest=release_digest, schema_majors={"apps": APPS_SCHEMA_MAJOR})) at
    every attach and serves `restart` from `asks`. Its start() returns at once and it connects
    forever, so the broker never waits on the bus."""
    return NodeSession(APPS_COMPONENT, APPS_SLICE,
                       Release(APPS_VERSION, release_digest, {APPS_LINE: APPS_SCHEMA_MAJOR}), url=url,
                       methods={APP_RESTART_METHOD: _restart_method(asks)})


class BusRestartLine:
    """Where the broker states a restart's steps (`RestartSteps`' sink): each as one record
    (APP_RESTART_RECORD) and as the line's APP_RESTART_KEY, which the session re-puts at every
    attach. `state` returns at once: it never blocks or waits for the bus."""

    def __init__(self, session: NodeSession) -> None:
        self._session = session

    def state(self, request_id: str, step: str) -> None:
        value = encode_app_restart_state(AppRestartState(request_id=request_id, step=AppRestartStep(step)))
        self._session.events.emit(APP_RESTART_RECORD, value, schema_major=APPS_SCHEMA_MAJOR)
        self._session.state.put(APP_RESTART_KEY, value)


__all__ = ["APPS_SLICE", "APPS_STATE", "APPS_VERSION", "BusRestartLine", "apps_session"]
