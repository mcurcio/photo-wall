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

    stderr: dict[subprocess.Popen, Path] = {}

    def spawn() -> subprocess.Popen:
        # Files, not PIPEs: nothing drains a pipe while the test polls, and a hang needs the log.
        name = tmp_path / f"worker-{len(stderr)}"
        with name.with_suffix(".out").open("w") as out, name.with_suffix(".err").open("w") as err:
            process = subprocess.Popen([sys.executable, "-m", "media.worker"], cwd=ROOT, env=env,
                                       stdout=out, stderr=err, text=True)
        stderr[process] = name.with_suffix(".err")
        return process

    def tail(process: subprocess.Popen) -> str:
        return "stderr tail:\n" + "\n".join(stderr[process].read_text().splitlines()[-40:])

    def stops_on_sigterm(process: subprocess.Popen) -> None:
        process.send_signal(signal.SIGTERM)
        try:
            code = process.wait(30)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"worker still running 30s after SIGTERM; {tail(process)}") \
                from None
        assert code == 0, tail(process)

    first = spawn()
    second = None
    try:
        wait_for(lambda: live_workers(registry.db) == JOB_LOOPS + MEDIA_LOOP or
                 first.poll() is not None, seconds=60, what="the first worker")
        assert first.poll() is None, tail(first)
        second = spawn()
        # Before: the second process exited `media_writer_active` without running any job loop.
        wait_for(lambda: live_workers(registry.db) == 2 * JOB_LOOPS + MEDIA_LOOP or
                 second.poll() is not None, seconds=60, what="the second worker")
        assert second.poll() is None, tail(second)
        assert live_workers(registry.db) == 2 * JOB_LOOPS + MEDIA_LOOP  # media: one writer only

        stops_on_sigterm(first)
        # The standby takes over the media loop once the writer exits.
        wait_for(lambda: live_workers(registry.db) == JOB_LOOPS + MEDIA_LOOP, seconds=30,
                 what="the media takeover")
        assert second.poll() is None, tail(second)
        stops_on_sigterm(second)
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
