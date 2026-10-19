"""Load what Central's image qualification loads, with only what the image can import.

Run as a program (tests/test_node_rollout_image_check.py does): it hides every module outside
the standard library, the image's own packages, tests/ and the image's distributions
(scripts/node_rollout_image_check.py IMAGE_DISTRIBUTIONS), so a dev-group or Pi-only package
(segno, gi, ...) imported at load fails here as it fails in the image. Then it imports the
qualification's production modules and collects its suites as `inside` runs them. Exit 0 when
everything loads.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import os
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
THIRD_PARTY = frozenset({"site-packages", "dist-packages"})


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class ImageOnly(importlib.abc.MetaPathFinder):
    """Refuses a top-level module the image does not have."""

    def __init__(self, allowed: frozenset[str]):
        self.allowed = allowed

    def find_spec(self, name, path=None, target=None):
        if "." in name or name in self.allowed:
            return None
        # A third-party module lives in an installed environment (the venv's site-packages,
        # Debian's dist-packages for gi); the standard library and tests/ never do.
        spec = importlib.machinery.PathFinder.find_spec(name)
        if spec is None or not spec.origin or not THIRD_PARTY & set(Path(spec.origin).parts):
            return None
        raise ModuleNotFoundError(f"No module named {name!r} in Central's image", name=name)


def main(empty_ini: str) -> int:
    sys.path.insert(0, str(ROOT))
    from scripts.node_rollout_image_check import (
        IMAGE_DISTRIBUTIONS,
        IMAGE_PACKAGES,
        PRODUCTION_MODULES,
        SUITES,
        distribution_closure,
    )

    image = {_normal(dist.metadata["Name"]) for dist in distribution_closure(IMAGE_DISTRIBUTIONS)}
    allowed = frozenset(IMAGE_PACKAGES | {
        module for module, dists in packages_distributions().items()
        if any(_normal(dist) in image for dist in dists)})
    sys.meta_path.insert(0, ImageOnly(allowed))
    for name in PRODUCTION_MODULES:
        importlib.import_module(name)
    import pytest

    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    files = sorted({name.split("::", 1)[0] for suite in SUITES.values() for name in suite})
    return pytest.main(["-q", "--collect-only", "-p", "no:cacheprovider", "-c", empty_ini,
                        f"--rootdir={TESTS}", *(str(TESTS / name) for name in files)])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
