"""The app broker on its Node's bus: a person's Restart app (decision 0036, "Restart app").

Issue #125 removed every automatic restart of the Player; a person pressing Restart app is a fourth
cause of a start (appliance/apps/broker.py `StartCause`: `asked_by_a_person`). Central asks it over
the Pi's fleet-pipe link, as it asks the host to reboot (contracts/node_state.py
HOST_REBOOT_METHOD), and the broker states each step of the one restart it makes. Contracts and
pydantic only: the Pi runs it.

The broker declares the `apps` store line (contracts.node_link.STORE_LINES: fleet pipe):

- `restart` (the broker's one bus method, APP_RESTART_METHOD): `AppRestart` names the request; the
  broker answers `AppRestartAnswer` at once, before it stops anything. `accepted`: this request
  stops the current run and starts exactly one. `in_flight`: a restart is in flight on this Pi
  (this request's or another's); nothing more is done. `duplicate`: this request was already
  served; nothing is done. The broker never repeats a request by itself, and a run that dies after
  the restart stays down.
- `apps.record.restart` (APP_RESTART_RECORD, the line's record buffer): `AppRestartState`, one
  record per step of a restart, each once: `app_stopped` (no run of the launch is left and its
  unit is unloaded: at once when the run was already dead), then `app_started` (the one start
  returned a run) or `not_started` (the start raised; nothing asks again); `not_stopped` when the
  stop itself was refused (nothing started).
- `restart` (APP_RESTART_KEY, the line's state bucket): the same `AppRestartState`, the newest
  restart's newest step, stated again at every attach, so a reader that lost a record (a bus
  restart starts empty) still learns where the restart stands.

Rules this module does NOT own: when a run starts (appliance/apps/broker.py); a restart's progress
as Central reads it, the Player's "showing" included (central/fleet/app_restart.py).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field

from contracts.wire_codec import encode
from contracts.wire_model import Model, decoded

APPS_LINE: Final = "apps"                    # contracts.node_link.STORE_LINES["apps"]
APPS_COMPONENT: Final = "apps"               # the component writing the line, and its birth's name
APPS_SCHEMA_MAJOR: Final = 1                 # the major of the line's record and key below
APP_RESTART_METHOD: Final = "restart"        # the broker's bus method: AppRestart in, AppRestartAnswer out
APP_RESTART_BYTES: Final = 256               # the largest ask, answer, record or state value
APP_RESTART_RECORD: Final = "record.restart"   # the record's subject under the line
APP_RESTART_KEY: Final = "restart"           # the state key of the newest AppRestartState

# A request's name: Central's is the console's request id (a UUID's text).
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", title="RequestId")]


class AppRestartOutcome(StrEnum):
    """The broker's answer to one ask (this module's docstring)."""
    ACCEPTED = "accepted"
    IN_FLIGHT = "in_flight"
    DUPLICATE = "duplicate"


class AppRestartStep(StrEnum):
    """A step of a restart the broker states (this module's docstring)."""
    APP_STOPPED = "app_stopped"
    APP_STARTED = "app_started"
    NOT_STOPPED = "not_stopped"
    NOT_STARTED = "not_started"


class AppRestart(Model):
    """An ask of the broker's `restart` method: the request's name, so a repeat is one restart."""
    request_id: RequestId


class AppRestartAnswer(Model):
    """The broker's answer to `restart`, given before it stops or starts anything."""
    outcome: AppRestartOutcome


class AppRestartState(Model):
    """One step of the restart `request_id` asked for: a record, and the state key's value."""
    request_id: RequestId
    step: AppRestartStep


def encode_app_restart(ask: AppRestart) -> bytes:
    """Canonical JSON of at most APP_RESTART_BYTES."""
    return encode(ask.model_dump(mode="json"), APP_RESTART_BYTES, "app_restart_too_large")


def decode_app_restart(raw: bytes) -> AppRestart:
    """The ask `raw` encodes; ValueError("app_restart") for anything else."""
    return decoded(AppRestart, raw, APP_RESTART_BYTES, "app_restart")


def encode_app_restart_answer(answer: AppRestartAnswer) -> bytes:
    """Canonical JSON of at most APP_RESTART_BYTES."""
    return encode(answer.model_dump(mode="json"), APP_RESTART_BYTES, "app_restart_answer_too_large")


def decode_app_restart_answer(raw: bytes) -> AppRestartAnswer:
    """The answer `raw` encodes; ValueError("app_restart_answer") for anything else."""
    return decoded(AppRestartAnswer, raw, APP_RESTART_BYTES, "app_restart_answer")


def encode_app_restart_state(state: AppRestartState) -> bytes:
    """Canonical JSON of at most APP_RESTART_BYTES."""
    return encode(state.model_dump(mode="json"), APP_RESTART_BYTES, "app_restart_state_too_large")


def decode_app_restart_state(raw: bytes) -> AppRestartState:
    """The step `raw` encodes; ValueError("app_restart_state") for anything else."""
    return decoded(AppRestartState, raw, APP_RESTART_BYTES, "app_restart_state")


__all__ = [
    "APPS_COMPONENT", "APPS_LINE", "APPS_SCHEMA_MAJOR", "APP_RESTART_BYTES", "APP_RESTART_KEY", "APP_RESTART_METHOD",
    "APP_RESTART_RECORD", "AppRestart", "AppRestartAnswer", "AppRestartOutcome", "AppRestartState", "AppRestartStep",
    "RequestId", "decode_app_restart", "decode_app_restart_answer", "decode_app_restart_state", "encode_app_restart",
    "encode_app_restart_answer", "encode_app_restart_state",
]
