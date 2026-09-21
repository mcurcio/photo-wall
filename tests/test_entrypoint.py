"""docker-entrypoint.sh guard behaviour, exercised hermetically without Docker.

The root branch keys on `id -u`, so a fake `id` (reporting 0) on PATH forces it
without real privilege; fake `install`/`gosu` stand in for the container tools so
the script's control flow -- not the host -- is under test. The fake `install`
creates the directory tree with the requested mode but skips the privileged
chown we cannot perform, so the test can assert the 0013 cache layout is
materialized (media/ apps/ os-images/, mode 0700) while still proving a
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
    # Stand in for the privileged `install -d -o U -g G -m MODE DIR`: create the
    # directory with the requested mode but skip the chown, which needs real root
    # we do not have. Dir creation + mode stay under test; ownership does not.
    "install": (
        "#!/bin/sh\n"
        "mode=0755\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -d) ;;\n"
        "    -m) mode=\"$2\"; shift ;;\n"
        "    -o|-g) shift ;;\n"
        "    -*) ;;\n"
        "    *) mkdir -p \"$1\" && chmod \"$mode\" \"$1\" ;;\n"
        "  esac\n"
        "  shift\n"
        "done\n"
    ),
    # gosu <uid:gid> <cmd...>: drop the identity arg and exec the command, so a
    # successful drop-through is observable as the command's own output.
    "gosu": '#!/bin/sh\nshift\nexec "$@"\n',
}

_SUBDIRS = ("media", "apps", "os-images")


def _run(tmp_path, env_overrides):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in _FAKE_BIN.items():
        script = fake_bin / name
        script.write_text(body)
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        # 0013: one cache root; point it at a real, existing dir so the boot
        # step's chown/install branch has a target to operate on.
        "PHOTO_WALL_CACHE_ROOT": str(tmp_path),
        **env_overrides,
    }
    return subprocess.run(
        ["sh", str(ENTRYPOINT), "sh", "-c", "echo ENTRYPOINT_RAN"],
        env=env,
        capture_output=True,
        text=True,
    )


def _assert_layout_materialized(tmp_path):
    for sub in _SUBDIRS:
        d = tmp_path / sub
        assert d.is_dir(), f"{sub}/ was not created under the cache root"
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, f"{sub}/ is not mode 0700"


def _assert_layout_absent(tmp_path):
    for sub in _SUBDIRS:
        assert not (tmp_path / sub).exists(), f"{sub}/ must not be created"


def test_valid_puid_pgid_materializes_layout_and_drops_through(tmp_path):
    result = _run(tmp_path, {"PHOTO_WALL_PUID": "10001", "PHOTO_WALL_PGID": "10001"})
    assert result.returncode == 0, result.stderr
    assert "ENTRYPOINT_RAN" in result.stdout
    # The single writer creates each domain subdir at 0700 before the drop.
    _assert_layout_materialized(tmp_path)


@pytest.mark.parametrize("bad", ["0", "00", "000", "010", "007", "abc", "10001x", "-1"])
def test_non_positive_or_non_numeric_puid_is_rejected(tmp_path, bad):
    result = _run(tmp_path, {"PHOTO_WALL_PUID": bad, "PHOTO_WALL_PGID": "10001"})
    assert result.returncode == 78
    assert "PHOTO_WALL_PUID" in result.stderr
    assert "ENTRYPOINT_RAN" not in result.stdout
    # Rejected before any cache dir is created or ownership is taken.
    _assert_layout_absent(tmp_path)


@pytest.mark.parametrize("bad", ["0", "00", "000", "010", "007", "abc"])
def test_non_positive_or_non_numeric_pgid_is_rejected(tmp_path, bad):
    result = _run(tmp_path, {"PHOTO_WALL_PUID": "10001", "PHOTO_WALL_PGID": bad})
    assert result.returncode == 78
    assert "PHOTO_WALL_PGID" in result.stderr
    assert "ENTRYPOINT_RAN" not in result.stdout
    _assert_layout_absent(tmp_path)


def test_default_identity_is_valid_when_unset(tmp_path):
    # With neither var set the defaults (10001) apply and the guard passes.
    result = _run(tmp_path, {})
    assert result.returncode == 0, result.stderr
    assert "ENTRYPOINT_RAN" in result.stdout
    _assert_layout_materialized(tmp_path)
