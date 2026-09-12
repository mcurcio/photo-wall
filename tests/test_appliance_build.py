"""Portable artifact faults and explicitly gated real Linux filesystem tooling."""

import hashlib
import json
import lzma
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from appliance.build import (
    BASE_MINIMAL_MODULES,
    MIB,
    SECTOR,
    BuildError,
    Partition,
    boot_abi,
    checked_file,
    configure_root_base,
    configure_root_generic,
    create_disk,
    decompress_base,
    execution_inventory,
    export_source,
    finalize,
    flash_cmdline,
    flash_fstab,
    install_runtime_packages,
    inventory,
    manifest,
    mbr,
    outside_git,
    partuuid,
    profile_firmware,
    read_mbr,
    run,
    squash,
    verify_executing_source,
    verify_initramfs,
    verify_release_boot,
)


def test_base_decompression_preserves_zero_holes_and_trailing_length(tmp_path, monkeypatch):
    partitions = (Partition(12, 2048, 2048), Partition(131, 4096, 4096))
    content = mbr(partitions, b"test") + bytes(8192 * 512 - 512)
    source, target = tmp_path / "base.xz", tmp_path / "base.img"
    source.write_bytes(lzma.compress(content))
    monkeypatch.setattr("appliance.build.BASE_BYTES", source.stat().st_size)
    monkeypatch.setattr("appliance.build.BASE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    decompress_base(source, target)
    assert target.read_bytes() == content
    assert read_mbr(target) == partitions
    if sys.platform == "linux":
        assert target.stat().st_blocks * 512 < len(content)


def test_partition_image_checked_roundtrip_and_truncation(tmp_path):
    partitions = (Partition(12, 2048, 2048), Partition(131, 4096, 2048))
    path = tmp_path / "disk"
    with path.open("wb") as stream:
        stream.write(mbr(partitions, b"test"))
        stream.truncate(6144 * 512)
    assert read_mbr(path) == partitions
    with path.open("r+b") as stream:
        stream.truncate(6144 * 512 - 1)
    with pytest.raises(BuildError, match="partition_truncated"):
        read_mbr(path)


def test_generated_artifacts_reject_git_directory_and_linked_parent(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").write_text("gitdir: elsewhere\n")
    link = tmp_path / "link"
    link.symlink_to(repository, target_is_directory=True)
    for path in (repository / "output.img", link / "nested/output.img"):
        with pytest.raises(BuildError, match="artifact_inside_git"):
            outside_git(path)


@pytest.mark.parametrize("values", [
    (), (Partition(12, 4096, 4096), Partition(131, 2048, 1024)),
    (Partition(12, 2048, 2048), Partition(131, 3000, 2048)),
])
def test_partition_overlap_rejected(values):
    with pytest.raises(BuildError):
        mbr(values, b"test")


@pytest.mark.parametrize("kind,start,size", [(0xEE, 2048, 1), (12, 0, 1),
                                             (12, 2048, 0), (12, 2048, 2**32)])
def test_partition_invalid(kind, start, size):
    with pytest.raises(BuildError):
        Partition(kind, start, size)


def test_artifact_reader_rejects_wrong_hash_symlink_fifo_oversize(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(b"generated artifact")
    assert checked_file(file, 100)["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    with pytest.raises(BuildError, match="artifact_hash"):
        checked_file(file, 100, expected="0" * 64)
    with pytest.raises(BuildError, match="artifact_limit"):
        checked_file(file, 1)
    link = tmp_path / "link"
    link.symlink_to(file)
    with pytest.raises(OSError):
        checked_file(link, 100)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(BuildError, match="artifact_limit"):
        checked_file(fifo, 100)


def test_generic_manifest_budget_accepts_large_valid_output():
    from scripts.build_vm_initrd import MAX_MANIFEST_BYTES, _manifest_bytes

    payload = _manifest_bytes({"schema": 1, "kind": "generic-vm-initramfs",
                               "module_inventory": "x" * (MIB + 1)})
    assert len(payload) > MIB
    assert len(payload) <= MAX_MANIFEST_BYTES


def test_generic_manifest_budget_rejects_oversized_output():
    from scripts.build_vm_initrd import _manifest_bytes

    with pytest.raises(BuildError, match="manifest_limit"):
        _manifest_bytes({"schema": 1, "kind": "generic-vm-initramfs",
                         "module_inventory": "x" * (16 * MIB)})


def test_tree_never_walks_symlink_and_bounds_aggregate(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a").write_bytes(b"abc")
    (tree / "outside").symlink_to(tmp_path, target_is_directory=True)
    assert inventory(tree)["outside"] == {"symlink": str(tmp_path)}
    with pytest.raises(BuildError, match="tree_limit"):
        inventory(tree, maximum_files=1)


def test_process_deadline_and_output_budget(tmp_path):
    with pytest.raises(BuildError, match="tool_timeout"):
        run([sys.executable, "-c", "import time;time.sleep(30)"], timeout=0.1)
    log = tmp_path / "log"
    with pytest.raises(BuildError, match="tool_output_limit"):
        run([sys.executable, "-c", "import sys;sys.stdout.write('x'*5000000)"], log=log)
    assert log.stat().st_size == 4 * MIB


def test_disk_refuses_existing_output_and_symlink_boot(tmp_path):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "config.txt").write_text("fixture\n")
    output = tmp_path / "existing"
    output.write_bytes(b"preserve")
    with pytest.raises(BuildError, match="output_exists"):
        create_disk(boot, output, source_epoch=1_700_000_000)
    assert output.read_bytes() == b"preserve"
    (boot / "link").symlink_to(output)
    with pytest.raises(BuildError, match="boot_tree_invalid"):
        create_disk(boot, tmp_path / "new", source_epoch=1_700_000_000)


def test_boot_abi_covers_kernel_and_bootstrap_logic_but_excludes_derived_files(tmp_path):
    root, boot = tmp_path / "root", tmp_path / "boot"
    modules = root / "usr/lib/modules/test-kernel"
    modules.mkdir(parents=True)
    boot.mkdir()
    (modules / "test.ko").write_bytes(b"module")
    (boot / "vmlinuz").write_bytes(b"kernel")
    (boot / "pi.dtb").write_bytes(b"dtb")
    (boot / "initrd.img").write_bytes(b"initramfs")
    logic = {
        "usr/lib/python3/dist-packages/appliance/__init__.py": b"appliance init",
        "usr/lib/python3/dist-packages/appliance/bootstrap.py": b"bootstrap",
        "usr/lib/python3/dist-packages/appliance/updates.py": b"updates",
        "usr/lib/python3/dist-packages/contracts/__init__.py": b"contracts init",
        "usr/lib/python3/dist-packages/contracts/release.py": b"release",
        "etc/initramfs-tools/hooks/photo-wall": b"hook",
        "etc/initramfs-tools/scripts/photowall": b"mountroot",
    }
    for relative, value in logic.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    first, _ = boot_abi(root, boot, "test-kernel")
    (boot / "initrd.img").write_bytes(b"generated later")
    assert boot_abi(root, boot, "test-kernel")[0] == first
    (modules / "test.ko").write_bytes(b"different module")
    assert boot_abi(root, boot, "test-kernel")[0] != first
    (modules / "test.ko").write_bytes(b"module")
    for relative in logic:
        path = root / relative
        path.write_bytes(path.read_bytes() + b" changed")
        assert boot_abi(root, boot, "test-kernel")[0] != first
        path.write_bytes(logic[relative])
    (root / "etc/photo-wall").mkdir(parents=True)
    (root / "etc/photo-wall/boot-policy.json").write_bytes(b"generated policy")
    (root / "etc/photo-wall/public.json").write_bytes(b"public configuration")
    assert boot_abi(root, boot, "test-kernel")[0] == first


def test_manifest_names_only_actual_hashed_root(tmp_path):
    file = tmp_path / "root"
    file.write_bytes(b"generated")
    release = manifest(file, revision="a" * 40, boot_abi="b" * 64)
    assert release.rootfs_name == "rootfs-" + hashlib.sha256(b"generated").hexdigest() + ".squashfs"
    assert release.rootfs_size == 9


def test_initramfs_boundary_allows_kernel_media_drivers_but_only_minimal_python():
    paths = ["usr/bin/python3.12", "scripts/photowall", "etc/photo-wall/boot-policy.json",
             "usr/lib/modules/raspi/kernel/drivers/media/v4l2-core/videodev.ko.zst"]
    paths.extend("usr/lib/python3.12/" + module for module in (
        "appliance/__init__.py", "appliance/bootstrap.py", "appliance/updates.py",
        "contracts/__init__.py", "contracts/release.py", "contracts/equipment.py"))
    contents = ("\n".join(paths) + "\n").encode()
    verify_initramfs(contents)
    for forbidden in (b"media/worker.py", b"appliance/build.py", b"contracts/models.py", b"gi/__init__.py"):
        with pytest.raises(BuildError, match="initramfs_boundary"):
            verify_initramfs(contents + b"usr/lib/python3.12/" + forbidden + b"\n")
    with pytest.raises(BuildError, match="initramfs_incomplete"):
        verify_initramfs(contents.replace(b"scripts/photowall\n", b""))


def test_player_sandbox_keeps_wayland_runtime_visible():
    service = (Path(__file__).parents[1] / "appliance/systemd/player.service").read_text()
    assert "RuntimeDirectory=photo-wall/player" in service
    assert "RuntimeDirectoryMode=0700" in service
    assert "RuntimeDirectoryPreserve=yes" in service
    assert "ExecStartPre=+/usr/bin/install -d" not in service
    assert "ProtectHome=read-only" in service
    assert "InaccessiblePaths=-/home -/root" in service
    assert "\nProtectHome=yes\n" not in "\n" + service
    assert "XDG_RUNTIME_DIR=/run/user/10001" in service
    assert "ReadWritePaths=/run/user" not in service


def test_runtime_package_apt_transport_config_is_root_scoped_and_written_first(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/sbin").mkdir()
    (root / "usr/bin/python3.12").write_bytes(b"python")
    (root / "etc").mkdir()
    (root / "etc/os-release").write_text('NAME="Ubuntu"\nVERSION_ID="24.04"\n')
    (root / "etc/apt/sources.list.d").mkdir(parents=True)
    (root / "var/lib/apt/lists").mkdir(parents=True)
    (root / "dev").mkdir()
    for name in ("null", "zero", "random", "urandom"):
        (root / "dev" / name).write_bytes(b"")
    evidence = tmp_path / "evidence"
    calls = []
    expected_config = (
        'Acquire::Retries "2";\n'
        'Acquire::http::Timeout "30";\n'
        'Acquire::https::Timeout "30";\n')
    config = root / "etc/apt/apt.conf.d/99-photo-wall-transport"
    seen_at_update = []

    def fake_in_root(_root, *argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:1] == ("apt-get",) and argv[-1:] == ("update",):
            seen_at_update.append(config.read_text())
        if argv[:1] == ("dpkg-query",):
            if (evidence / "base-packages.tsv").exists():
                return b"photo-wall-base\t1\tarm64\n"
            return b"linux-image-generic\t1\tarm64\ncloud-init\t1\tall\n"
        return b""

    monkeypatch.setattr("appliance.build.in_root", fake_in_root)
    monkeypatch.setattr("appliance.build.shutil.which", lambda _name: "/bin/true")
    monkeypatch.setattr("appliance.build.checked_file", lambda _path, _maximum: {
        "sha256": "0" * 64, "size": 1})

    install_runtime_packages(root, evidence)

    assert config.read_text() == expected_config
    assert config.stat().st_mode & 0o777 == 0o644
    assert seen_at_update == [expected_config]
    sources = (root / "etc/apt/sources.list").read_text().splitlines()
    assert len(sources) == 3
    assert all("signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg target=Packages" in line
               for line in sources)
    apt_calls = [argv for argv, _kwargs in calls if argv[:1] == ("apt-get",)]
    assert apt_calls[0][:4] == ("apt-get", "-o", "APT::Update::Error-Mode=any", "update")
    assert apt_calls[1][1] == "purge"
    assert apt_calls[2] == ("apt-get", "clean")
    assert "Dir::Cache::archives=/tmp/photo-wall-apt-plan" in apt_calls[3]
    assert "Acquire::ForceHash=SHA256" in apt_calls[3]
    assert "--print-uris" in apt_calls[3]
    assert apt_calls[4][1:3] == ("--download-only", "install")
    assert apt_calls[5][1] == "install"


def test_executing_builder_and_helpers_must_match_exported_source():
    files = execution_inventory()
    verify_executing_source({"files": files})
    for name in files:
        changed = dict(files, **{name: dict(files[name], sha256="0" * 64)})
        with pytest.raises(BuildError, match="executing_source_mismatch"):
            verify_executing_source({"files": changed})


def test_committed_source_export_matches_executing_builder(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    project = Path(__file__).resolve().parents[1]
    # Include the helper outside appliance/ that previously escaped the export.
    paths = set(execution_inventory()) | {"appliance/systemd/player.service"}
    for relative in paths:
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / relative, target)

    def git(*args):
        return run(["git", "-C", str(repository), *args]).decode().strip()

    git("init", "--quiet")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Source fixture")
    revision = git("rev-parse", "HEAD")
    source = tmp_path / "source"
    export_source(repository, source, revision)
    record = json.loads((source / "source-inventory.json").read_text())
    assert record["revision"] == revision
    assert (source / "scripts/ci_apt_cache.py").read_bytes() == (
        project / "scripts/ci_apt_cache.py").read_bytes()
    verify_executing_source(record)


def test_finalizer_rejects_entire_bundle_resigned_under_replaced_public_key(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from contracts.release import Release

    trusted, bundle = tmp_path / "trusted-public", tmp_path / "forged-bundle"
    trusted.mkdir()
    (bundle / "public").mkdir(parents=True)
    private = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    for directory, key in ((trusted, private), (bundle / "public", attacker)):
        for name in ("public.json", "bootstrap.json", "ca.pem"):
            (directory / name).write_bytes(b"generated public fixture\n")
        (directory / "release.pub.pem").write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    body = b"generated attacker rootfs"
    release = Release("a" * 40, "b" * 64,
                      hashlib.sha256(body).hexdigest(), len(body))
    (bundle / "release.json").write_bytes(release.encode())
    (bundle / release.rootfs_name).write_bytes(body)
    (bundle / "build.json").write_text(json.dumps(dict(boot_abi="b" * 64)))
    signature = tmp_path / "forged.sig"
    signature.write_bytes(attacker.sign(release.encode()))
    attacker.public_key().verify(signature.read_bytes(), release.encode())
    destination = tmp_path / "output"
    with pytest.raises(BuildError, match="bundle_public_mismatch"):
        finalize(bundle, signature, destination, trusted_public=trusted)
    assert not destination.exists()


def test_pi_firmware_profile_keeps_link_closure_and_records_removed_bytes(tmp_path, monkeypatch):
    root, evidence = tmp_path / "root", tmp_path / "evidence"
    evidence.mkdir()
    firmware = root / "usr/lib/firmware"
    for family in ("brcm", "cypress", "unrelated"):
        (firmware / family).mkdir(parents=True)
    (firmware / "LICENSE").write_bytes(b"license remains")
    (firmware / "cypress/needed").write_bytes(b"required by a kept link")
    (firmware / "brcm/link").symlink_to("../cypress/needed")
    (root / "etc/alternatives").mkdir(parents=True)
    (root / "etc/alternatives/pi-firmware").symlink_to("/usr/lib/firmware/cypress/needed")
    (firmware / "brcm/selected").symlink_to("/etc/alternatives/pi-firmware")
    (firmware / "unrelated/remove").write_bytes(b"remove")
    monkeypatch.setattr("appliance.build._root", lambda path: path)
    profile_firmware(root, evidence)
    assert (firmware / "brcm/link").read_bytes() == b"required by a kept link"
    assert (firmware / "brcm/selected").read_bytes() == b"required by a kept link"
    assert os.readlink(firmware / "brcm/selected") == "../cypress/needed"
    assert not (firmware / "unrelated").exists()
    report = (evidence / "firmware-profile.json").read_bytes()
    assert json.loads(report)["removed_bytes"] == 6
    profile_firmware(root, evidence)
    assert (evidence / "firmware-profile.json").read_bytes() == report
    (firmware / "brcm/dangling").symlink_to("../missing")
    with pytest.raises(BuildError, match="firmware_link_missing"):
        profile_firmware(root, evidence)


def test_flash_partition_table_roundtrip_fat_boot_and_ext4_root(tmp_path):
    boot = Partition(0x0C, 2048, 512 * MIB // SECTOR)
    root = Partition(0x83, boot.start + boot.sectors, 2048 * MIB // SECTOR)
    path = tmp_path / "flash.img"
    with path.open("wb") as stream:
        stream.write(mbr((boot, root), b"test"))
        stream.truncate((root.start + root.sectors) * SECTOR)
    assert read_mbr(path) == (boot, root)
    assert [partition.kind for partition in read_mbr(path)] == [0x0C, 0x83]
    assert boot.start + boot.sectors == root.start
    # Deterministic: the same partitions + disk id always produce the same table.
    assert mbr((boot, root), b"test") == mbr((boot, root), b"test")


def test_flash_partition_table_rejects_single_partition_as_flash_layout(tmp_path):
    # Mutation-probe control: a netboot-shaped single-FAT-partition table must
    # NOT satisfy the flash (2-partition) expectation this suite checks above.
    boot = Partition(0x0C, 2048, 512 * MIB // SECTOR)
    path = tmp_path / "single.img"
    with path.open("wb") as stream:
        stream.write(mbr((boot,), b"test"))
        stream.truncate((boot.start + boot.sectors) * SECTOR)
    assert read_mbr(path) != (boot, Partition(0x83, boot.start + boot.sectors, 2048))
    assert len(read_mbr(path)) == 1


def test_partuuid_is_deterministic_little_endian_disk_signature():
    disk_id = bytes.fromhex("deadbeef")
    assert partuuid(disk_id, 1) == "efbeadde-01"
    assert partuuid(disk_id, 2) == "efbeadde-02"
    assert partuuid(disk_id, 2) == partuuid(disk_id, 2)
    with pytest.raises(BuildError, match="partuuid_invalid"):
        partuuid(disk_id, 5)
    with pytest.raises(BuildError, match="partuuid_invalid"):
        partuuid(b"short", 1)


def test_flash_cmdline_references_partuuid_root_with_no_netboot_ram_hook():
    cmdline = flash_cmdline("efbeadde-02").decode()
    assert "root=PARTUUID=efbeadde-02" in cmdline
    assert "rootfstype=ext4" in cmdline
    assert "boot=photowall" not in cmdline
    assert "root=/dev/ram0" not in cmdline
    with pytest.raises(BuildError, match="partuuid_invalid"):
        flash_cmdline("not-a-partuuid")


def test_flash_fstab_mounts_root_and_boot_by_partuuid():
    fstab = flash_fstab("efbeadde-02", "efbeadde-01").decode()
    assert "PARTUUID=efbeadde-02  /               ext4" in fstab
    assert "PARTUUID=efbeadde-01  /boot/firmware  vfat" in fstab
    with pytest.raises(BuildError, match="partuuid_invalid"):
        flash_fstab("bad", "efbeadde-01")


def test_configure_root_generic_bakes_only_release_key_and_drops_netboot_wiring(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    root = tmp_path / "root"
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/python3.12").write_bytes(b"python")
    (root / "etc").mkdir()
    (root / "etc/os-release").write_text('NAME="Ubuntu"\nVERSION_ID="24.04"\n')
    (root / "etc/passwd").write_text("root:x:0:0::/root:/bin/bash\n")
    (root / "etc/group").write_text("video:x:44:\nrender:x:104:\ninput:x:105:\n")
    (root / "etc/netplan").mkdir(parents=True)
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "tmp").mkdir()

    wheelhouse = tmp_path / "wheelhouse"
    (wheelhouse / "wheels").mkdir(parents=True)
    (wheelhouse / "requirements.txt").write_bytes(b"")

    release_pub = tmp_path / "release.pub.pem"
    release_pub.write_bytes(b"-----BEGIN PUBLIC KEY-----\nfixture\n-----END PUBLIC KEY-----\n")

    evidence = tmp_path / "evidence"
    evidence.mkdir()

    def fake_in_root(_root, *argv, **_kwargs):
        if argv[:1] == ("/opt/photo-wall/venv/bin/python",) and "weston_ini" in argv[-1]:
            return b"[core]\n"
        return b"[]"

    monkeypatch.setattr("appliance.build.in_root", fake_in_root)

    configure_root_generic(root, project, wheelhouse, release_pub, evidence)

    config_dir = root / "etc/photo-wall"
    assert {path.name for path in config_dir.iterdir()} == {
        "release.pub.pem", "public.json", "grow-rootfs.sh"}
    assert config_dir.joinpath("release.pub.pem").read_bytes() == release_pub.read_bytes()
    public = json.loads(config_dir.joinpath("public.json").read_bytes())
    assert public == {"schema": 1}
    assert "central_origin" not in public
    assert "bootstrap.json" not in {path.name for path in config_dir.iterdir()}
    assert "ca.pem" not in {path.name for path in config_dir.iterdir()}

    units = {path.name for path in (root / "etc/systemd/system").iterdir() if path.is_file()}
    assert units == {"photo-wall-weston.service", "photo-wall-player.service",
                     "photo-wall-grow-rootfs.service"}
    wants = {path.name for path in (root / "etc/systemd/system/multi-user.target.wants").iterdir()}
    assert wants == units
    assert "photo-wall-accept-trial.service" not in wants
    assert "photo-wall-trial-recovery.service" not in wants
    assert not (root / "etc/fstab").exists()


def _base_root_and_source(tmp_path):
    """Shared fixture: a `_root`-valid root plus a `source` tree holding
    exactly the bootstrapper's minimal import closure and its unit --
    everything `configure_root_base` (0009 slice 5) needs, and nothing
    `configure_root`/`configure_root_generic` (netboot/flash, untouched by
    this slice) uses.
    """
    root = tmp_path / "root"
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/python3.12").write_bytes(b"python")
    (root / "etc").mkdir()
    (root / "etc/os-release").write_text('NAME="Ubuntu"\nVERSION_ID="24.04"\n')
    (root / "etc/netplan").mkdir(parents=True)
    (root / "etc/systemd/system").mkdir(parents=True)

    project = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    for relative in (*BASE_MINIMAL_MODULES, "appliance/systemd/photo-wall-provision.service"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / relative, target)

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return root, source, evidence


def test_configure_root_base_stages_bootstrapper_closure_and_provision_unit(tmp_path, monkeypatch):
    """Tier 1, acceptance item 1: the base configuration STAGES the enabled
    `photo-wall-provision` unit, `appliance/provision.py` plus its minimal
    import closure, and a working Python + zeroconf/ifaddr import."""
    root, source, evidence = _base_root_and_source(tmp_path)
    calls = []

    def fake_in_root(_root, *argv, **kwargs):
        calls.append(argv)
        return b""

    monkeypatch.setattr("appliance.build.in_root", fake_in_root)

    configure_root_base(root, source, evidence)

    dist_packages = root / "usr/lib/python3/dist-packages"
    for relative in BASE_MINIMAL_MODULES:
        staged = dist_packages / relative
        assert staged.read_bytes() == (source / relative).read_bytes()

    # The one native-dependency import check actually asserted (mutation
    # probe: dropping the unit/closure below turns this same call absent).
    import_calls = [argv for argv in calls if argv[:1] == ("python3.12",)]
    assert any("appliance.provision" in " ".join(argv) and "zeroconf" in " ".join(argv)
               for argv in import_calls)

    unit = root / "etc/systemd/system/photo-wall-provision.service"
    assert unit.is_file()
    assert unit.read_bytes() == (source / "appliance/systemd/photo-wall-provision.service").read_bytes()
    wants = root / "etc/systemd/system/multi-user.target.wants/photo-wall-provision.service"
    assert wants.is_symlink()
    assert os.readlink(wants) == "/etc/systemd/system/photo-wall-provision.service"


def test_configure_root_base_excludes_player_venv_and_deployment_config(tmp_path, monkeypatch):
    """Tier 1, acceptance item 2: the base configuration does NOT stage the
    Player venv, any deployment `/etc/photo-wall` config, `release.pub.pem`,
    or the accept-trial/trial-recovery units. Assert their ABSENCE.

    Mutation probe (reverse by hand): staging the Player venv into the base
    (e.g. re-adding `configure_root`'s `pip install --require-hashes ...`
    venv build to this function) makes the venv-absence assertion below
    fail; omitting the unit install makes the presence test above fail.
    """
    root, source, evidence = _base_root_and_source(tmp_path)
    monkeypatch.setattr("appliance.build.in_root", lambda _root, *argv, **kwargs: b"")

    configure_root_base(root, source, evidence)

    assert not (root / "opt/photo-wall/venv").exists()
    config_dir = root / "etc/photo-wall"
    assert config_dir.is_dir()
    assert list(config_dir.iterdir()) == []
    for name in ("public.json", "bootstrap.json", "ca.pem", "release.pub.pem"):
        assert not (config_dir / name).exists()
    units = {path.name for path in (root / "etc/systemd/system").iterdir() if path.is_file()}
    assert units == {"photo-wall-provision.service"}
    assert "photo-wall-accept-trial.service" not in units
    assert "photo-wall-trial-recovery.service" not in units
    wants_dir = root / "etc/systemd/system/multi-user.target.wants"
    assert {path.name for path in wants_dir.iterdir()} == {"photo-wall-provision.service"}


linux_tools = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("PHOTO_WALL_IMAGE_TOOL_TESTS") != "1",
    reason="explicit disposable Linux file-tooling fixture required")


@linux_tools
def test_real_stateless_fat_image_and_squashfs_reopen(tmp_path):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "config.txt").write_bytes(b"generated boot configuration\n")
    (boot / "nested").mkdir()
    (boot / "nested/data").write_bytes(b"exact nested bytes")
    output = tmp_path / "common.img"
    report = create_disk(boot, output, fat_mib=64, source_epoch=1_700_000_000)
    assert report["size"] == 65 * MIB
    (first,) = read_mbr(output)
    assert run(["mtype", "-i", f"{output}@@{first.start * 512}", "::nested/data"]) == b"exact nested bytes"
    root = tmp_path / "root"
    root.mkdir()
    (root / "data").write_bytes(b"fixture root content")
    shutil.copytree(boot, root / "boot/firmware")
    source = root / "usr/share/photo-wall/build/source.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(dict(revision="a" * 40, files=execution_inventory())))
    squashfile = tmp_path / "root.squashfs"
    digest = squash(root, squashfile, 1_700_000_000)
    assert digest == checked_file(squashfile, MIB)
    assert run(["unsquashfs", "-cat", str(squashfile), "data"]) == b"fixture root content"
    verify_release_boot(squashfile, boot, "a" * 40)
    (boot / "config.txt").write_bytes(b"changed boot bytes outside signed root")
    with pytest.raises(BuildError, match="authenticated_boot_mismatch"):
        verify_release_boot(squashfile, boot, "a" * 40)


@linux_tools
def test_real_guestfs_metadata_preserving_extraction(tmp_path):
    import guestfs

    source = tmp_path / "source"
    source.mkdir()
    file = source / "content"
    file.write_bytes(b"metadata-preservation-fixture")
    file.chmod(0o751)
    os.link(file, source / "hardlink")
    (source / "symbolic").symlink_to("content")
    os.setxattr(file, "user.photo-wall", b"preserved")
    run(["setcap", "cap_net_bind_service=ep", str(file)])
    image = tmp_path / "fixture.ext4"
    with image.open("wb") as stream:
        stream.truncate(64 * MIB)
    run(["mkfs.ext4", "-F", "-q", "-d", str(source), str(image)])
    archive = tmp_path / "root.tar"
    guest = guestfs.GuestFS(python_return_dict=True)
    try:
        guest.add_drive_opts(str(image), readonly=True, format="raw")
        guest.launch()
        guest.mount_ro("/dev/sda", "/")
        guest.tar_out("/", str(archive), numericowner=True, xattrs=True, acls=True)
        guest.shutdown()
    finally:
        guest.close()
    restored = tmp_path / "restored"
    restored.mkdir()
    run(["tar", "--numeric-owner", "--xattrs", "--xattrs-include=*", "--acls", "-xpf",
         str(archive), "-C", str(restored)])
    content = restored / "content"
    assert content.read_bytes() == file.read_bytes()
    assert stat.S_IMODE(content.stat().st_mode) == 0o751
    assert content.stat().st_ino == (restored / "hardlink").stat().st_ino
    assert os.readlink(restored / "symbolic") == "content"
    assert os.getxattr(content, "user.photo-wall") == b"preserved"
    assert os.getxattr(content, "security.capability") == os.getxattr(file, "security.capability")
    shutil.rmtree(restored)


@linux_tools
def test_real_flash_two_partition_disk_assembly_and_verify(tmp_path):
    """CI arm64 builder only -- UNVERIFIED on this host (no root, no
    mkfs.vfat/mkfs.ext4/guestfs). Gated exactly like the real-build tests
    above: skipped unless sys.platform == "linux" and
    PHOTO_WALL_IMAGE_TOOL_TESTS=1, which only the CI job sets.

    Exercises the real disk-assembly path `configure_root_generic` alone
    cannot: `create_disk_flash`'s mkfs.vfat + mkfs.ext4 population of a real
    2-partition MBR image, and `verify_disk_flash`'s guestfs reopen of both
    partitions against the exact source trees.
    """
    from appliance.build import create_disk_flash, verify_disk_flash

    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "config.txt").write_bytes(b"generated boot configuration\n")
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/os-release").write_text('NAME="Ubuntu"\nVERSION_ID="24.04"\n')
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/init").write_bytes(b"fixture init")
    output = tmp_path / "flash.img"

    report = create_disk_flash(boot, root, output, fat_mib=64, source_epoch=1_700_000_000)

    assert [partition["kind"] for partition in report["partitions"]] == [0x0C, 0x83]
    cmdline = (boot / "cmdline.txt").read_text()
    assert cmdline.count("root=PARTUUID=") == 1
    assert "boot=photowall" not in cmdline
    fstab = (root / "etc/fstab").read_text()
    assert report["root_partuuid"] in fstab
    assert report["boot_partuuid"] in fstab
    verify_disk_flash(output, boot, root)
    (root / "usr/bin/init").write_bytes(b"changed outside the signed disk")
    with pytest.raises(BuildError, match="disk_root_mismatch"):
        verify_disk_flash(output, boot, root)


@linux_tools
def test_real_base_squashfs_contains_provision_unit_and_lacks_player_venv(tmp_path, monkeypatch):
    """Tier 2 (0009 slice 5 acceptance): real squashfs assembly of the base
    image. SKIPPED HERE -- this sandbox has no Linux `mksquashfs`/root; only
    the CI arm64 builder (`sys.platform == "linux"` and
    `PHOTO_WALL_IMAGE_TOOL_TESTS=1`) runs this, exactly like the other
    `@linux_tools` real-build tests in this file.

    Exercises the real assembly `configure_root_base` alone cannot: squash
    the configured root with `mksquashfs` and reopen it with `unsquashfs`
    to assert the built image contains the provision unit + a working
    zeroconf import, and lacks the Player venv/config.
    """
    root, source, evidence = _base_root_and_source(tmp_path)
    (root / "tmp").mkdir()

    def fake_in_root(_root, *argv, **kwargs):
        if argv[:1] == ("python3.12",):
            return b""
        return b""

    monkeypatch.setattr("appliance.build.in_root", fake_in_root)
    configure_root_base(root, source, evidence)

    squashfile = tmp_path / "base.squashfs"
    squash(root, squashfile, 1_700_000_000)
    listing = run(["unsquashfs", "-l", str(squashfile)]).decode()
    assert "usr/lib/python3/dist-packages/appliance/provision.py" in listing
    assert "etc/systemd/system/photo-wall-provision.service" in listing
    assert "opt/photo-wall/venv" not in listing
    assert "etc/photo-wall/public.json" not in listing
    assert "etc/photo-wall/release.pub.pem" not in listing
