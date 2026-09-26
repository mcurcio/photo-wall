"""The device-runtime harness, run here exactly as the netboot-e2e leg runs it in trixie
(`python3 -I -S`, only the staged closure on sys.path), on this interpreter: it passes on the
real closure, and fails the leg on a failing row or a missing closure module."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.module_closure import REPO, initrd_closure, stage

HARNESS = REPO / "scripts" / "uplink_device_harness.py"


@pytest.fixture(scope="module")
def staged(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("device")
    stage(initrd_closure(), repo=REPO, into=root / "closure")
    subprocess.run([sys.executable, "-m", "scripts.uplink_device_harness", "mint",
                    str(root / "certs")], cwd=REPO, check=True, timeout=60)
    return root / "closure", root / "certs"


def run(closure: Path, certs: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-S", str(HARNESS), "run", "--closure",
                           str(closure), "--certs", str(certs)], cwd=closure.parent,
                          capture_output=True, text=True, timeout=120)


def test_the_harness_passes_on_the_staged_closure(staged):
    result = run(*staged)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "isolated=1 no_site=1" in result.stdout and "OpenSSL" in result.stdout
    assert result.stdout.count("\nOK ") == 6


def test_a_wrong_leaf_on_the_happy_chain_fails_the_leg(staged, tmp_path):
    closure, certs = staged
    swapped = tmp_path / "certs"
    shutil.copytree(certs, swapped)
    for suffix in ("pem", "key"):
        shutil.copy(certs / f"other-name.{suffix}", swapped / f"central.{suffix}")
    result = run(closure, swapped)
    assert result.returncode == 1
    assert "FAIL 301 chain to Central over verified TLS" in result.stdout


def test_a_module_missing_from_the_closure_fails_the_leg(staged, tmp_path):
    closure, certs = staged
    partial = tmp_path / "closure"
    shutil.copytree(closure, partial)
    (partial / "contracts" / "central_identity.py").unlink()
    result = run(partial, certs)
    assert result.returncode == 1 and "FAIL import" in result.stdout
