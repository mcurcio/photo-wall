"""The app-run facts the broker appends to its node feed for the health judge (1b P1b).

The broker (`appliance.apps.broker_runner.BrokerLoop`) writes them and the judge
(`appliance.health.judge`) reads them; neither imports the other, so the kinds and value shapes
live here, the lowest layer both reach. Stdlib only.

- `app_started` / `app_exited`: the loop's current run changed. A run that was the loop's
  current run on one known turn and is not on a later known turn has exited, once; a run that
  becomes current has started, once. Observed on every known turn, Central session or not.
- `app_descriptors`: every `appliance.apps.descriptors.DESCRIPTOR_SAMPLE_MS` while a run is
  current, how many file descriptors it holds against its soft limit, read from /proc.

The probe facts (`probe_*`, `app_killed`, `app_renderer`, ...) keep their names where they are
written today; moving them here is a residual, not 1b's.
"""
from __future__ import annotations

from typing import Final, TypedDict

APP_STARTED: Final = "app_started"
APP_EXITED: Final = "app_exited"
APP_DESCRIPTORS: Final = "app_descriptors"


class RunDocument(TypedDict):
    """`appliance.apps.probe.AppRunKey.document()`: the run the fact is about."""

    invocation_id: str
    pid: int
    start_ticks: int
    app_epoch: int


class AppRunValue(TypedDict):
    """The value of `app_started` and `app_exited`."""

    run: RunDocument


class AppDescriptorsValue(TypedDict):
    """The value of `app_descriptors`: `open` entries in /proc/<pid>/fd, `soft_limit` the
    process's soft RLIMIT_NOFILE (/proc/<pid>/limits "Max open files")."""

    run: RunDocument
    open: int
    soft_limit: int


__all__ = ["APP_DESCRIPTORS", "APP_EXITED", "APP_STARTED", "AppDescriptorsValue", "AppRunValue",
           "RunDocument"]
