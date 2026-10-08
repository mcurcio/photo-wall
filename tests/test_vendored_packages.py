"""The vendored-wheel table is the thing under test (E3c S4; erratum E-E3C-CUT-3): each wheel is the one
`uv.lock` pins, no import root has two sources, and the base's launchers reach exactly the vendored
roots they declare."""
from __future__ import annotations

import tomllib

from support.repo import REPO

from scripts.build_node_base_deb import POLICIES
from scripts.debian_packages import PACKAGES
from scripts.module_closure import closure_for
from scripts.vendored_packages import WHEELS, import_table


def test_every_vendored_wheel_is_the_one_uv_lock_pins():
    locked = {package["name"]: package for package in tomllib.loads((REPO / "uv.lock").read_text())["package"]}
    for entry in WHEELS:
        package = locked[entry.distribution]
        assert package["version"] == entry.version, entry.distribution
        assert {"url": entry.url, "hash": f"sha256:{entry.sha256}"} in [
            {"url": wheel["url"], "hash": wheel["hash"]} for wheel in package["wheels"]], entry.distribution
        assert entry.url.endswith("-py3-none-any.whl"), entry.url


def test_no_debian_package_gives_a_vendored_root():
    debian = {root for package in PACKAGES for root in package.imports}
    assert import_table() and not set(import_table()) & debian


def test_the_host_core_launcher_reaches_exactly_nats_among_vendored_roots():
    assert set(closure_for(POLICIES["host-core"]).third_party) & set(import_table()) == {"nats"}
