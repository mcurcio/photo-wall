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
