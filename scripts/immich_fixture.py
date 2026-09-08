"""Disposable real-Immich harness; generated fixture media may be freely redistributed.

No existing Immich instance is accepted. Private credentials stay in a new state
directory and container roles receive only the mount needed for their function.
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

MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
TERMINATE_GRACE = 5.0


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
    elif (command == "exec" and service == "central-probe" and len(tail) >= 5
          and tail[:4] == ["python", "-m", "scripts.immich_runtime", "verify"]):
        identity = ("verify", tail[4])
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


def fixture_provenance_paths() -> tuple[tuple[str, ...], tuple[str, ...]]:
    runtime = tuple(item.target for item in IMMICH_RUNTIME_BUNDLE.files)
    audited = (
        "media/immich.py", "media/models.py", "central/catalog.py", "contracts/models.py",
        "contracts/time.py", "pyproject.toml", "uv.lock", *runtime,
    )
    host = (
        "scripts/docker_diagnostics.py", "scripts/immich_fixture.py",
        "tests/integration/compose.immich.yml",
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
        self.base = ["docker", "compose", "--project-name", self.project, "--env-file",
                     str(self.state / ".env"), "--file",
                     str(ROOT / "tests/integration/compose.immich.yml")]

    @classmethod
    def create(cls, state: Path) -> FixtureHost:
        require(state.is_absolute(), "state_path_must_be_absolute")
        state.mkdir(mode=0o700, parents=False, exist_ok=False)
        state = state.resolve()
        for name in ("setup", "runtime"):
            (state / name).mkdir(mode=0o700)
        project = "pw-immich-fixture-" + secrets.token_hex(6)
        write_json(state / "fixture.json", {"schema": 1, "project": project, "state": str(state)})
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
                raise HarnessError(failure)
            try:
                code = process.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                FixtureHost._terminate(process)
                FixtureHost._drain(process, stdout_tail, stderr_tail)
                FixtureHost._record_failure(args, -1, stdout_tail, stderr_tail)
                raise HarnessError("docker_command_timeout") from None
            if code:
                FixtureHost._record_failure(args, code, stdout_tail, stderr_tail)
                failure = trusted_helper_failure(args, bytes(stderr_tail.data))
                if failure is not None:
                    raise HarnessError(failure.code)
                raise HarnessError("docker_command_failed")
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

    def export_runtime(self) -> None:
        # Container state includes the fixture runtime key; target is private 0700.
        self.compose("cp", "central-probe:/runtime/.", str(self.state / "runtime"),
                     timeout=30, capture=False)

    def role(self, role: str, action: str, *, page_size: int = 3) -> dict:
        if role == "setup":
            output = self.compose("run", "--rm", "--no-deps", "setup", "setup", action,
                                  timeout=240)
        else:
            output = self.compose("exec", "-T", "central-probe", "python",
                                  "-m", "scripts.immich_runtime", "verify", action,
                                  "--page-size", str(page_size), timeout=240)
        result = json.loads(output)
        require(isinstance(result, dict), "invalid_role_result")
        return result

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
            expected = (["upstream_net", "wall_net"] if service == "central-probe"
                        else ["upstream_net"])
            require(memberships == expected, "unexpected_network_membership")
            require(not container["HostConfig"]["PortBindings"], "upstream_host_port_exposed")
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




def run_fixture(state: Path, keep: bool, *, page_size: int = 3,
                base_image: str | None = None) -> dict:
    require(type(page_size) is int and 1 <= page_size <= 1000, "invalid_page_size")
    host = FixtureHost.create(state)
    evidence = {"class": "integration", "scope": "real upstream adapter and network boundary",
                "started_utc": datetime.now(timezone.utc).isoformat(), "checks": {}}
    evidence["source_state"] = {
        "git_revision": host._command(["git", "rev-parse", "HEAD"], timeout=10,
                                      capture=True).strip(),
        "checkout_dirty": bool(host._command(["git", "status", "--porcelain"], timeout=10,
                                             capture=True).strip()),
        "qualification": "Exact adapter source hashes are recorded from the running image; "
                         "the checkout may include uncommitted implementation work.",
    }
    write_json(host.state / "evidence.json", evidence)
    try:
        evidence["stage"] = "build"
        write_json(host.state / "evidence.json", evidence)
        host.build(base_image=base_image)
        evidence["stage"] = "startup"
        write_json(host.state / "evidence.json", evidence)
        host.compose("up", "-d", "--wait", "--wait-timeout", "240", timeout=600, capture=False)
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
        for role, action in [("setup", "initialize"), ("central", "initial"),
                             ("setup", "live"), ("central", "live"),
                             ("setup", "delete"), ("central", "deleted"),
                             ("setup", "deny"), ("central", "deny"),
                             ("setup", "restore")]:
            evidence["stage"] = role + "_" + action
            write_json(host.state / "evidence.json", evidence)
            evidence["checks"][role + "_" + action] = host.role(role, action, page_size=page_size)
            write_json(host.state / "evidence.json", evidence)
        evidence["checks"]["denial_after"] = host.probe(upstream_ip)
        host.compose("stop", "immich", capture=False)
        evidence["checks"]["central_outage"] = host.role("central", "outage", page_size=page_size)
        host.compose("up", "-d", "--wait", "--wait-timeout", "120", "immich",
                     timeout=180, capture=False)
        evidence["checks"]["central_recovered"] = host.role("central", "recovered", page_size=page_size)
        evidence["result"] = "passed"
        return {"result": "passed", "evidence": str(host.state / "evidence.json"),
                "services_retained": keep}
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
    for name in ("run", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        if name == "run":
            command.add_argument("--keep", action="store_true")
            command.add_argument("--page-size", type=int, default=3)
            command.add_argument("--base-image")
    args = parser.parse_args()
    if args.command == "run":
        result = run_fixture(args.state_dir, args.keep, page_size=args.page_size,
                             base_image=args.base_image)
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
