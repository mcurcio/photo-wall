"""Fail-closed evidence binding for the real image boot harness."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.boot_fixture import FixtureError
from scripts.test_appliance_e2e import ApplianceE2E, boot_reports, checked_inputs, enrollment

RELEASE = "a" * 64
BOOT = "01234567-89ab-cdef-0123-456789abcdef"
PLAYER = "p-" + "b" * 32


def report(**changes):
    return dict(schema=1, boot_id=BOOT, release_id=RELEASE, slot="A", trial=True,
                persistence="durable", fault=None) | changes


def row(**changes):
    return dict(player_id=PLAYER, authority_epoch=1, persistence="durable", retired=False) | changes


def test_console_noise_and_duplicate_reports_do_not_invent_boots():
    serial = "kernel messages\n" + json.dumps(report()) + "\n" + json.dumps(report())
    assert boot_reports(serial, RELEASE) == [report()]
    assert boot_reports("invalid {not json}\n", RELEASE) == []


@pytest.mark.parametrize("changes", [dict(release_id="c" * 64), dict(persistence="volatile"),
                                     dict(fault="update_storage"), dict(boot_id="invalid")])
def test_faulted_or_wrong_release_boot_cannot_pass(changes):
    with pytest.raises(FixtureError, match="guest_boot_report_invalid"):
        boot_reports(json.dumps(report(**changes)), RELEASE)


def test_old_inventory_cannot_prove_restart_and_changed_identity_fails():
    assert enrollment([]) is None
    assert enrollment([row()], row()) is None
    assert enrollment([row(authority_epoch=2)], row())["authority_epoch"] == 2
    with pytest.raises(FixtureError, match="durable_identity_changed"):
        enrollment([row(player_id="p-" + "c" * 32, authority_epoch=2)], row())
    with pytest.raises(FixtureError, match="unexpected_player_count"):
        enrollment([row(), row()])
    with pytest.raises(FixtureError, match="invalid_guest_enrollment"):
        enrollment([row(persistence="volatile")])


def test_cleanup_refuses_replaced_vm_before_any_removal():
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return ('other-container\nsha256:' + 'd' * 64 + '\npw-vm-fixture\n' +
                json.dumps(dict(Running=True, ExitCode=0, OOMKilled=False))).encode()

    harness = object.__new__(ApplianceE2E)
    harness.run, harness.name = run, "pw-vm-fixture"
    harness.container_id, harness.builder_image = "original-container", "sha256:" + "d" * 64
    with pytest.raises(FixtureError, match="vm_identity_changed"):
        harness.cleanup()
    assert all(args[1] == "inspect" for args in calls)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    def put(path, value):
        path.write_text(json.dumps(value))

    def record(path, content):
        path.write_bytes(content)
        return dict(size=len(content), sha256=hashlib.sha256(content).hexdigest())

    disk = tmp_path / "image.img"
    disk_record = record(disk, b"signed test disk")
    generic = tmp_path / "generic"
    generic.mkdir()
    kernel_record = record(generic / "Image", b"test kernel")
    initrd_record = record(generic / "initrd.img", b"preserved test initrd")
    production = dict(size=10, sha256="e" * 64)
    put(generic / "manifest.json", dict(kind="generic-vm-initramfs",
        validation=dict(reopened=True, protected_bytes_equal=True),
        outputs=dict(kernel=dict(name="Image", **kernel_record), initrd=dict(name="initrd.img", **initrd_record)),
        input=dict(expected_sha256=production["sha256"], expected_size=production["size"])))
    put(tmp_path / "artifact.json", dict(image=disk.name, image_sha256=disk_record["sha256"],
        image_size=disk_record["size"], release_id=RELEASE, pxe_files={"initrd.img": production}))
    for name in ("bundle", "deployment"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr("scripts.boot_gateway.BootBundle.load", lambda *args:
                        SimpleNamespace(release=SimpleNamespace(revision="f" * 40, release_id=RELEASE)))
    manifest = tmp_path / "ci-image.json"
    put(manifest, dict(schema=1, source_commit="f" * 40, disk=dict(path=str(disk), **disk_record),
        generic_boot=str(generic), bundle=str(tmp_path / "bundle"), deployment=str(tmp_path / "deployment")))
    return manifest, disk, generic


def test_image_and_initramfs_are_bound_to_same_final_artifact(inputs):
    manifest, disk, generic = inputs
    assert checked_inputs(manifest)["disk"] == disk
    record = json.loads((generic / "manifest.json").read_text())
    record["input"]["expected_sha256"] = "0" * 64
    (generic / "manifest.json").write_text(json.dumps(record))
    with pytest.raises(FixtureError, match="initramfs_source_mismatch"):
        checked_inputs(manifest)


def test_changed_disk_is_rejected_before_starting_services(inputs):
    manifest, disk, _ = inputs
    disk.write_bytes(b"tampered image")
    with pytest.raises(FixtureError, match="disk_identity_mismatch"):
        checked_inputs(manifest)


def test_changed_generic_kernel_is_rejected(inputs):
    manifest, _, generic = inputs
    (generic / "Image").write_bytes(b"wrong kernel")
    with pytest.raises(FixtureError, match="generic_identity_mismatch"):
        checked_inputs(manifest)
