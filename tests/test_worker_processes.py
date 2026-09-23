"""Design §2: one kind of worker, 1+ identical processes on a shared cache disk.

Two real `python -m media.worker` processes share one cache root and one database schema. Both
must run the job runtime; only the legacy media loop is single-writer (the media lock), and the
second process stands by for it instead of exiting. PostgreSQL (`registry`) is required.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from runtime_fakes import apply_procrastinate_schema

ROOT = Path(__file__).resolve().parents[1]
JOB_LOOPS = 2  # the runtime's loops per process: fetch and upkeep
MEDIA_LOOP = 1


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


def workers(db) -> int:
    with db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM procrastinate_workers").fetchone()["n"]


def wait_for(predicate, *, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_two_worker_processes_on_one_cache_root_both_run_the_job_runtime(registry, tmp_path):
    apply_procrastinate_schema(registry.db.dsn)  # the counts below read its tables at once
    (tmp_path / "cache").mkdir()
    connections = tmp_path / "connections.json"
    connections.write_text(json.dumps({"schema": 1, "connections": []}))
    connections.chmod(0o600)
    env = {**os.environ, "PHOTO_WALL_DATABASE_URL": registry.db.dsn,
           "PHOTO_WALL_CONNECTIONS_FILE": str(connections),
           "PHOTO_WALL_CACHE_ROOT": str(tmp_path / "cache"),
           "PHOTO_WALL_RELEASE_REPO": "example.invalid/none",
           "PATH": f"{fake_media_tools(tmp_path / 'bin')}{os.pathsep}{os.environ.get('PATH', '')}"}

    def spawn() -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-m", "media.worker"], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    first = spawn()
    second = None
    try:
        wait_for(lambda: workers(registry.db) == JOB_LOOPS + MEDIA_LOOP or
                 first.poll() is not None, seconds=60, what="the first worker")
        assert first.poll() is None, first.communicate()
        second = spawn()
        # Before: the second process exited `media_writer_active` without running any job loop.
        wait_for(lambda: workers(registry.db) == 2 * JOB_LOOPS + MEDIA_LOOP or
                 second.poll() is not None, seconds=60, what="the second worker")
        assert second.poll() is None, second.communicate()
        assert workers(registry.db) == 2 * JOB_LOOPS + MEDIA_LOOP  # media: one writer only

        first.send_signal(signal.SIGTERM)
        assert first.wait(30) == 0, first.communicate()
        # The standby takes over the media loop once the writer exits.
        wait_for(lambda: workers(registry.db) == JOB_LOOPS + MEDIA_LOOP, seconds=30,
                 what="the media takeover")
        assert second.poll() is None
        second.send_signal(signal.SIGTERM)
        assert second.wait(30) == 0, second.communicate()
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
