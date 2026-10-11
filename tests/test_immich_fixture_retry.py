"""Docker hiccups must not fail the disposable fixture, and a permanent fault must fail fast.

Registry pulls go through `scripts.registry_pull` (one image at a time, already-present images
skipped, transient registry/network failures retried with jittered backoff inside a time budget,
permanent ones failed at once); `FixtureHost.start` keeps a bounded retry for container-start
hiccups. Both run against a fake `docker` on PATH, mirroring test_docker_diagnostics.py, with
sleep, clock and random injected so nothing really waits.
"""

from __future__ import annotations

import os
import re

import pytest

from scripts.harness_failure import CodedFailure
from scripts.immich_fixture import ROOT, FixtureHost, HarnessError, fixture_images
from scripts.registry_pull import RegistryPullError, pull_image

REF = "ghcr.io/example/image:v1@sha256:" + "a" * 64
THROTTLED = "toomanyrequests: retry-after: 1ms, allowed: 44000/minute"
UNAVAILABLE = "received unexpected HTTP status: 503 Service Unavailable"
MISSING = "manifest unknown: manifest unknown"


def docker_script(path, body):
    executable = path / "docker"
    executable.write_text("#!/bin/sh\n" + body)
    executable.chmod(0o755)


# A fake `docker`: `image inspect` answers from $INSPECT_OUTPUT (unset: the image is absent),
# `pull` fails with $PULL_STDERR until its counter passes $PULL_FAIL_UNTIL, and compose `up`
# fails until $UP_FAIL_UNTIL. Every call is logged, so the tests see exactly what ran.
_FAKE_DOCKER = """
echo "$@" >> "$CALLS_LOG"
if [ "$1" = image ] && [ "$2" = inspect ]; then
  if [ -n "$INSPECT_OUTPUT" ]; then echo "$INSPECT_OUTPUT"; exit 0; fi
  echo "Error: No such image" >&2
  exit 1
fi
if [ "$1" = pull ]; then
  count=$(( $(cat "$PULL_COUNT" 2>/dev/null || echo 0) + 1 ))
  echo "$count" > "$PULL_COUNT"
  if [ "$count" -le "${PULL_FAIL_UNTIL:-0}" ]; then
    echo "Error response from daemon: $PULL_STDERR" >&2
    exit 1
  fi
  exit 0
fi
for arg in "$@"; do
  if [ "$arg" = up ]; then
    count=$(( $(cat "$UP_COUNT" 2>/dev/null || echo 0) + 1 ))
    echo "$count" > "$UP_COUNT"
    if [ "$count" -le "${UP_FAIL_UNTIL:-0}" ]; then
      echo "up failed: container did not become healthy in time" >&2
      exit 1
    fi
  fi
done
exit 0
"""


@pytest.fixture
def docker(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    docker_script(tmp_path / "bin", _FAKE_DOCKER)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CALLS_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("PULL_COUNT", str(tmp_path / "pull.count"))
    monkeypatch.setenv("UP_COUNT", str(tmp_path / "up.count"))
    monkeypatch.delenv("INSPECT_OUTPUT", raising=False)

    def calls(command):
        log = tmp_path / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.startswith(command)]
    return calls


class _Time:
    """A clock that only `sleep` advances, recording every sleep."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _pull(ref=REF, time=None, **options):
    time = time or _Time()
    return pull_image(ref, sleep=time.sleep, clock=time.clock, random=lambda: 1.0, **options)


@pytest.mark.parametrize("stderr", [THROTTLED, UNAVAILABLE, "net/http: TLS handshake timeout",
                                    "read tcp 10.0.0.1:443: connection reset by peer"])
def test_a_transient_registry_failure_is_retried_with_backoff_until_the_pull_succeeds(
        docker, monkeypatch, stderr):
    monkeypatch.setenv("PULL_STDERR", stderr)
    monkeypatch.setenv("PULL_FAIL_UNTIL", "2")
    time = _Time()

    assert _pull(time=time) is True

    assert docker("pull") == [f"pull {REF}"] * 3
    assert time.sleeps == [2.0, 4.0]  # full jitter at its top (random=1): base * 2**attempt


def test_a_larger_retry_after_from_the_registry_wins_over_the_backoff(docker, monkeypatch):
    monkeypatch.setenv("PULL_STDERR", "toomanyrequests: retry-after: 1m30s")
    monkeypatch.setenv("PULL_FAIL_UNTIL", "1")
    time = _Time()

    _pull(time=time)

    assert time.sleeps == [90.0]


def test_a_permanent_registry_failure_fails_at_once_with_a_bounded_code(docker, monkeypatch):
    monkeypatch.setenv("PULL_STDERR", MISSING)
    monkeypatch.setenv("PULL_FAIL_UNTIL", "999")
    time = _Time()

    with pytest.raises(RegistryPullError) as caught:
        _pull(time=time)

    assert str(caught.value) == "registry_pull_failed"
    assert docker("pull") == [f"pull {REF}"]
    assert time.sleeps == []
    assert b"manifest unknown" in caught.value.diagnostic


def test_a_transient_failure_that_never_clears_fails_once_the_time_budget_is_spent(
        docker, monkeypatch):
    monkeypatch.setenv("PULL_STDERR", THROTTLED)
    monkeypatch.setenv("PULL_FAIL_UNTIL", "999")
    time = _Time()

    with pytest.raises(RegistryPullError) as caught:
        _pull(time=time, budget=60.0)

    assert str(caught.value) == "registry_pull_unavailable"
    assert sum(time.sleeps) == 60.0  # never sleeps past the budget
    assert time.sleeps == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert len(docker("pull")) == 6
    assert b"toomanyrequests" in caught.value.diagnostic


def test_an_image_already_present_is_not_pulled(docker, monkeypatch):
    monkeypatch.setenv("INSPECT_OUTPUT", "linux/arm64")

    assert _pull(platform="linux/arm64") is False

    assert docker("pull") == []


def test_a_present_image_of_another_platform_or_a_tag_is_still_pulled(docker, monkeypatch):
    monkeypatch.setenv("INSPECT_OUTPUT", "linux/arm64")

    assert _pull(platform="linux/amd64") is True
    assert _pull("ghcr.io/example/image:moving-tag") is True

    assert docker("pull") == [f"pull --platform linux/amd64 {REF}",
                              "pull ghcr.io/example/image:moving-tag"]


def test_fixture_pull_fetches_the_compose_files_digest_pinned_images_one_at_a_time(
        docker, tmp_path, monkeypatch):
    host = FixtureHost.create(tmp_path / "state")
    monkeypatch.setenv("PULL_STDERR", THROTTLED)
    monkeypatch.setenv("PULL_FAIL_UNTIL", "1")
    time = _Time()

    host.pull("immich", "database", "redis", sleep=time.sleep, clock=time.clock)

    compose = (ROOT / "tests/integration/compose.immich.yml").read_text()
    refs = fixture_images("immich", "database", "redis")
    assert [ref.split("@")[0].rsplit(":", 1)[0] for ref in refs] == [
        "ghcr.io/immich-app/immich-server", "ghcr.io/immich-app/postgres",
        "docker.io/valkey/valkey"]
    for ref in refs:
        assert re.search(r"@sha256:[a-f0-9]{64}$", ref) and f"image: {ref}\n" in compose
    assert docker("pull") == [f"pull {refs[0]}", *(f"pull {ref}" for ref in refs)]
    assert len(time.sleeps) == 1


def test_fixture_pull_reports_a_permanent_failure_as_a_bounded_harness_code(
        docker, tmp_path, monkeypatch):
    host = FixtureHost.create(tmp_path / "state")
    monkeypatch.setenv("PULL_STDERR", MISSING)
    monkeypatch.setenv("PULL_FAIL_UNTIL", "999")

    with pytest.raises(HarnessError) as caught:
        host.pull("immich", sleep=lambda seconds: None)

    assert isinstance(caught.value, CodedFailure)
    assert str(caught.value) == "registry_pull_failed"
    assert b"manifest unknown" in caught.value.diagnostic
    assert len(docker("pull")) == 1


def test_fixture_start_retries_a_transient_failure_then_succeeds(docker, tmp_path, monkeypatch):
    host = FixtureHost.create(tmp_path / "state")
    monkeypatch.setenv("UP_FAIL_UNTIL", "2")  # fails attempts 1 and 2, succeeds on 3

    host.start(wait_timeout=5, timeout=10, sleep=lambda seconds: None)

    up_calls = [line for line in docker("compose") if " up " in f" {line} "]
    down_calls = [line for line in docker("compose") if " down " in f" {line} "]
    # A partial container from each failed attempt is torn down before the
    # next `up`: exactly one down per retry, none after the final success.
    assert len(up_calls) == 3
    assert len(down_calls) == 2


class _RecordingHost:
    """A FixtureHost stand-in recording what `run_fixture` asks of the disposable upstream."""

    def __init__(self, state):
        self.state, self.roles, self.composed = state, [], []
        state.mkdir()

    def _command(self, args, *, timeout, capture):
        return "0" * 40 if "rev-parse" in args else ""

    def build(self, *, base_image=None):
        pass

    def pull(self, *services):
        pass

    def start(self, **_):
        pass

    def topology(self):
        return {}, "192.0.2.1"

    def compose(self, *args, **_):
        self.composed.append(args[:2])
        return "{}"

    def probe(self, upstream_ip):
        return {}

    def role(self, role, action):
        self.roles.append((role, action))
        return {}

    def export_runtime(self):
        self.composed.append(("cp", "runtime"))

    def publish(self):
        self.composed.append(("publish",))
        return {"base_url": "http://127.0.0.1:1/api"}

    def cleanup(self):
        pass


@pytest.mark.parametrize("setup_only", [False, True])
@pytest.mark.parametrize("publish", [False, True])
def test_every_run_sets_up_the_content_set_and_only_publish_hands_it_to_this_host(
        tmp_path, monkeypatch, setup_only, publish):
    import scripts.immich_fixture as fixture

    hosts = []
    monkeypatch.setattr(fixture.FixtureHost, "create",
                        classmethod(lambda cls, state, publish: hosts.append(
                            _RecordingHost(state)) or hosts[-1]))
    result = fixture.run_fixture(tmp_path / "state", True, setup_only=setup_only,
                                 publish=publish)
    assert result["result"] == "passed" and result["published"] is publish
    (host,) = hosts
    assert host.roles == [("setup", "initialize")]
    assert ("stop", "immich") not in host.composed
    assert (("publish",) in host.composed) is publish
    if publish:  # the runtime state is on this host before the admin session joins it
        assert host.composed.index(("cp", "runtime")) < host.composed.index(("publish",))
    assert not hasattr(fixture, "ADAPTER_ROLES")


def test_the_published_topology_allows_exactly_one_loopback_binding(tmp_path, monkeypatch):
    """Under --publish only Immich may bind, on 127.0.0.1 and nowhere else."""
    import json as _json
    import subprocess as _subprocess

    import scripts.immich_fixture as fixture

    def inspected(host_ip):
        def container(service, networks, bindings):
            return {"Config": {"Labels": {"com.docker.compose.service": service}},
                    "NetworkSettings": {"Networks": {
                        f"{project}_{name}": {"IPAddress": "192.0.2.7"} for name in networks}},
                    "HostConfig": {"PortBindings": bindings,
                                   "Sysctls": {"net.ipv4.ip_forward": "0"}},
                    "Image": "sha256:" + "a" * 64}
        return [container("immich", ["upstream_net", "published_net"],
                          {"2283/tcp": [{"HostIp": host_ip, "HostPort": ""}]}),
                container("central-probe", ["upstream_net", "wall_net"], None)]

    host = FixtureHost.create(tmp_path / "state", publish=True)
    project = host.project
    assert str(fixture.PUBLISHED_COMPOSE_FILE) in host.base
    for host_ip, passes in (("127.0.0.1", True), ("0.0.0.0", False)):
        def run(args, **_):
            output = (_json.dumps(inspected(host_ip)) if args[:2] == ["docker", "inspect"]
                      else _json.dumps([{"Id": "sha256:" + "a" * 64, "Os": "linux",
                                         "Architecture": "arm64"}]))
            return _subprocess.CompletedProcess(args, 0, stdout=output)

        monkeypatch.setattr(host, "compose", lambda *args, **_: "c1 c2")
        monkeypatch.setattr(fixture.subprocess, "run", run)
        if passes:
            inventory, address = host.topology()
            assert inventory["immich"]["networks"] == ["published_net", "upstream_net"]
            assert address == "192.0.2.7"
        else:
            with pytest.raises(HarnessError, match="upstream_host_port_exposed"):
                host.topology()
    unpublished = FixtureHost.create(tmp_path / "plain")
    assert str(fixture.PUBLISHED_COMPOSE_FILE) not in unpublished.base
