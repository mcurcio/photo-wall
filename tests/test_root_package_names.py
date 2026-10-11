"""A root's package is judged where the root is built and where it is launched, never when
Central parses a stored document (contracts.node_boot, E-0019-FIX-16): the seal and the Node's
two launchers each refuse a root naming another package. The release writer's own refusal is
tests/test_node_release_writer.py's `test_a_root_sealed_for_another_package_or_abi_is_refused`."""
from __future__ import annotations

import pytest

from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import APP_PACKAGE, MANAGER_PACKAGE

EARLIER_MANAGER = "photo-wall-node-manager"
ABI = {"base_abi": "base-v2", "graphics_abi": "graphics-v1", "plugin_abi": "plugin-v1"}


def reference(deb_name: str) -> AppEnvironmentRefV2:
    return AppEnvironmentRefV2("d" * 64, 128, "e" * 64, deb_name, "1.0.0", "arm64", "f" * 64,
                               "1" * 64, "/usr/bin/app", *ABI.values())


def test_the_seal_refuses_a_deb_of_another_package(tmp_path, monkeypatch):
    from scripts import seal_root
    monkeypatch.setattr(seal_root, "_deb_fields", lambda deb: (EARLIER_MANAGER, "1.0.0"))
    with pytest.raises(seal_root.SealError, match="^seal_root_package_mismatch$"):
        seal_root.seal(tmp_path / "rootfs", role="manager-primary", deb=tmp_path / "x.deb",
                       base_abi={}, display_abi={}, output=tmp_path)


def test_the_manager_launcher_starts_only_a_root_of_the_manager_package(tmp_path, monkeypatch):
    from appliance.node import manager_launcher
    verified = []
    monkeypatch.setattr(manager_launcher, "verify_root", lambda *args, **kwargs: verified.append(args))
    for name, expected in ((EARLIER_MANAGER, False), (APP_PACKAGE, False), (MANAGER_PACKAGE, True)):
        ref = reference(name)
        launcher = manager_launcher.SystemdManagerLauncher(
            tmp_path, {ref.environment_sha256: ref}, **ABI)
        assert launcher.verify(ref.environment_sha256) is expected, name
    assert len(verified) == 1  # only the manager package's root reached the root check


def test_the_app_driver_launches_only_a_root_of_the_player_package(tmp_path):
    from appliance.apps.process_linux import SystemdAppProcessDriver
    driver = SystemdAppProcessDriver(tmp_path, None, **ABI)
    with pytest.raises(ValueError, match="^app_package_kind_mismatch$"):
        driver.verify(reference(MANAGER_PACKAGE))
