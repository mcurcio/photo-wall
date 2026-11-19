"""The app-run facts the broker appends to its node feed for the health judge (1b P1b).

The broker (`appliance.apps.broker_runner.BrokerLoop`) writes them and the judge
(`appliance.health.judge`) reads them; neither imports the other, so the kinds and value shapes
live here, the lowest layer both reach. Stdlib only.

- `app_started` / `app_exited`: the run state, judged from the launch, never from samples
  alone. A run has started once the driver's launch record names it (`Launch`, written at its
  spawn or adoption: the run the driver saw, else the launch's epoch alone,
  `appliance.apps.probe.AppRunKey.of_launch`); a started run that a known turn finds not current
  has exited. So a run that began and ended between two turns (a Player that exits within the
  broker's first turn after a cold start) still starts and exits. A run is named once: a turn
  that saw it names it, and the launch's epoch alone (a record `start` wrote before it saw the
  spawn) is that same run, never a second one (`RunFacts.state`). Each is a fact, stated once
  when it first holds and never again: a run that is frozen, or stays down, writes nothing more
  (issue #125: `app_started` means a run started). Judged on every known turn, Central session
  or not.
- APP_RUN_STATE: the same state as a snapshot, carried on every page of the broker's feed
  (`AppRunState`, None before any launch names a run), current at the reply. A reader whose
  read had a gap (the ring's eviction, a new broker incarnation) learns the run state from it
  once it has caught up, never from a fact repeated on a timer (`run_state_fact`; the judge's
  `forget` drops a pending condition and the snapshot opens it again).
- `app_observation_failed`: the broker could not observe the app's unit (a driver fault, say a
  unit failed with its main process still set); reported once per reason while it lasts, and the
  broker observes again on a backoff (`appliance.apps.broker_runner.OBSERVATION_BACKOFF_MS`),
  never exiting over it.
- `app_descriptors`: every `appliance.apps.descriptors.DESCRIPTOR_SAMPLE_MS` while a run is
  current, how many file descriptors it holds against its soft limit, read from /proc.
- `app_frames_black`: the Player's own statement, on its probe channel, of which of its Outputs
  are black while content is wanted and why (the channel's run, not the Player's word for it).

The broker also tells Display which run is current (decision 0034, S4b part 4): one `app_run`
packet (`AppRunOp`) on Display's root-only ingress (`appliance.kernel.display_paths.INGRESS_SOCKET`),
sent by `appliance.apps.display_runs.DisplayRuns` and read by `appliance.display_host.runner`. The
op's name and value shape live here, with the Player's uid both contexts name.

The probe facts (`probe_*`, `app_renderer`, ...) keep their names where they are
written today; moving them here is a residual, not 1b's.
"""
from __future__ import annotations

from typing import Final, TypedDict

APP_STARTED: Final = "app_started"
APP_EXITED: Final = "app_exited"
APP_DESCRIPTORS: Final = "app_descriptors"
APP_OBSERVATION_FAILED: Final = "app_observation_failed"
APP_FRAMES_BLACK: Final = "app_frames_black"
APP_RUN_OP: Final = "app_run"       # the broker's current run, on Display's ingress
APP_RUN_STATE: Final = "app_run_state"  # the broker feed page's run state snapshot (`AppRunState`)
PLAYER_UID: Final = 10004           # the Player's uid (debian sysusers pw-player): its unit's User=, Display's grant


class RunDocument(TypedDict):
    """`appliance.apps.probe.AppRunKey.document()`: the run the fact is about."""

    invocation_id: str
    pid: int
    start_ticks: int
    app_epoch: int


class AppRunOp(TypedDict):
    """The `app_run` ingress packet: the broker's current run (`RunDocument`), None when no run is
    current. Display holds the last one it accepted; a new controller is told again."""

    op: str
    run: RunDocument | None


class AppRunValue(TypedDict):
    """The value of `app_started` and `app_exited`."""

    run: RunDocument


class AppRunState(TypedDict):
    """APP_RUN_STATE: the run the current launch named and whether a known turn found it gone."""

    run: RunDocument
    exited: bool


def run_state_fact(state: object) -> tuple[str, AppRunValue] | None:
    """The fact a page's APP_RUN_STATE holds: (`app_exited` or `app_started`, its value); None
    when no launch named a run. ValueError("app_run_state") when it is not an AppRunState."""
    if state is None:
        return None
    if (type(state) is not dict or set(state) != {"run", "exited"} or type(state["run"]) is not dict
            or type(state["exited"]) is not bool):
        raise ValueError("app_run_state")
    return (APP_EXITED if state["exited"] else APP_STARTED), {"run": state["run"]}


class AppObservationFailedValue(TypedDict):
    """The value of `app_observation_failed`: the driver's fault code (its ValueError's text)."""

    reason: str


class AppDescriptorsValue(TypedDict):
    """The value of `app_descriptors`: `open` entries in /proc/<pid>/fd, `soft_limit` the
    process's soft RLIMIT_NOFILE (/proc/<pid>/limits "Max open files")."""

    run: RunDocument
    open: int
    soft_limit: int


class FrameBlackFact(TypedDict):
    """One Output named by `app_frames_black`: `cause` is a `FrameBlackCause` value."""

    output: str
    cause: str


class AppFramesBlackValue(TypedDict):
    """The value of `app_frames_black`: every Output the run states black; empty, none is."""

    run: RunDocument
    frames: list[FrameBlackFact]


__all__ = ["APP_DESCRIPTORS", "APP_EXITED", "APP_FRAMES_BLACK", "APP_OBSERVATION_FAILED", "APP_RUN_OP",
           "APP_RUN_STATE", "APP_STARTED", "PLAYER_UID", "AppDescriptorsValue", "AppFramesBlackValue",
           "AppObservationFailedValue", "AppRunOp", "AppRunState", "AppRunValue", "FrameBlackFact", "RunDocument",
           "run_state_fact"]
