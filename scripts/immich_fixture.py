"""Disposable real-Immich harness; generated fixture media may be freely redistributed.

No existing Immich instance is accepted. Private credentials stay in a new state
directory and container roles receive only the mount needed for their function.

`run --publish` also publishes Immich on an ephemeral 127.0.0.1 port for the named tests on
this host: the state directory then holds `published.json` (the URL), `runtime/library.json`
(the content set and its keys) and `runtime/admin.json` (the admin session), all synthetic.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import selectors
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.docker_diagnostics import (  # noqa: E402
    MAX_DOCKER_DEBUG_ENTRY,
    bounded_diagnostic,
    docker_debug_args,
    record_docker_debug,
)
from scripts.harness_bundle import IMMICH_RUNTIME_BUNDLE  # noqa: E402
from scripts.harness_failure import (  # noqa: E402
    CodedFailure,
    FailureAction,
    FailureEnvelope,
    FailureRole,
)
from scripts.registry_pull import RegistryPullError, pull_images  # noqa: E402

MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
TERMINATE_GRACE = 5.0

COMPOSE_FILE = ROOT / "tests/integration/compose.immich.yml"
PUBLISHED_COMPOSE_FILE = ROOT / "tests/integration/compose.immich-published.yml"
# The one host binding `--publish` allows: Immich's API on an ephemeral loopback port.
PUBLISHED_PORT = "2283/tcp"
# The upstream services whose images come from a registry (the rest build locally).
UPSTREAM_SERVICES = ("immich", "database", "redis")

# Container start is exposed to daemon flakiness (registry pulls retry in
# scripts.registry_pull). Bounding the retry count keeps a truly permanent
# failure (unfixable config) from hanging CI, while still absorbing the
# transient hiccup that clears on a human re-run. This layer cannot reliably
# tell the two apart -- both surface as the same closed
# `docker_command_failed`/`docker_command_timeout` code -- so retrying a
# bounded few times regardless is treated as safe for a fixture that is torn
# down and rebuilt from scratch on every attempt anyway.
STARTUP_RETRY_ATTEMPTS = 4
STARTUP_RETRY_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
_TRANSIENT_DOCKER_CODES = ("docker_command_failed", "docker_command_timeout")


class _Tail:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.data = bytearray()
        self.truncated = False

    def add(self, block: bytes) -> None:
        self.data.extend(block)
        if len(self.data) > self.maximum:
            self.truncated = True
            del self.data[:-self.maximum]

    def diagnostic(self) -> bytes:
        return bounded_diagnostic(bytes(self.data), truncated=self.truncated)

# This isolated negative probe is test-driver code, never Player application code
# or configuration. It deliberately knows the denied target supplied by the host.
PROBE_CODE = """
import json, socket, sys, urllib.request
ip = sys.argv[1]
try:
    socket.getaddrinfo('immich', 2283)
    dns_denied = False
except socket.gaierror:
    dns_denied = True
try:
    with socket.create_connection((ip, 2283), timeout=2):
        tcp_denied = False
except OSError:
    tcp_denied = True
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with client.open('http://central-probe:8000/healthz', timeout=3) as response:
    central_ok = response.status == 200 and response.read() == b'{"ok":true}'
result = dict(dns_denied=dns_denied, numeric_tcp_denied=tcp_denied,
              central_health_reachable=central_ok)
print(json.dumps(result))
sys.exit(0 if all(result.values()) else 1)
"""


class HarnessError(CodedFailure):
    """Only a bounded code may be printed; raw upstream errors stay private."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise HarnessError(code)


def call_with_retry(action, *, attempts: int, backoff_seconds: tuple[float, ...],
                    cleanup, retryable, sleep=time.sleep):
    """Run `action`, retrying a bounded number of times with increasing backoff.

    `cleanup()` runs before every retry (never before the first attempt, and
    never after the last) so a partial container from the failed attempt
    cannot collide with the next one by name. A non-retryable error, and the
    final attempt's error regardless of retryability, always propagate
    unmodified -- the caller keeps whatever diagnosable detail it carries.
    """
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as error:
            if attempt == attempts or not retryable(error):
                raise
            cleanup()
            sleep(backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)])


def _retryable_startup_error(error: BaseException) -> bool:
    return isinstance(error, HarnessError) and str(error) in _TRANSIENT_DOCKER_CODES


def fixture_images(*services: str) -> list[str]:
    """The digest-pinned image reference each named service declares in the compose file."""
    compose = COMPOSE_FILE.read_text()
    refs = []
    for service in services:
        match = re.search(rf"^  {re.escape(service)}:\n(?:    .*\n)*?    image: (\S+)$",
                          compose, re.MULTILINE)
        require(match is not None and re.search(r"@sha256:[a-f0-9]{64}$", match[1]) is not None,
                "fixture_document_invalid")
        refs.append(match[1])
    return refs


def pull_fixture_images(*services: str, **retry) -> None:
    """Make the services' registry images present (see scripts.registry_pull), keeping the
    printed failure a HarnessError code and the sanitized stderr on its `diagnostic`."""
    try:
        pull_images(fixture_images(*services), **retry)
    except RegistryPullError as error:
        failure = HarnessError(error.code)
        failure.diagnostic = error.diagnostic
        raise failure from None


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, value: object) -> None:
    with path.open("w") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def helper_identity(args: list[str]) -> tuple[str, str] | None:
    """Recognize only the exact Compose commands that execute a helper role."""
    if len(args) < 4 or args[:2] != ["docker", "compose"]:
        return None
    command_index = next((index for index, value in enumerate(args[2:], 2)
                          if value in ("run", "exec")), None)
    if command_index is None:
        return None
    command = args[command_index]
    position = command_index + 1
    while position < len(args) and args[position] in ("--rm", "--no-deps", "-T"):
        position += 1
    if position >= len(args):
        return None
    service, tail = args[position], args[position + 1:]
    identity = None
    if command == "run" and service in ("upstream-tools", "operator") and len(tail) == 1:
        identity = (service, tail[0])
    elif command == "run" and service == "init" and not tail:
        identity = ("init", "initialize")
    elif command == "run" and service == "setup" and len(tail) == 2 and tail[0] == "setup":
        identity = ("setup", tail[1])
    if identity is None:
        return None
    try:
        return FailureRole(identity[0]).value, FailureAction(identity[1]).value
    except ValueError:
        return None


def trusted_helper_failure(args: list[str], stderr: bytes) -> FailureEnvelope | None:
    identity = helper_identity(args)
    if identity is None:
        return None
    for line in stderr.decode(errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        failure = FailureEnvelope.parse_public(
            value, expected_role=identity[0], expected_action=identity[1]
        )
        if failure is not None:
            return failure
    return None


def provenance_helper_identity(args: list[str]) -> str | None:
    """Only the fixed, unmodified runtime audit exec may supply its typed failure."""
    if args[:2] != ["docker", "compose"]:
        return None
    position = 2
    while position < len(args) and args[position] in (
            "-p", "--project-name", "--env-file", "-f", "--file"):
        if position + 1 >= len(args):
            return None
        position += 2
    tail = args[position:]
    if (len(tail) == 5 and tail[:2] == ["exec", "-T"]
            and tail[2] in ("central", "worker")
            and tail[3:] == ["python", "/harness/scripts/runtime_provenance.py"]):
        return tail[2]
    return None


def trusted_provenance_failure(args: list[str], stderr: bytes):
    from scripts.provenance_models import ProvenanceCollectionError, decode_provenance_failure

    role = provenance_helper_identity(args)
    if role is None:
        return None
    for line in stderr.splitlines():
        try:
            failure = decode_provenance_failure(line)
        except (TypeError, ValueError):
            continue
        return ProvenanceCollectionError(failure, role)
    return None


def fixture_provenance_paths() -> tuple[tuple[str, ...], tuple[str, ...]]:
    runtime = tuple(item.target for item in IMMICH_RUNTIME_BUNDLE.files)
    audited = (
        "media/immich.py", "media/models.py", "central/catalog.py", "contracts/models.py",
        "contracts/time.py", "pyproject.toml", "uv.lock", *runtime,
    )
    host = (
        "scripts/docker_diagnostics.py", "scripts/immich_fixture.py", "scripts/registry_pull.py",
        "tests/integration/compose.immich.yml", "tests/integration/compose.immich-published.yml",
        *(item.source for item in IMMICH_RUNTIME_BUNDLE.files),
    )
    return audited, host


class FixtureHost:
    """Scoped Docker topology and reusable role runner for later integration."""

    def __init__(self, state: Path) -> None:
        self.state = state.resolve()
        marker = read_json(self.state / "fixture.json")
        self.project = marker["project"]
        require(re.fullmatch(r"pw-immich-fixture-[a-f0-9]{12}", self.project) is not None,
                "invalid_fixture_marker")
        require(marker.get("state") == str(self.state), "fixture_path_mismatch")
        self.published = marker.get("publish") is True
        files = (COMPOSE_FILE, PUBLISHED_COMPOSE_FILE) if self.published else (COMPOSE_FILE,)
        self.base = ["docker", "compose", "--project-name", self.project, "--env-file",
                     str(self.state / ".env")]
        for file in files:
            self.base += ["--file", str(file)]

    @classmethod
    def create(cls, state: Path, *, publish: bool = False) -> FixtureHost:
        require(state.is_absolute(), "state_path_must_be_absolute")
        state.mkdir(mode=0o700, parents=False, exist_ok=False)
        state = state.resolve()
        for name in ("setup", "runtime"):
            (state / name).mkdir(mode=0o700)
        project = "pw-immich-fixture-" + secrets.token_hex(6)
        write_json(state / "fixture.json", {"schema": 1, "project": project, "state": str(state),
                                            "publish": publish})
        # Values are generated here, not shell expressions. No deployment secret is read.
        require(not any(c in str(state) for c in "\r\n$#'\""), "unsupported_state_path")
        config = {
            "FIXTURE_STATE": str(state), "FIXTURE_DB_PASSWORD": secrets.token_hex(24),
            "FIXTURE_IMAGE": project + ":local", "FIXTURE_BASE_IMAGE": project + "-base:local",
        }
        env_path = state / ".env"
        with env_path.open("x") as stream:
            os.chmod(env_path, 0o600)
            stream.write("".join(f"{key}={value}\n" for key, value in config.items()))
        return cls(state)

    def compose(self, *args: str, timeout: int = 120, capture: bool = True) -> str:
        return self._command([*self.base, *args], timeout=timeout, capture=capture)

    @staticmethod
    def _coded_failure(code: str, stderr_tail: "_Tail") -> HarnessError:
        # The printed/public code stays a closed, bounded value (see
        # HarnessError); the bounded, already-sanitized stderr tail rides
        # along as a non-printed attribute so a retry-exhausted caller (or a
        # test) can still get at *why*, without widening what CI ever prints.
        error = HarnessError(code)
        error.diagnostic = stderr_tail.diagnostic()
        return error

    @staticmethod
    def _command(args: list[str], *, timeout: int, capture: bool) -> str:
        launched = docker_debug_args(args)
        process = subprocess.Popen(launched, cwd=ROOT, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout = bytearray()
        stdout_tail = _Tail(MAX_DOCKER_DEBUG_ENTRY // 2)
        stderr_tail = _Tail(MAX_DOCKER_DEBUG_ENTRY // 2)
        total, deadline, failure = 0, time.monotonic() + timeout, None
        try:
            with selectors.DefaultSelector() as poll:
                poll.register(process.stdout, selectors.EVENT_READ, (stdout_tail, capture))
                poll.register(process.stderr, selectors.EVENT_READ, (stderr_tail, False))
                while poll.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        failure = "docker_command_timeout"
                        break
                    for key, _ in poll.select(min(left, .2)):
                        block = os.read(key.fileobj.fileno(), 65536)
                        if not block:
                            poll.unregister(key.fileobj)
                            continue
                        total += len(block)
                        if total > MAX_STREAM_BYTES:
                            failure = "docker_output_limit"
                            break
                        tail, collect = key.data
                        tail.add(block)
                        if collect:
                            if len(stdout) + len(block) > MAX_CAPTURE_BYTES:
                                failure = "docker_output_limit"
                                break
                            stdout.extend(block)
                    if failure:
                        break
            if failure:
                FixtureHost._terminate(process)
                FixtureHost._drain(process, stdout_tail, stderr_tail)
                FixtureHost._record_failure(args, -1, stdout_tail, stderr_tail)
                raise FixtureHost._coded_failure(failure, stderr_tail)
            try:
                code = process.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                FixtureHost._terminate(process)
                FixtureHost._drain(process, stdout_tail, stderr_tail)
                FixtureHost._record_failure(args, -1, stdout_tail, stderr_tail)
                raise FixtureHost._coded_failure("docker_command_timeout", stderr_tail) from None
            if code:
                FixtureHost._record_failure(args, code, stdout_tail, stderr_tail)
                provenance_failure = trusted_provenance_failure(args, bytes(stderr_tail.data))
                if provenance_failure is not None:
                    raise provenance_failure
                failure = trusted_helper_failure(args, bytes(stderr_tail.data))
                if failure is not None:
                    raise HarnessError(failure.code)
                raise FixtureHost._coded_failure("docker_command_failed", stderr_tail)
            return bytes(stdout).decode(errors="strict") if capture else ""
        finally:
            if process.poll() is None:
                FixtureHost._terminate(process)
            process.wait()
            process.stdout.close()
            process.stderr.close()

    @staticmethod
    def _terminate(process) -> None:
        group = process.pid
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return
        except PermissionError:
            # Some sandboxed test hosts deny group signals. Preserve the primary
            # harness failure while still terminating the directly owned leader.
            process.terminate()
            try:
                process.wait(timeout=TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return
        deadline = time.monotonic() + TERMINATE_GRACE
        while time.monotonic() < deadline:
            process.poll()  # Reap an exited leader; descendants may still own the pipes.
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                process.wait()
                return
            except PermissionError:
                process.wait()
                return
            time.sleep(.05)
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    @staticmethod
    def _drain(process, stdout_tail: _Tail, stderr_tail: _Tail) -> None:
        try:
            remaining_stdout, remaining_stderr = process.communicate(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            # Termination already forced the owned process group. Diagnostics
            # must never replace the bounded harness failure if a foreign or
            # uninterruptible writer still holds a duplicated pipe descriptor.
            remaining_stdout, remaining_stderr = b"", b""
        for tail, block in ((stdout_tail, remaining_stdout), (stderr_tail, remaining_stderr)):
            tail.add(block or b"")

    @staticmethod
    def _record_failure(args: list[str], code: int,
                        stdout_tail: _Tail, stderr_tail: _Tail) -> None:
        record_docker_debug(args, code, b"stderr:\n" + stderr_tail.diagnostic()
                            + b"\nstdout:\n" + stdout_tail.diagnostic())

    def build(self, *, base_image: str | None = None) -> None:
        from scripts.container_build import daemon_compose_build, daemon_image_build

        if base_image is None:
            self._command(daemon_image_build(
                self.project + "-base:local", ROOT, dockerfile=ROOT / "Dockerfile"
            ), timeout=600, capture=False)
        else:
            require(re.fullmatch(r"sha256:[a-f0-9]{64}", base_image) is not None,
                    "immutable_fixture_base_required")
            actual = self._command(["docker", "image", "inspect", "--format", "{{.Id}}", base_image],
                                   timeout=30, capture=True).strip()
            require(actual == base_image, "fixture_base_changed")
            self._command(["docker", "tag", base_image, self.project + "-base:local"],
                          timeout=30, capture=False)
        # The parent is loaded in this Docker daemon. A selected docker-container
        # Buildx builder (as in GHA) cannot resolve that daemon-local FROM tag.
        self.compose(*daemon_compose_build("central-probe"), timeout=600, capture=False)

    def pull(self, *services: str, **retry) -> None:
        """Make the upstream registry images present before startup, so a slow or
        throttled registry is never mistaken for a container-start failure."""
        pull_fixture_images(*services, **retry)

    def start(self, *, wait_timeout: int = 240, timeout: int = 600, sleep=time.sleep) -> None:
        """Start the fixture's containers and wait for health, retrying a
        transient docker/daemon hiccup a bounded number of times. `cleanup()`
        runs between retries so the next attempt never collides with a
        partial container left by the last one."""
        call_with_retry(
            lambda: self.compose("up", "-d", "--wait", "--wait-timeout", str(wait_timeout),
                                 timeout=timeout, capture=False),
            attempts=STARTUP_RETRY_ATTEMPTS, backoff_seconds=STARTUP_RETRY_BACKOFF_SECONDS,
            cleanup=self.cleanup, retryable=_retryable_startup_error, sleep=sleep,
        )

    def export_runtime(self) -> None:
        # Container state includes the fixture runtime key; target is private 0700.
        self.compose("cp", "central-probe:/runtime/.", str(self.state / "runtime"),
                     timeout=30, capture=False)

    def role(self, role: str, action: str) -> dict:
        require(role == "setup", "unknown_fixture_action")
        # The content set waits for Immich to read every asset (and to geocode two).
        output = self.compose("run", "--rm", "--no-deps", "setup", "setup", action, timeout=600)
        result = json.loads(output)
        require(isinstance(result, dict), "invalid_role_result")
        return result

    def publish(self) -> dict:
        """Hand the named tests on this host the published URL and the admin session.

        Runs after `export_runtime()` (which brought `runtime/library.json`); the admin session
        is read from the setup volume by a one-off container and written privately."""
        require(self.published, "fixture_document_invalid")
        binding = self.compose("port", "immich", "2283", timeout=30).strip()
        host, _, port = binding.rpartition(":")
        require(host == "127.0.0.1" and port.isdigit(), "upstream_host_port_exposed")
        session = json.loads(self.compose("run", "--rm", "--no-deps", "-T", "--entrypoint",
                                          "cat", "setup", "/setup/session.json", timeout=60))
        require(isinstance(session, dict) and isinstance(session.get("token"), str),
                "fixture_document_invalid")
        write_json(self.state / "runtime/admin.json", {"token": session["token"]})
        published = {"base_url": f"http://127.0.0.1:{port}/api"}
        write_json(self.state / "published.json", published)
        return published

    def topology(self) -> tuple[dict, str]:
        containers = self.compose("ps", "--all", "--quiet").split()
        require(bool(containers), "fixture_containers_missing")
        output = subprocess.run(["docker", "inspect", *containers], check=True,
                                capture_output=True, text=True, timeout=30)
        inspected = json.loads(output.stdout)
        inventory, upstream_ip = {}, ""
        for container in inspected:
            service = container["Config"]["Labels"]["com.docker.compose.service"]
            networks = container["NetworkSettings"]["Networks"]
            memberships = sorted(name.removeprefix(self.project + "_") for name in networks)
            published = self.published and service == "immich"
            expected = (["upstream_net", "wall_net"] if service == "central-probe"
                        else ["published_net", "upstream_net"] if published
                        else ["upstream_net"])
            require(memberships == expected, "unexpected_network_membership")
            bindings = container["HostConfig"]["PortBindings"] or {}
            if published:
                require(list(bindings) == [PUBLISHED_PORT]
                        and [item["HostIp"] for item in bindings[PUBLISHED_PORT]]
                        == ["127.0.0.1"], "upstream_host_port_exposed")
            else:
                require(not bindings, "upstream_host_port_exposed")
            if service == "immich":
                upstream_ip = networks[self.project + "_upstream_net"]["IPAddress"]
            if service == "central-probe":
                require(container["HostConfig"]["Sysctls"].get("net.ipv4.ip_forward") == "0",
                        "forwarding_not_disabled")
            inventory[service] = {"image_id": container["Image"], "networks": memberships}
        images = subprocess.run(["docker", "image", "inspect",
                                 *sorted({item["image_id"] for item in inventory.values()})],
                                check=True, capture_output=True, text=True, timeout=30)
        by_id = {item["Id"]: item for item in json.loads(images.stdout)}
        for item in inventory.values():
            info = by_id[item["image_id"]]
            item.update(platform=info["Os"] + "/" + info["Architecture"],
                        repository_digests=info.get("RepoDigests", []))
        ipaddress.ip_address(upstream_ip)
        return inventory, upstream_ip

    def probe(self, upstream_ip: str) -> dict:
        return json.loads(self.compose("run", "--rm", "--no-deps", "player-probe",
                                       PROBE_CODE, upstream_ip))

    def cleanup(self) -> None:
        self.compose("down", "--volumes", "--remove-orphans", timeout=120, capture=False)




# The upstream every consumer needs: a signed-in owner, the runtime key and the content set.
SETUP_ROLES = (("setup", "initialize"),)


def run_fixture(state: Path, keep: bool, *, base_image: str | None = None,
                setup_only: bool = False, publish: bool = False) -> dict:
    """Start the disposable upstream and set it up, with the Player-probe boundary checked
    before the setup and, unless `setup_only`, again after it. `publish` (with `keep`) leaves
    Immich reachable on 127.0.0.1 for the named tests (tests/support/immich_library.py)."""
    host = FixtureHost.create(state, publish=publish)
    scope = "set-up upstream" + (", published on 127.0.0.1" if publish else "")
    evidence = {"class": "integration", "scope": scope,
                "started_utc": datetime.now(timezone.utc).isoformat(), "checks": {}}
    evidence["source_state"] = {
        "git_revision": host._command(["git", "rev-parse", "HEAD"], timeout=10,
                                      capture=True).strip(),
        "checkout_dirty": bool(host._command(["git", "status", "--porcelain"], timeout=10,
                                             capture=True).strip()),
        "qualification": "Exact fixture source hashes are recorded from the running image; "
                         "the checkout may include uncommitted implementation work.",
    }
    write_json(host.state / "evidence.json", evidence)
    try:
        evidence["stage"] = "build"
        write_json(host.state / "evidence.json", evidence)
        host.build(base_image=base_image)
        evidence["stage"] = "startup"
        write_json(host.state / "evidence.json", evidence)
        host.pull(*UPSTREAM_SERVICES)
        host.start(wait_timeout=240, timeout=600)
        inventory, upstream_ip = host.topology()
        evidence["inventory"] = inventory
        audited_files, harness_files = fixture_provenance_paths()
        evidence["adapter_runtime"] = json.loads(host.compose(
            "exec", "-T", "central-probe", "python", "-c",
            "import hashlib, importlib.metadata, json, pathlib, platform; "
            f"files={list(audited_files)!r}; "
            "print(json.dumps({'python':platform.python_version(),"
            "'packages':{p:importlib.metadata.version(p) for p in ['httpx','pydantic','Pillow']},"
            "'files':{p:hashlib.sha256(pathlib.Path('/app',p).read_bytes()).hexdigest()"
            " for p in files}}))",
        ))
        evidence["harness_files"] = {
            relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            for relative in harness_files
        }
        evidence["checks"]["denial_before"] = host.probe(upstream_ip)
        for role, action in SETUP_ROLES:
            evidence["stage"] = role + "_" + action
            write_json(host.state / "evidence.json", evidence)
            evidence["checks"][role + "_" + action] = host.role(role, action)
            write_json(host.state / "evidence.json", evidence)
        if not setup_only:
            evidence["checks"]["denial_after"] = host.probe(upstream_ip)
        if publish:
            evidence["stage"] = "publish"
            host.export_runtime()
            evidence["checks"]["published"] = host.publish()
        evidence["result"] = "passed"
        return {"result": "passed", "evidence": str(host.state / "evidence.json"),
                "services_retained": keep, "published": publish}
    except Exception as error:
        evidence["result"] = "failed"
        evidence["failure"] = (str(error) if isinstance(error, HarnessError)
                               else "unexpected_harness_failure")
        raise
    finally:
        evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            host.export_runtime()
            evidence["private_runtime_exported"] = True
        except HarnessError:
            evidence["private_runtime_exported"] = False
        write_json(host.state / "evidence.json", evidence)
        if not keep:
            host.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    # `prefetch` pulls the upstream images ahead of a later `run` (CI overlaps it with builds).
    commands.add_parser("prefetch")
    for name in ("run", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        if name == "run":
            command.add_argument("--keep", action="store_true")
            command.add_argument("--base-image")
            command.add_argument("--setup-only", action="store_true")
            command.add_argument("--publish", action="store_true",
                                 help="publish Immich on 127.0.0.1 for the named tests")
    args = parser.parse_args()
    if args.command == "run":
        result = run_fixture(args.state_dir, args.keep, base_image=args.base_image,
                             setup_only=args.setup_only, publish=args.publish)
    elif args.command == "prefetch":
        pull_fixture_images(*UPSTREAM_SERVICES)
        result = {"prefetched": list(UPSTREAM_SERVICES)}
    else:
        FixtureHost(args.state_dir).cleanup()
        result = {"cleaned": True}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = str(error) if isinstance(error, HarnessError) else "unexpected_harness_failure"
        print(json.dumps({"error": code}), file=sys.stderr)
        sys.exit(1)
