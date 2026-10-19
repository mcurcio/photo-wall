"""The image qualification overlay must be importable on its own, under -I."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.node_rollout_image_check import _test_dependencies


def test_copied_test_dependencies_import_pytest_in_isolation(tmp_path):
    deps = tmp_path / "deps"
    _test_dependencies(deps)
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", f"import sys; sys.path[:0] = [{str(deps)!r}]; import pytest"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_qualification_loads_with_only_what_centrals_image_has(tmp_path):
    # The image holds the project's runtime dependencies and pytest's, never the dev group or what
    # only the Pi has: a module the qualification reaches that imports one at load (segno, gi)
    # fails the image's suites on main. This loads the same modules the same way on every PR.
    ini = tmp_path / "pytest.ini"
    ini.write_text("[pytest]\n")
    result = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).parent / "support" / "image_runtime.py"), str(ini)],
        capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
