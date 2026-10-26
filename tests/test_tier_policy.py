"""tests/conftest.py's CI policy, run as pytest runs it: a skip no allowlist entry owns fails a CI
run, a derived tier is what `-m` selects, and a leg's shards run every test once."""

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

@pytest.mark.parametrize("case", range(3))
def test_cases(case):
    pass

@pytest.mark.shard_each_case
@pytest.mark.parametrize("case", range(12))
def test_each_case(case):
    pass

def test_named_0():
    pass

def test_named_1():
    pass

def test_named_2():
    pass

def test_named_3():
    pass

def test_named_4():
    pass

def test_named_5():
    pass

def test_named_6():
    pass

def test_named_7():
    pass

def test_named_8():
    pass

def test_named_9():
    pass

def test_named_10():
    pass

def test_named_11():
    pass
"""


def run_probe(tmp_path, *args, ci, shard=None):
    (tmp_path / "test_probe.py").write_text(PROBE)
    env = {key: value for key, value in os.environ.items()
           if key not in {"CI", "PHOTO_WALL_TEST_DATABASE_URL", "PHOTO_WALL_TEST_REQUIRE_DATABASE"}}
    env |= {"PYTHONPATH": os.pathsep.join([str(TESTS), str(TESTS.parent)]),
            "PYTHONDONTWRITEBYTECODE": "1", **({"CI": "true"} if ci else {})}
    env.pop("PHOTO_WALL_TEST_SHARD", None)
    if shard is not None:
        env["PHOTO_WALL_TEST_SHARD"] = shard
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


def collected(run) -> set[str]:
    return {line for line in run.stdout.splitlines() if "::" in line}


def test_the_shards_run_every_test_once_and_keep_a_tests_cases_together(tmp_path):
    """support/shards.py: the two shards are disjoint and together the whole run; a test's
    parametrised cases share a shard unless it is marked `shard_each_case`. Mutation probe: hash
    the node id -> the cases split."""
    whole = collected(run_probe(tmp_path, "--collect-only", ci=False))
    first = collected(run_probe(tmp_path, "--collect-only", ci=False, shard="1/2"))
    second = collected(run_probe(tmp_path, "--collect-only", ci=False, shard="2/2"))
    assert first and second and not first & second and first | second == whole
    cases = {test for test in whole if "test_cases[" in test}
    assert len(cases) == 3 and (cases <= first or cases <= second)
    # A `shard_each_case` test's cases spread over both shards. Mutation probe: ignore the marker
    # -> its twelve cases land in one shard.
    each = {test for test in whole if "test_each_case[" in test}
    assert len(each) == 12 and each & first and each & second
    malformed = run_probe(tmp_path, "--collect-only", ci=False, shard="3/2")
    assert malformed.returncode != 0 and "PHOTO_WALL_TEST_SHARD must be index/count" in malformed.stderr
