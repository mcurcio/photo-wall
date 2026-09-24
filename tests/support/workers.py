"""Real `python -m media.worker` processes under test: their environment, counting the live ones,
and polling for a condition.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from central.infra.runtime import STALLED_WORKER_SECONDS

JOB_LOOPS = 2  # the runtime's loops per process: fetch and upkeep
MEDIA_LOOP = 1  # the legacy media loop: one per fleet (the media writer lock)


def fake_media_tools(directory: Path) -> Path:
    """`ffmpeg`/`ffprobe` stand-ins so worker boot does not depend on the host's media tools.

    The real `Preparer` resolves both on PATH at construction and runs `-version` for the recipe
    identity at media-loop boot; nothing here prepares media. Same fake-executable-on-PATH style
    as test_docker_diagnostics.py. They come first on PATH, so the test runs the same with or
    without real tools installed (CI has none).
    """
    directory.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        tool = directory / name
        tool.write_text(f"#!/bin/sh\necho '{name} version 0-test-stub'\n")
        tool.chmod(0o755)
    return directory


def worker_env(root: Path, dsn: str, **extra: str) -> dict[str, str]:
    """The environment a worker (and Central) process boots with, rooted at `root`.

    Creates `root/cache` (the one shared cache root), an empty `root/connections.json` (0600, as
    the loader requires) and the media-tool stand-ins in `root/bin`, first on PATH. `extra`
    overrides or adds variables.
    """
    (root / "cache").mkdir()
    connections = root / "connections.json"
    connections.write_text(json.dumps({"schema": 1, "connections": []}))
    connections.chmod(0o600)
    return {**os.environ, "PHOTO_WALL_DATABASE_URL": dsn,
            "PHOTO_WALL_CONNECTIONS_FILE": str(connections),
            "PHOTO_WALL_CACHE_ROOT": str(root / "cache"),
            "PATH": f"{fake_media_tools(root / 'bin')}{os.pathsep}{os.environ.get('PATH', '')}",
            **extra}


def live_worker_ids(db) -> set[int]:
    """The procrastinate worker loops that are alive: a heartbeat fresher than the stalled
    timeout. A killed process's rows linger until pruned; they are not counted."""
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id FROM procrastinate_workers "
            "WHERE last_heartbeat > now() - make_interval(secs => %s)",
            (STALLED_WORKER_SECONDS,)).fetchall()
    return {row["id"] for row in rows}


def live_workers(db) -> int:
    return len(live_worker_ids(db))


def wait_for(predicate: Callable[[], object], *, seconds: float, what: str,
             interval: float = 0.1) -> float:
    """Poll `predicate` until truthy; the seconds it took. AssertionError on timeout."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if predicate():
            return time.monotonic() - start
        time.sleep(interval)
    raise AssertionError(f"timed out after {seconds}s waiting for {what}")
