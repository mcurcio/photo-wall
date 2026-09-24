"""Design §2: one kind of worker, 1+ identical processes on a shared cache disk.

Two real `python -m media.worker` processes share one cache root and one database schema. Both
must run the job runtime; only the legacy media loop is single-writer (the media lock), and the
second process stands by for it instead of exiting. PostgreSQL (`registry`) is required.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

from runtime_fakes import apply_procrastinate_schema
from support.workers import JOB_LOOPS, MEDIA_LOOP, live_workers, wait_for, worker_env

ROOT = Path(__file__).resolve().parents[1]


def test_two_worker_processes_on_one_cache_root_both_run_the_job_runtime(registry, tmp_path):
    apply_procrastinate_schema(registry.db.dsn)  # the counts below read its tables at once
    env = worker_env(tmp_path, registry.db.dsn, PHOTO_WALL_RELEASE_REPO="example.invalid/none")

    def spawn() -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-m", "media.worker"], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    first = spawn()
    second = None
    try:
        wait_for(lambda: live_workers(registry.db) == JOB_LOOPS + MEDIA_LOOP or
                 first.poll() is not None, seconds=60, what="the first worker")
        assert first.poll() is None, first.communicate()
        second = spawn()
        # Before: the second process exited `media_writer_active` without running any job loop.
        wait_for(lambda: live_workers(registry.db) == 2 * JOB_LOOPS + MEDIA_LOOP or
                 second.poll() is not None, seconds=60, what="the second worker")
        assert second.poll() is None, second.communicate()
        assert live_workers(registry.db) == 2 * JOB_LOOPS + MEDIA_LOOP  # media: one writer only

        first.send_signal(signal.SIGTERM)
        assert first.wait(30) == 0, first.communicate()
        # The standby takes over the media loop once the writer exits.
        wait_for(lambda: live_workers(registry.db) == JOB_LOOPS + MEDIA_LOOP, seconds=30,
                 what="the media takeover")
        assert second.poll() is None
        second.send_signal(signal.SIGTERM)
        assert second.wait(30) == 0, second.communicate()
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
