"""Each Node launcher's first-party import closure over the source tree, under the packages its
program must never reach.

The launchers (appliance/launchers/<launcher>/__main__.py, installed by photo-wall-node) name
their entry module; the build holds each one's PATH to the package directories that entry's
imports reach (scripts/import_check.py, decision 0019). What a program must not reach inside those
directories (the broker never imports a host module, HostCore never the app lifecycle) is a
property of its closure, held here per launcher and judged by module_closure's refusal of a
forbidden module. Not a test module: the context tests import it.
"""
from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from support.repo import REPO

from scripts import import_check
from scripts.module_closure import Closure, compute_closure, first_party_packages

_EVERY: Final = ("player", "central", "media", "gi")
FORBIDDEN: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType({
    "root-import": ("player", "central", "gi", "appliance.host", "appliance.apps.broker",
                    "appliance.apps.broker_runner", "appliance.apps.process_linux"),
    "node-bootstrap": (*_EVERY, "appliance.host", "appliance.health", "appliance.display_host",
                       "appliance.central_session"),
    "display-controller": _EVERY,
    "host-core": (*_EVERY, "appliance.apps.broker", "appliance.apps.broker_runner",
                  "appliance.apps.process_linux", "appliance.apps.environment",
                  "appliance.apps.lifecycle_storage", "appliance.display_host", "appliance.health",
                  "appliance.node.manager", "appliance.node.manager_desired",
                  "appliance.node.manager_launcher", "appliance.node.manager_observation",
                  "appliance.node.manager_runner", "appliance.node.preparer"),
    "app-broker": (*_EVERY, "appliance.host"),
    "manager-supervisor": _EVERY,
    "health-judge": _EVERY,
})
# The third-party roots a closure may reach (Debian packages, judged by the build's import check).
THIRD_PARTY: Final = MappingProxyType({"nats": "python3-nats", "pydantic": "python3-pydantic"})


def entries() -> Mapping[str, str]:
    """launcher -> ENTRY, read from the source tree's launchers as the build reads them."""
    with tempfile.TemporaryDirectory(prefix="launchers-") as temporary:
        root = Path(temporary) / import_check.COMPOSITION / "usr/lib/photo-wall"
        root.mkdir(parents=True)
        (root / "node").symlink_to(REPO / "appliance/launchers")
        return {name: entry for name, (entry, _path) in
                import_check.launchers(Path(temporary)).items()}


def closure(launcher: str) -> Closure:
    """The launcher's entry closure; ClosureError when it reaches a forbidden module."""
    return compute_closure((entries()[launcher],), repo=REPO,
                           first_party=first_party_packages(REPO),
                           forbidden=FORBIDDEN[launcher], third_party=THIRD_PARTY)
