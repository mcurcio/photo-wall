"""Docker failure evidence is useful without exporting fixture credentials."""

import os

import pytest

from scripts.docker_diagnostics import MAX_DOCKER_DEBUG_ENTRY
from scripts.immich_fixture import FixtureHost, HarnessError


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
