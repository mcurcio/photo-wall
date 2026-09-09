"""Docker failure evidence is useful without exporting fixture credentials."""

import json
import os

import pytest

from scripts.docker_diagnostics import (
    MAX_DOCKER_DEBUG_ENTRY,
    bounded_diagnostic,
    sanitize_docker_output,
)
from scripts.harness_failure import FailureEnvelope
from scripts.immich_actions import ActionError
from scripts.immich_fixture import (
    MAX_STREAM_BYTES,
    FixtureHost,
    HarnessError,
    _Tail,
    trusted_provenance_failure,
)
from scripts.provenance_models import ProvenanceCollectionError


@pytest.mark.parametrize("role", ["central", "worker"])
def test_exact_runtime_provenance_exec_preserves_typed_failure(tmp_path, monkeypatch, role):
    failure = ProvenanceCollectionError.from_code("provenance_manifest_unreadable").failure
    payload = failure.model_dump_json(by_alias=True)
    docker_script(tmp_path, f"echo '{payload}' >&2\nexit 1\n")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    with pytest.raises(ProvenanceCollectionError) as caught:
        FixtureHost._command([
            "docker", "compose", "-p", "private-project", "--env-file", "private-env",
            "-f", "private-compose", "exec", "-T", role, "python",
            "/harness/scripts/runtime_provenance.py",
        ], timeout=10, capture=True)
    assert caught.value.role == role
    assert caught.value.failure == failure


@pytest.mark.parametrize("tail", [
    ["exec", "-T", "operator", "python", "/harness/scripts/runtime_provenance.py"],
    ["exec", "-T", "central", "python", "-c", "arbitrary"],
    ["exec", "-T", "central", "python", "/harness/scripts/runtime_provenance.py", "--app-root", "/other"],
    ["exec", "-T", "central", "python", "/other/runtime_provenance.py"],
    ["run", "-T", "central", "python", "/harness/scripts/runtime_provenance.py"],
    ["exec", "--user", "0", "-T", "central", "python", "/harness/scripts/runtime_provenance.py"],
])
def test_nonmatching_command_cannot_supply_trusted_provenance_failure(tail):
    failure = ProvenanceCollectionError.from_code("provenance_manifest_unreadable").failure
    assert trusted_provenance_failure(
        ["docker", "compose", *tail], failure.model_dump_json(by_alias=True).encode()
    ) is None


@pytest.mark.parametrize("change", [
    {"schema": True}, {"stage": "bundle"}, {"code": "private-token"},
    {"extra": "private-token"}, {"type": "other"},
])
def test_invalid_provenance_failure_envelope_is_untrusted(change):
    failure = ProvenanceCollectionError.from_code("provenance_manifest_unreadable").failure
    value = failure.model_dump(mode="json", by_alias=True)
    value.update(change)
    assert trusted_provenance_failure(
        ["docker", "compose", "exec", "-T", "central", "python",
         "/harness/scripts/runtime_provenance.py"], json.dumps(value).encode()
    ) is None


@pytest.mark.parametrize("field", ["type", "schema", "stage", "code"])
def test_incomplete_provenance_failure_envelope_is_untrusted(field):
    failure = ProvenanceCollectionError.from_code("provenance_manifest_unreadable").failure
    value = failure.model_dump(mode="json", by_alias=True)
    del value[field]
    assert trusted_provenance_failure(
        ["docker", "compose", "exec", "-T", "central", "python",
         "/harness/scripts/runtime_provenance.py"], json.dumps(value).encode()
    ) is None


def docker_script(path, body):
    executable = path / "docker"
    executable.write_text("#!/bin/sh\n" + body)
    executable.chmod(0o755)


def test_immich_command_records_bounded_sanitized_stdout_and_stderr(tmp_path, monkeypatch):
    docker_script(tmp_path, "echo 'password=private-database-value' >&2\n"
                  "echo '{\"token\":\"private-runtime-token\"}'\n"
                  "python3 -c 'print(\"x\" * 70000)' >&2\nexit 17\n")
    log = tmp_path / "docker-debug.log"
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PHOTO_WALL_DOCKER_DEBUG", "1")
    monkeypatch.setenv("PHOTO_WALL_DOCKER_DEBUG_LOG", str(log))

    with pytest.raises(HarnessError, match="docker_command_failed"):
        FixtureHost._command(["docker", "compose", "build", "private-argument"],
                             timeout=10, capture=False)

    diagnostic = log.read_bytes()
    assert diagnostic.startswith(b"docker operation=compose exit=17\n")
    assert b"stderr:\n" in diagnostic and b"stdout:\n" in diagnostic
    assert b"private-argument" not in diagnostic
    assert b"private-database-value" not in diagnostic
    assert b"private-runtime-token" not in diagnostic
    assert b"<redacted>" in diagnostic
    assert len(diagnostic) <= MAX_DOCKER_DEBUG_ENTRY + 64
    assert log.stat().st_mode & 0o777 == 0o600


def test_immich_success_keeps_debug_stderr_out_of_structured_result(tmp_path, monkeypatch):
    docker_script(tmp_path, "echo debug >&2\necho '{\"ok\":true}'\n")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PHOTO_WALL_DOCKER_DEBUG", "1")

    assert FixtureHost._command(["docker", "compose", "ps"], timeout=10, capture=True) == '{"ok":true}\n'
    assert FixtureHost._command(["docker", "compose", "ps"], timeout=10, capture=False) == ""


def test_bearer_is_redacted_before_generic_authorization_key():
    result = sanitize_docker_output(b"Authorization: Bearer private-bearer-value\n")
    assert b"private-bearer-value" not in result
    assert result == b"Authorization: <redacted>\n"


def test_truncated_tail_drops_partial_secret_line_and_keeps_complete_diagnostics():
    secret = b"private-secret-suffix"
    oversized = b"Authorization: Bearer " + b"x" * MAX_DOCKER_DEBUG_ENTRY + secret
    result = bounded_diagnostic(oversized + b"\nnext complete diagnostic\n")
    assert secret not in result
    assert b"x" * 32 not in result
    assert result.endswith(b"next complete diagnostic\n")


def test_stream_limit_terminates_unbounded_producer_and_keeps_bounded_tail(tmp_path, monkeypatch):
    docker_script(tmp_path,
        "python3 -c 'import os; block=b\"x\"*65536; "
        f"[os.write(1,block) for _ in range({MAX_STREAM_BYTES // 65536 + 4})]'\n")
    log = tmp_path / "docker-debug.log"
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PHOTO_WALL_DOCKER_DEBUG_LOG", str(log))
    with pytest.raises(HarnessError, match="docker_output_limit"):
        FixtureHost._command(["docker", "compose", "build"], timeout=10, capture=False)
    assert log.stat().st_size <= MAX_DOCKER_DEBUG_ENTRY + 64


def test_timeout_preserves_harness_error_when_descendant_holds_pipe(tmp_path, monkeypatch):
    from scripts import immich_fixture

    pid_file = tmp_path / "child.pid"
    docker_script(tmp_path,
        "python3 -c 'import os,signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1],\"w\").write(str(os.getpid())); "
        "os.write(2,b\"child-ready\\n\"); time.sleep(2)' \"$1\" &\nwait\n")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(immich_fixture, "TERMINATE_GRACE", .1)

    with pytest.raises(HarnessError, match="docker_command_timeout"):
        FixtureHost._command(["docker", str(pid_file)], timeout=.5, capture=False)

    assert pid_file.is_file()


def test_terminate_probes_group_after_leader_exit_then_forces_kill(monkeypatch):
    from scripts import immich_fixture

    class ExitedLeader:
        pid = 1234
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0

    signals = []
    moments = iter((0, 0, 1))
    monkeypatch.setattr(immich_fixture, "TERMINATE_GRACE", .5)
    monkeypatch.setattr(immich_fixture.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(immich_fixture.time, "sleep", lambda _: None)
    monkeypatch.setattr(immich_fixture.os, "killpg",
                        lambda group, selected: signals.append((group, selected)))

    FixtureHost._terminate(ExitedLeader())

    assert signals == [(1234, immich_fixture.signal.SIGTERM), (1234, 0),
                       (1234, immich_fixture.signal.SIGKILL)]


def test_drain_timeout_cannot_mask_primary_failure():
    class StuckPipe:
        def communicate(self, timeout):
            raise __import__("subprocess").TimeoutExpired("docker", timeout)

    stdout, stderr = _Tail(32), _Tail(32)
    FixtureHost._drain(StuckPipe(), stdout, stderr)
    assert stdout.data == stderr.data == b""


def helper_failure(**changes):
    failure = FailureEnvelope.create(
        "role_action", "operator", "health", "operator_http_503"
    ).public_payload()
    failure["failure"].update(changes)
    return failure


def failed_docker(tmp_path, monkeypatch, payload, args):
    docker_script(tmp_path, "printf '%s\\n' '" + json.dumps(payload) + "' >&2\nexit 17\n")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    with pytest.raises(HarnessError) as caught:
        FixtureHost._command(args, timeout=10, capture=False)
    return str(caught.value)


def test_generic_docker_command_cannot_promote_forged_helper_failure(tmp_path, monkeypatch):
    assert failed_docker(
        tmp_path, monkeypatch, helper_failure(), ["docker", "compose", "build"]
    ) == "docker_command_failed"


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(error="lowercasesecrettoken", failure={"code": "lowercasesecrettoken"}),
    lambda value: value["failure"].update(role="upstream-tools"),
    lambda value: value["failure"].update(action="snapshot"),
    lambda value: value["failure"].update(schema=2),
    lambda value: value["failure"].update(schema=True),
    lambda value: value["failure"].update(extra="operator_http_503"),
    lambda value: value.update(extra="operator_http_503"),
    lambda value: value.update(error="operator_http_502"),
])
def test_known_helper_rejects_forged_malformed_or_mismatched_envelope(
        tmp_path, monkeypatch, mutate):
    payload = helper_failure()
    mutate(payload)
    assert failed_docker(
        tmp_path, monkeypatch, payload,
        ["docker", "compose", "run", "--rm", "--no-deps", "operator", "health"],
    ) == "docker_command_failed"


def test_known_helper_promotes_only_exact_typed_envelope(tmp_path, monkeypatch):
    assert failed_docker(
        tmp_path, monkeypatch, helper_failure(),
        ["docker", "compose", "run", "--rm", "--no-deps", "operator", "health"],
    ) == "operator_http_503"


def test_action_error_maps_arbitrary_code_shaped_text_to_generic():
    assert str(ActionError("lowercasesecrettoken")) == "demo_failed"
