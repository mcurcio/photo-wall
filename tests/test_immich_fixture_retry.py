"""A transient docker pull/start hiccup must not fail the disposable fixture.

`FixtureHost.pull`/`FixtureHost.start` retry a bounded number of times with
increasing backoff, cleaning up any partial container between attempts. This
covers both the pure retry/backoff/cleanup logic (`call_with_retry`, no
docker involved) and its wiring into `FixtureHost` (a fake `docker` on PATH,
mirroring the pattern in test_docker_diagnostics.py, so no real docker is
required).
"""

from __future__ import annotations

import os

import pytest

from scripts.harness_failure import CodedFailure
from scripts.immich_fixture import (
    STARTUP_RETRY_ATTEMPTS,
    STARTUP_RETRY_BACKOFF_SECONDS,
    FixtureHost,
    HarnessError,
    call_with_retry,
)


class _Recorder:
    """Captures call order across action/cleanup/sleep for ordering assertions."""

    def __init__(self):
        self.events: list[str] = []

    def cleanup(self) -> None:
        self.events.append("cleanup")

    def sleep(self, seconds: float) -> None:
        self.events.append(f"sleep:{seconds}")


def test_call_with_retry_recovers_from_transient_failure_and_retries():
    recorder = _Recorder()
    calls = {"count": 0}

    def action():
        calls["count"] += 1
        recorder.events.append(f"action:{calls['count']}")
        if calls["count"] < 3:
            raise HarnessError("docker_command_failed")
        return "started"

    result = call_with_retry(
        action, attempts=STARTUP_RETRY_ATTEMPTS, backoff_seconds=STARTUP_RETRY_BACKOFF_SECONDS,
        cleanup=recorder.cleanup, retryable=lambda error: isinstance(error, HarnessError),
        sleep=recorder.sleep,
    )

    assert result == "started"
    assert calls["count"] == 3
    # Cleanup and backoff run strictly between attempts: never before the
    # first action, never after the final (successful) one.
    assert recorder.events == [
        "action:1", "cleanup", f"sleep:{STARTUP_RETRY_BACKOFF_SECONDS[0]}",
        "action:2", "cleanup", f"sleep:{STARTUP_RETRY_BACKOFF_SECONDS[1]}",
        "action:3",
    ]


def test_call_with_retry_backoff_increases_and_is_bounded_by_the_schedule():
    recorder = _Recorder()

    def action():
        raise HarnessError("docker_command_failed")

    with pytest.raises(HarnessError):
        call_with_retry(
            action, attempts=STARTUP_RETRY_ATTEMPTS, backoff_seconds=STARTUP_RETRY_BACKOFF_SECONDS,
            cleanup=recorder.cleanup, retryable=lambda error: isinstance(error, HarnessError),
            sleep=recorder.sleep,
        )

    sleeps = [event for event in recorder.events if event.startswith("sleep:")]
    assert sleeps == [f"sleep:{value}" for value in STARTUP_RETRY_BACKOFF_SECONDS]


def test_call_with_retry_exhausts_bounded_attempts_with_informative_last_error():
    calls = {"count": 0}
    cleanups = {"count": 0}

    def action():
        calls["count"] += 1
        # A permanent failure looks identical to a transient one at this
        # layer (both raise the same closed docker_command_failed code); the
        # bounded attempt count is what keeps this from hanging CI, and the
        # attached diagnostic is what makes the final error more than a bare
        # code (see FixtureHost._coded_failure).
        error = HarnessError("docker_command_failed")
        error.diagnostic = f"real stderr from attempt {calls['count']}: bad image reference".encode()
        raise error

    with pytest.raises(HarnessError) as caught:
        call_with_retry(
            action, attempts=STARTUP_RETRY_ATTEMPTS, backoff_seconds=STARTUP_RETRY_BACKOFF_SECONDS,
            cleanup=lambda: cleanups.__setitem__("count", cleanups["count"] + 1),
            retryable=lambda error: isinstance(error, HarnessError), sleep=lambda seconds: None,
        )

    assert calls["count"] == STARTUP_RETRY_ATTEMPTS
    assert cleanups["count"] == STARTUP_RETRY_ATTEMPTS - 1
    # The bare public code stays closed/coded...
    assert str(caught.value) == "docker_command_failed"
    # ...but the caller can still get at real, non-bare diagnosis of the very
    # last attempt, not a generic placeholder.
    assert caught.value.diagnostic == (
        f"real stderr from attempt {STARTUP_RETRY_ATTEMPTS}: bad image reference".encode()
    )


def test_call_with_retry_does_not_retry_a_non_retryable_error():
    calls = {"count": 0}
    cleanups = {"count": 0}

    def action():
        calls["count"] += 1
        raise HarnessError("fixture_containers_missing")

    with pytest.raises(HarnessError, match="fixture_containers_missing"):
        call_with_retry(
            action, attempts=STARTUP_RETRY_ATTEMPTS, backoff_seconds=STARTUP_RETRY_BACKOFF_SECONDS,
            cleanup=lambda: cleanups.__setitem__("count", cleanups["count"] + 1),
            retryable=lambda error: isinstance(error, HarnessError) and str(error) == "docker_command_failed",
            sleep=lambda seconds: None,
        )

    assert calls["count"] == 1
    assert cleanups["count"] == 0


def docker_script(path, body):
    executable = path / "docker"
    executable.write_text("#!/bin/sh\n" + body)
    executable.chmod(0o755)


# A fake `docker` recognizes only its own compose subcommand (matched as a
# whole argument, never a substring of a state path) and looks at a
# per-subcommand counter file to decide whether this attempt should still
# fail. This is the same "fake executable on PATH" mocking style already
# used by test_docker_diagnostics.py, so FixtureHost needs no injected
# runner just to be testable.
_FAKE_DOCKER = """
echo "$@" >> "$CALLS_LOG"
op=""
for arg in "$@"; do
  case "$arg" in
    pull) op=pull ;;
    up) op=up ;;
    down) op=down ;;
  esac
done
case "$op" in
  pull)
    count=$(( $(cat "$PULL_COUNT" 2>/dev/null || echo 0) + 1 ))
    echo "$count" > "$PULL_COUNT"
    if [ "$count" -le "${PULL_FAIL_UNTIL:-0}" ]; then
      echo "pull failed: registry hiccup, dial tcp timeout" >&2
      exit 1
    fi
    ;;
  up)
    count=$(( $(cat "$UP_COUNT" 2>/dev/null || echo 0) + 1 ))
    echo "$count" > "$UP_COUNT"
    if [ "$count" -le "${UP_FAIL_UNTIL:-0}" ]; then
      echo "up failed: container did not become healthy in time" >&2
      exit 1
    fi
    ;;
esac
exit 0
"""


def _prepare_fixture_host(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    (tmp_path / "bin").mkdir()
    docker_script(tmp_path / "bin", _FAKE_DOCKER)
    monkeypatch.setenv("CALLS_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("PULL_COUNT", str(tmp_path / "pull.count"))
    monkeypatch.setenv("UP_COUNT", str(tmp_path / "up.count"))
    return FixtureHost.create(tmp_path / "state")


def test_fixture_host_start_retries_a_transient_failure_then_succeeds(tmp_path, monkeypatch):
    host = _prepare_fixture_host(tmp_path, monkeypatch)
    monkeypatch.setenv("UP_FAIL_UNTIL", "2")  # fails attempts 1 and 2, succeeds on 3

    host.start(wait_timeout=5, timeout=10, sleep=lambda seconds: None)

    assert int((tmp_path / "up.count").read_text()) == 3
    calls = (tmp_path / "calls.log").read_text().splitlines()
    up_calls = [line for line in calls if " up " in f" {line} "]
    down_calls = [line for line in calls if " down " in f" {line} "]
    # A partial container from each failed attempt is torn down before the
    # next `up`: exactly one down per retry, none after the final success.
    assert len(up_calls) == 3
    assert len(down_calls) == 2


def test_fixture_host_pull_fails_after_bounded_retries_with_informative_diagnostic(
        tmp_path, monkeypatch):
    host = _prepare_fixture_host(tmp_path, monkeypatch)
    monkeypatch.setenv("PULL_FAIL_UNTIL", "999")  # never recovers: a permanent failure

    with pytest.raises(HarnessError) as caught:
        host.pull("immich", "database", "redis", timeout=10, sleep=lambda seconds: None)

    assert isinstance(caught.value, CodedFailure)
    assert str(caught.value) == "docker_command_failed"
    assert int((tmp_path / "pull.count").read_text()) == STARTUP_RETRY_ATTEMPTS
    # Not a bare code: the last attempt's real (sanitized, bounded) stderr
    # rides along on the exception for anyone who needs to diagnose it.
    assert b"registry hiccup" in caught.value.diagnostic

    calls = (tmp_path / "calls.log").read_text().splitlines()
    down_calls = [line for line in calls if " down " in f" {line} "]
    assert len(down_calls) == STARTUP_RETRY_ATTEMPTS - 1
