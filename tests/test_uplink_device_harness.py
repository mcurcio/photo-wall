"""The device-runtime harness, run here exactly as tests/debs/test_netboot_init.py runs it in trixie
(`python3 -I -S`, only the tree on sys.path), on this interpreter, over a tree shaped like the one
photo-wall-netboot-init's hook copies (stage 1's files, `appliance` a PEP 420 portion): it passes,
and fails the leg on a failing row or a module stage 1 reaches that the tree lacks."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.module_closure import REPO, first_party_files

HARNESS = REPO / "scripts" / "uplink_device_harness.py"


@pytest.fixture(scope="module")
def staged(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("device")
    for path in first_party_files(("appliance.netboot_init",), repo=REPO):
        if path != Path("appliance/__init__.py"):     # no package installs it (a PEP 420 portion)
            (root / "tree" / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / path, root / "tree" / path)
    subprocess.run([sys.executable, "-m", "scripts.uplink_device_harness", "mint",
                    str(root / "certs")], cwd=REPO, check=True, timeout=60)
    return root / "tree", root / "certs"


def run(tree: Path, certs: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-S", str(HARNESS), "run", "--tree",
                           str(tree), "--certs", str(certs)], cwd=tree.parent,
                          capture_output=True, text=True, timeout=120)


def test_the_harness_passes_on_stage_1s_tree(staged):
    result = run(*staged)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "isolated=1 no_site=1" in result.stdout and "OpenSSL" in result.stdout
    assert result.stdout.count("\nOK ") == 6


def test_a_wrong_leaf_on_the_happy_chain_fails_the_leg(staged, tmp_path):
    tree, certs = staged
    swapped = tmp_path / "certs"
    shutil.copytree(certs, swapped)
    for suffix in ("pem", "key"):
        shutil.copy(certs / f"other-name.{suffix}", swapped / f"central.{suffix}")
    result = run(tree, swapped)
    assert result.returncode == 1
    assert "FAIL 301 chain to Central over verified TLS" in result.stdout


def test_a_module_missing_from_the_tree_fails_the_leg(staged, tmp_path):
    tree, certs = staged
    partial = tmp_path / "tree"
    shutil.copytree(tree, partial)
    (partial / "contracts" / "central_identity.py").unlink()
    result = run(partial, certs)
    assert result.returncode == 1 and "FAIL import" in result.stdout
