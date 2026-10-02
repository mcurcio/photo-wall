"""tests/conftest.py's CI policy, run as pytest runs it: a skip no allowlist entry owns fails a CI
run, and a derived tier is what `-m` selects."""

import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).parent

PROBE = """
import pytest

def test_unowned():
    pytest.skip("nobody owns this skip")

def test_owned():
    pytest.skip("needs device-tree-compiler (dtc, fdtoverlay, fdtget)")

def test_database(registry):
    pass

def test_unit():
    pass
"""


def run_probe(tmp_path, *args, ci):
    (tmp_path / "test_probe.py").write_text(PROBE)
    env = {key: value for key, value in os.environ.items()
           if key not in {"CI", "PHOTO_WALL_TEST_DATABASE_URL", "PHOTO_WALL_TEST_REQUIRE_DATABASE"}}
    env |= {"PYTHONPATH": os.pathsep.join([str(TESTS), str(TESTS.parent)]),
            "PYTHONDONTWRITEBYTECODE": "1", **({"CI": "true"} if ci else {})}
    # `-p conftest` loads the suite's conftest as a plugin for a probe outside tests/.
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "-p", "conftest", "--rootdir", str(tmp_path), *args, str(tmp_path)],
                          env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)


def test_a_skip_nobody_owns_fails_a_ci_run_only(tmp_path):
    unowned = run_probe(tmp_path, "-k", "unowned", ci=True)
    assert unowned.returncode == 1, unowned.stdout
    assert "nobody owns this skip" in unowned.stdout.split("skips no CI job owns")[1]
    assert run_probe(tmp_path, "-k", "unowned", ci=False).returncode == 0
    assert run_probe(tmp_path, "-k", "owned and not unowned", ci=True).returncode == 0


def test_the_tiers_are_derived_from_fixtures(tmp_path):
    selected = run_probe(tmp_path, "-m", "db", "--collect-only", ci=False)
    assert "test_probe.py::test_database" in selected.stdout
    assert "test_probe.py::test_unit" not in selected.stdout
    unit = run_probe(tmp_path, "-m", "not db and not browser", "--collect-only", ci=False)
    assert "test_probe.py::test_unit" in unit.stdout
    assert "test_probe.py::test_database" not in unit.stdout
