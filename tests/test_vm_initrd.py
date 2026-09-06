"""Bounded generic VM initramfs module and GPU preload checks."""

from pathlib import Path

import pytest

from appliance.build import BuildError
from scripts import build_vm_initrd

RELEASE = "6.8.0-generic"


def _module_tree(tmp_path: Path, *, dependency: str = "kernel/drivers/gpu/drm/drm.ko") -> Path:
    root = tmp_path / "root"
    module_root = root / "lib/modules" / RELEASE
    (module_root / "kernel/drivers/gpu/drm/virtio").mkdir(parents=True)
    (module_root / "kernel/drivers/gpu/drm/virtio/virtio-gpu.ko").write_bytes(b"gpu")
    (module_root / "kernel/drivers/gpu/drm/drm.ko").write_bytes(b"drm")
    (module_root / "modules.dep").write_text(
        "kernel/drivers/gpu/drm/virtio/virtio-gpu.ko: " + dependency + "\n"
        "kernel/drivers/gpu/drm/drm.ko:\n",
        encoding="utf-8",
    )
    return root


def test_gpu_closure_normalizes_hyphenated_module_and_proves_dependency(tmp_path):
    root = _module_tree(tmp_path)

    assert build_vm_initrd._module_status(root, "virtio_gpu", RELEASE) == "module"
    assert build_vm_initrd._module_dependency_closure(root, RELEASE) == ("drm", "virtio_gpu")


def test_gpu_closure_fails_closed_for_missing_dependency(tmp_path):
    root = _module_tree(tmp_path, dependency="kernel/drivers/gpu/drm/missing.ko")

    with pytest.raises(BuildError, match="generic_gpu_dependency_missing"):
        build_vm_initrd._module_dependency_closure(root, RELEASE)


def test_gpu_closure_rejects_tampered_dependency_metadata(tmp_path):
    root = _module_tree(tmp_path)
    (root / "lib/modules" / RELEASE / "modules.dep").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(BuildError, match="generic_modules_dep_invalid"):
        build_vm_initrd._module_dependency_closure(root, RELEASE)


def test_virtio_9p_preload_closure_proves_transitive_dependencies(tmp_path):
    root = _module_tree(tmp_path)
    module_root = root / "lib/modules" / RELEASE
    (module_root / "fs/9p").mkdir(parents=True)
    (module_root / "net/9p").mkdir(parents=True)
    (module_root / "net/9p/9pnet_virtio.ko").write_bytes(b"virtio")
    (module_root / "net/9p/9pnet.ko").write_bytes(b"net")
    (module_root / "fs/9p/9p.ko").write_bytes(b"9p")
    (module_root / "modules.dep").write_text(
        "kernel/drivers/gpu/drm/virtio/virtio-gpu.ko: kernel/drivers/gpu/drm/drm.ko\n"
        "kernel/drivers/gpu/drm/drm.ko:\n"
        "fs/9p/9p.ko:\n"
        "net/9p/9pnet.ko: fs/9p/9p.ko\n"
        "net/9p/9pnet_virtio.ko: net/9p/9pnet.ko\n",
        encoding="utf-8",
    )

    assert build_vm_initrd._module_dependency_closure(
        root, RELEASE, "9pnet_virtio") == ("9p", "9pnet", "9pnet_virtio")


def test_hook_is_read_only_share_guarded_and_ordered_before_existing_entries():
    assert b"mount -t 9p -o trans=virtio,version=9p2000.L,ro photo-wall-ci" in build_vm_initrd.HOOK_BYTES
    assert b"ExecStart=/usr/bin/python3 -I /run/photo-wall-ci/vm_rollback_control.py" in build_vm_initrd.HOOK_BYTES
    assert b"Restart=no" in build_vm_initrd.HOOK_BYTES
    assert b"After=photo-wall-accept-trial.service" in build_vm_initrd.HOOK_BYTES
    assert build_vm_initrd.ORDER_ADDITION.startswith(b"/scripts/init-bottom/photo-wall-evidence")


def test_module_preload_additions_are_exact_and_idempotent(tmp_path):
    segment = tmp_path / "main"
    (segment / "conf").mkdir(parents=True)
    config = segment / "conf/modules"
    config.write_text("# existing\nvirtio_gpu\n", encoding="utf-8")

    first = build_vm_initrd._append_modules([segment], ("virtio_gpu", "drm"))
    after_first = config.read_bytes()
    second = build_vm_initrd._append_modules([segment], ("virtio_gpu", "drm"))

    assert first == ["drm"]
    assert second == []
    assert after_first == config.read_bytes()
    assert build_vm_initrd._configured_modules([segment]) == {"virtio_gpu", "drm"}


def test_required_preloads_include_qemu_gpu_and_9p_transport():
    assert {"virtio_gpu", "9p", "9pnet", "9pnet_virtio"} <= set(build_vm_initrd.REQUIRED_MODULES)
    assert set(build_vm_initrd.PRELOAD_ROOTS) == {"virtio_gpu", "9p", "9pnet", "9pnet_virtio"}


def test_health_observer_is_independent_read_only_and_shell_valid():
    import subprocess

    hook = build_vm_initrd.HOOK_BYTES
    health = hook.split(b"PHOTO_WALL_CI_HEALTH'\n", 1)[1].split(b"PHOTO_WALL_CI_HEALTH\n", 1)[0]
    assert b"After=photo-wall-player.service\n" in health
    assert b"accept-trial" not in health and b"Requires=" not in health
    assert b"ProtectSystem=strict\n" in health and b"ReadWritePaths=" not in health
    assert b"RuntimeMaxSec=620\n" in health and b"Restart=no\n" in health
    assert b"ExecStart=/usr/bin/python3 -I /run/photo-wall-ci/vm_health_probe.py\n" in health
    subprocess.run(["/bin/sh", "-n"], input=hook, check=True)
