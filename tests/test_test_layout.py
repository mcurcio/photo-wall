"""The suite's import layout, run as pytest runs it: a scratch tree under the real pytest
configuration may add test directories with no registration and reuse a test file name in each."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SAME = """from support.rig import VALUE


def test_reads_the_shared_rig():
    assert VALUE == 1
"""
CROSS = """from node.alpha.test_same import VALUE


def test_imports_a_sibling_context_by_its_path():
    assert VALUE == 1
"""


def test_context_directories_may_share_a_test_file_name(tmp_path):
    shutil.copy2(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    tests = tmp_path / "tests"
    (tests / "support").mkdir(parents=True)
    (tests / "support" / "__init__.py").write_text("")
    (tests / "support" / "rig.py").write_text("VALUE = 1\n")
    (tests / "test_same.py").write_text(SAME)
    # Directory names pyproject.toml has never heard of: adding a context is not a config edit.
    for context in ("alpha", "beta"):
        (tests / "node" / context).mkdir(parents=True)
        (tests / "node" / context / "test_same.py").write_text(SAME)
    (tests / "node" / "beta" / "test_cross.py").write_text(CROSS)
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTEST_ADDOPTS"}} | {"PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout, result.stdout
