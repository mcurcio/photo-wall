"""Vendored wheels: third-party Python a base launcher ships inside its own directory, because Debian
does not package it (erratum E-E3C-CUT-3; E3b design §12 change 10).

Each entry is the wheel `uv.lock` pins, by url and sha256 (tests/test_vendored_packages.py binds the
two), with the import roots it gives first-party code. A closure policy's `vendored` table maps a root
to its distribution (`import_table`), and the base builder stages the wheel's every member beside the
launcher's closure (`stage_wheel`), from the pinned download only. One import root has one source: two
wheels, or a wheel and a Debian package (`scripts.debian_packages`), naming one root is refused when
this module is imported.

Build tooling: stdlib only, runs on the builder's own python3.
"""
from __future__ import annotations

import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from scripts.debian_packages import PACKAGES
from scripts.pinned_fetch import cached_pinned


@dataclass(frozen=True, slots=True)
class VendoredWheel:
    distribution: str            # the uv.lock package name
    version: str
    url: str                     # the uv.lock wheel url (py3-none-any)
    sha256: str                  # the uv.lock wheel hash
    imports: tuple[str, ...]     # the import roots it gives first-party code


WHEELS: Final[tuple[VendoredWheel, ...]] = (
    VendoredWheel("nats-py", "2.16.0",
                  "https://files.pythonhosted.org/packages/48/a3/16cec37172144d3362d7551bb823a098b2b2903281fc095dfe4480b8c2fd/nats_py-2.16.0-py3-none-any.whl",
                  "aeb1ff123966c05833d26c7df7e1d54c1c6d32b612428b21677a2e921f1fecae", ("nats",)),
)


def _table() -> Mapping[str, str]:
    debian = {root for package in PACKAGES for root in package.imports}
    table: dict[str, str] = {}
    for entry in WHEELS:
        for root in entry.imports:
            if root in table or root in debian:
                raise ValueError("vendored_import_twice")
            table[root] = entry.distribution
    return MappingProxyType(table)


_IMPORTS: Final = _table()


def import_table() -> Mapping[str, str]:
    """import root -> distribution (read-only): a closure policy's `vendored` table."""
    return _IMPORTS


def wheel(distribution: str) -> VendoredWheel:
    """The entry for `distribution`; KeyError on an unknown name."""
    for entry in WHEELS:
        if entry.distribution == distribution:
            return entry
    raise KeyError(distribution)


def stage_wheel(wheel: VendoredWheel, into: Path, downloads: Path) -> None:
    """Every member of the wheel (its packages and its dist-info, licence included) under `into`, from
    pinned_fetch.cached_pinned(wheel.url, wheel.sha256, downloads): directories 0755, files 0644.
    ValueError("vendored_wheel_member") for a member path that resolves outside `into`."""
    root = Path(into).resolve()
    with zipfile.ZipFile(cached_pinned(wheel.url, wheel.sha256, downloads)) as archive:
        for member in archive.infolist():
            target = (root / member.filename).resolve()
            if target == root or not target.is_relative_to(root):
                raise ValueError("vendored_wheel_member")
            if member.is_dir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
            target.chmod(0o644)
