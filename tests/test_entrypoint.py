"""docker-entrypoint.sh guard behaviour, exercised hermetically without Docker.

The root branch keys on `id -u`, so a fake `id` (reporting 0) on PATH forces it
without real privilege; fake `chown`/`gosu` stand in for the container tools so
the script's control flow -- not the host -- is under test. This asserts a
misconfigured target uid/gid can never silently run the worker as root.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"

_FAKE_BIN = {
    # Force the root branch regardless of the real euid.
    "id": "#!/bin/sh\necho 0\n",
    # A chown that always succeeds; the real one needs privilege we do not have.
    "chown": "#!/bin/sh\nexit 0\n",
    # gosu <uid:gid> <cmd...>: drop the identity arg and exec the command, so a
    # successful drop-through is observable as the command's own output.
    "gosu": '#!/bin/sh\nshift\nexec "$@"\n',
}


def _run(tmp_path, env_overrides):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in _FAKE_BIN.items():
        script = fake_bin / name
        script.write_text(body)
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        # Point at a real, existing dir so the chown branch is actually entered.
        "PHOTO_WALL_MEDIA_ROOT": str(tmp_path),
        **env_overrides,
    }
    return subprocess.run(
        ["sh", str(ENTRYPOINT), "sh", "-c", "echo ENTRYPOINT_RAN"],
        env=env,
        capture_output=True,
        text=True,
    )


def test_valid_puid_pgid_chowns_and_drops_through(tmp_path):
    result = _run(tmp_path, {"PHOTO_WALL_PUID": "10001", "PHOTO_WALL_PGID": "10001"})
    assert result.returncode == 0, result.stderr
    assert "ENTRYPOINT_RAN" in result.stdout


@pytest.mark.parametrize("bad", ["0", "abc", "10001x", "-1"])
def test_non_positive_or_non_numeric_puid_is_rejected(tmp_path, bad):
    result = _run(tmp_path, {"PHOTO_WALL_PUID": bad, "PHOTO_WALL_PGID": "10001"})
    assert result.returncode == 78
    assert "PHOTO_WALL_PUID" in result.stderr
    assert "ENTRYPOINT_RAN" not in result.stdout


@pytest.mark.parametrize("bad", ["0", "abc"])
def test_non_positive_or_non_numeric_pgid_is_rejected(tmp_path, bad):
    result = _run(tmp_path, {"PHOTO_WALL_PUID": "10001", "PHOTO_WALL_PGID": bad})
    assert result.returncode == 78
    assert "PHOTO_WALL_PGID" in result.stderr
    assert "ENTRYPOINT_RAN" not in result.stdout


def test_default_identity_is_valid_when_unset(tmp_path):
    # With neither var set the defaults (10001) apply and the guard passes.
    result = _run(tmp_path, {})
    assert result.returncode == 0, result.stderr
    assert "ENTRYPOINT_RAN" in result.stdout
