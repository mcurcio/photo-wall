"""The image qualification overlay must be importable on its own, under -I."""
from __future__ import annotations

import subprocess
import sys

from scripts.node_rollout_image_check import _test_dependencies


def test_copied_test_dependencies_import_pytest_in_isolation(tmp_path):
    deps = tmp_path / "deps"
    _test_dependencies(deps)
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", f"import sys; sys.path[:0] = [{str(deps)!r}]; import pytest"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
