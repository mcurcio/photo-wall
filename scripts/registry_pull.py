"""Every registry image pull goes through here: one image at a time, retried only when the
registry or the network says the failure is transient.

An image already present locally is never pulled again: a digest-pinned reference names its
content, so `docker image inspect` finding it (on the requested platform) is the whole answer.
A tag can move, so a tag is always pulled. A failed pull is classified from its stderr: a
throttle (429, `toomanyrequests`), a registry 5xx or a network fault is retried with
exponential backoff and full jitter (a larger `retry-after` wins), until the time budget is
spent; anything else (manifest unknown, not found, unauthorized, denied) fails at once. Docker
keeps the layers an attempt already fetched, so a retry resumes rather than restarts.

The public failure is a closed code; the sanitized stderr rides on the exception's
`diagnostic` and in the Docker debug log, never in what CI prints. Stdlib only.
"""

from __future__ import annotations

import random as _random
import re
import subprocess
import time
from collections.abc import Callable, Iterable

from scripts.docker_diagnostics import bounded_diagnostic, docker_debug_args, record_docker_debug

BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 30.0
RETRY_BUDGET_SECONDS = 180.0
PULL_TIMEOUT_SECONDS = 1200
INSPECT_TIMEOUT_SECONDS = 60

_TRANSIENT = re.compile(
    rb"(?i)toomanyrequests|too many requests|\b429\b"
    rb"|received unexpected HTTP status: 5\d\d|\b50[0234]\b"
    rb"|TLS handshake timeout|i/o timeout|connection reset|unexpected EOF"
    rb"|context deadline exceeded"
)
_RETRY_AFTER = re.compile(rb"(?i)retry-after:\s*~?\s*([0-9][0-9.a-z\xc2\xb5]*)")
_DURATION_PART = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ns|us|\xb5s|ms|s|m|h)")
_UNIT_SECONDS = {"ns": 1e-9, "us": 1e-6, "\xb5s": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0,
                 "h": 3600.0}
_DIGEST_PINNED = re.compile(r"@sha256:[a-f0-9]{64}$")


class RegistryPullError(RuntimeError):
    """`code` is `registry_pull_failed` (permanent) or `registry_pull_unavailable` (budget spent);
    the str is the code alone. A plain exception, so a release build (scripts.service_base) runs
    this module without the acceptance harness's failure vocabulary."""

    def __init__(self, code: str, diagnostic: bytes):
        super().__init__(code)
        self.code, self.diagnostic = code, diagnostic


def transient(stderr: bytes) -> bool:
    """A failure the registry or network will clear by itself; the rest are permanent."""
    return _TRANSIENT.search(stderr) is not None


def retry_after(stderr: bytes) -> float:
    """The registry's `retry-after` in seconds (plain seconds or a Go duration), else 0."""
    match = _RETRY_AFTER.search(stderr)
    if match is None:
        return 0.0
    token = match[1].decode(errors="replace").rstrip(".,")
    try:
        return float(token)
    except ValueError:
        parts = _DURATION_PART.findall(token)
        return sum(float(value) * _UNIT_SECONDS[unit] for value, unit in parts)


def backoff(attempt: int, random: Callable[[], float]) -> float:
    """Full jitter: uniform in [0, min(cap, base * 2**attempt)) for the 0-based attempt."""
    return random() * min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** attempt)


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(docker_debug_args(args), stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=timeout, check=False)


def _present(ref: str, platform: str | None, run, timeout: float) -> bool:
    if _DIGEST_PINNED.search(ref) is None:
        return False
    try:
        result = run(["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}",
                      ref], timeout)
    except subprocess.TimeoutExpired:
        return False
    if result.returncode:
        return False
    return platform is None or result.stdout.decode(errors="replace").strip() == platform


def _failure(code: str, stderr: bytes) -> RegistryPullError:
    return RegistryPullError(code, bounded_diagnostic(stderr))


def pull_image(ref: str, *, platform: str | None = None,
               budget: float = RETRY_BUDGET_SECONDS, timeout: float = PULL_TIMEOUT_SECONDS,
               run=_run, sleep=time.sleep, clock=time.monotonic,
               random=_random.random) -> bool:
    """Make `ref` present locally; True if it had to be pulled, False if it already was."""
    if _present(ref, platform, run, INSPECT_TIMEOUT_SECONDS):
        return False
    args = ["docker", "pull", *(("--platform", platform) if platform else ()), ref]
    deadline = clock() + budget
    attempt = 0
    while True:
        try:
            result = run(args, timeout)
            code, stderr = result.returncode, result.stderr or b""
        except subprocess.TimeoutExpired as expired:
            code, stderr = -1, (expired.stderr or b"") + b"\ncommand timeout"
        if code == 0:
            return True
        record_docker_debug(args, code, b"stderr:\n" + stderr)
        if code != -1 and not transient(stderr):
            raise _failure("registry_pull_failed", stderr)
        left = deadline - clock()
        if left <= 0:
            raise _failure("registry_pull_unavailable", stderr)
        sleep(min(left, max(backoff(attempt, random), retry_after(stderr))))
        attempt += 1


def pull_images(refs: Iterable[str], **options) -> list[str]:
    """Pull each of `refs` in turn (see pull_image); returns the ones actually pulled."""
    return [ref for ref in refs if pull_image(ref, **options)]

