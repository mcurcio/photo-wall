"""Release roots as images (E2c): `stage_image` seals, adopts by rename, hashes by descriptor,
mounts through the `ImageMounter` and runs the release check; `mounted_root` recomputes the
staged predicate at every use; `SystemdImageMounter` reads mountinfo and the loop's sysfs.

Portable: a fake mounter stands in for PID1 (its "mount" copies a real sealed tree to the mount
point), and the pool's owner is the test's own uid (`environment.ROOT_UID`), so no root is
needed. The real loop mounts are the node-pid1 legs' (`tests/test_node_pid1.py`).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from node.test_node_linux_adapters import fixture_archive
from support.image_mount import FakeImageMounter
from support.repo import REPO

from appliance.apps import environment
from appliance.apps.environment import mounted_root, stage_image
from appliance.kernel import image_mount
from appliance.kernel.capacity import line
from appliance.kernel.image_mount import IMAGE_MOUNT_OPTIONS, NO_IMAGE, SystemdImageMounter
from scripts import build_node_base_deb as base
from scripts.build_environment_image import EnvironmentImage
from scripts.build_node_components import reproducible_image
from scripts.sealed_archive import stage_archive

ABI = {"base_abi": "base-v2", "graphics_abi": "graphics-v2", "plugin_abi": "plugins-v2"}
IMAGE_BYTES = b"a squashfs image stands here " * 64


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A sealed tree (what the image's mount shows), the image's ref, and an empty store."""
    monkeypatch.setattr(environment, "ROOT_UID", os.getuid())
    monkeypatch.setattr(environment, "ROOT_GID", os.getgid())
    # The Player's package name: the broker's launch check also checks the package kind.
    archive, tar_reference = fixture_archive(tmp_path / "build", deb_name="photo-wall-player")
    built = tmp_path / "built"
    built.mkdir()
    tree = stage_archive(archive, built, tar_reference, **ABI, owner_uid=os.getuid())
    sha = hashlib.sha256(IMAGE_BYTES).hexdigest()
    # The manifest's reference omits the digest and size: the image's ref pairs with the tree.
    reference = replace(tar_reference, environment_sha256=sha, size_bytes=len(IMAGE_BYTES))
    store = tmp_path / "store"
    images, roots, downloads = store / "root-images", store / "app-roots", store / "downloads"
    for directory in (images, roots, downloads):
        directory.mkdir(parents=True)
    mounter = FakeImageMounter({sha: tree})

    def incoming(data: bytes = IMAGE_BYTES) -> Path:
        path = downloads / sha
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def stage(image: Path, ref=reference, **abi) -> Path:
        return stage_image(image, roots, ref, images=images, mounter=mounter, **(abi or ABI))

    def staged(ref=reference, **abi) -> Path:
        return mounted_root(roots, ref, images=images, mounter=mounter, **(abi or ABI))

    hashes = []
    image_sha256 = environment._image_sha256

    def counted(descriptor):
        hashes.append(descriptor)
        return image_sha256(descriptor)
    monkeypatch.setattr(environment, "_image_sha256", counted)
    return SimpleNamespace(reference=reference, sha=sha, images=images, roots=roots,
                           pool=images / (sha + ".squashfs"), mounter=mounter, incoming=incoming,
                           stage=stage, staged=staged, hashes=hashes)


def test_an_image_is_sealed_adopted_by_rename_hashed_mounted_and_checked(world):
    fetched = world.incoming()
    inode = fetched.stat().st_ino
    assert world.stage(fetched) == world.roots / world.sha
    # One copy: the pool image is the fetched file itself, sealed read-only.
    info = world.pool.stat()
    assert (info.st_ino, info.st_mode & 0o777, info.st_nlink) == (inode, 0o444, 1)
    assert not fetched.exists()
    assert world.mounter.mount_calls == [(world.pool, world.roots / world.sha)]
    assert len(world.hashes) == 1
    assert world.staged() == world.roots / world.sha


def test_a_flipped_byte_is_refused_and_the_pool_file_unlinked_before_any_mount(world):
    flipped = bytearray(IMAGE_BYTES)
    flipped[7] ^= 1
    with pytest.raises(ValueError, match="^root_image_digest_mismatch$"):
        world.stage(world.incoming(bytes(flipped)))
    assert list(world.images.iterdir()) == [] and world.mounter.mount_calls == []


def test_adoption_across_filesystems_is_refused_never_copied(world, monkeypatch):
    def cross_device(source, target):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(environment.os, "rename", cross_device)
    with pytest.raises(ValueError, match="^root_image_adopt_cross_device$"):
        world.stage(world.incoming())
    assert list(world.images.iterdir()) == [] and world.mounter.mount_calls == []


def test_a_mispaired_image_stays_mounted_refused_until_a_corrected_ref_stages_it(world, monkeypatch):
    wrong = replace(world.reference, deb_version="9.9")
    with pytest.raises(ValueError, match="^environment_reference_mismatch$"):
        world.stage(world.incoming(), wrong)
    assert world.pool.exists() and world.roots / world.sha in world.mounter.mounts
    with pytest.raises(ValueError, match="^environment_reference_mismatch$"):
        world.staged(wrong)
    renamed = []
    rename = environment.os.rename
    monkeypatch.setattr(environment.os, "rename", lambda *a: renamed.append(a) or rename(*a))
    hashed = len(world.hashes)
    # The corrected ref for the same digest: staged by the mount already there, no download.
    assert world.stage(world.roots.parent / "downloads" / world.sha) == world.roots / world.sha
    assert not renamed and len(world.hashes) == hashed and len(world.mounter.mount_calls) == 1


def test_a_release_for_another_abi_is_refused_before_any_adopt_or_mount(world):
    fetched = world.incoming()
    other = replace(world.reference, base_abi="base-v3")
    with pytest.raises(ValueError, match="^environment_abi_mismatch$"):
        world.stage(fetched, other)  # the Node measured ABI's base
    assert fetched.exists() and list(world.images.iterdir()) == []
    assert world.mounter.mount_calls == []
    world.stage(fetched)
    # The staged predicate binds the measured ABI: a Node measuring another base refuses it.
    with pytest.raises(ValueError, match="^environment_abi_mismatch$"):
        world.staged(**{**ABI, "base_abi": "base-v3"})


@pytest.mark.parametrize("left", ["mode", "owner"])
def test_a_pool_file_left_unsealed_is_unlinked_and_the_next_attempt_stages(world, monkeypatch, left):
    world.pool.write_bytes(IMAGE_BYTES)  # a simulated crash before sealing
    world.pool.chmod(0o600 if left == "mode" else 0o444)
    if left == "owner":
        monkeypatch.setattr(environment, "ROOT_UID", os.getuid() + 1)  # owned by someone else
    with pytest.raises(ValueError, match="^root_image_ownership$"):
        world.stage(world.incoming())
    assert not world.pool.exists() and world.mounter.mount_calls == []
    monkeypatch.setattr(environment, "ROOT_UID", os.getuid())
    assert world.stage(world.incoming()) == world.roots / world.sha


@pytest.mark.parametrize("mounted", [True, False], ids=["mounted", "pooled"])
def test_a_wrong_size_ref_for_a_held_digest_is_a_pairing_refusal_never_an_unlink(world, mounted):
    world.stage(world.incoming())
    if not mounted:
        world.mounter.mounts.clear()
    wrong = replace(world.reference, size_bytes=world.reference.size_bytes + 1)
    with pytest.raises(ValueError, match="^environment_reference_mismatch$"):
        world.stage(world.incoming(), wrong)
    # The image (and any live mount over it) stays; the right ref stages it with no download.
    assert world.pool.exists() and (world.roots / world.sha in world.mounter.mounts) == mounted
    assert world.stage(world.roots.parent / "downloads" / world.sha) == world.roots / world.sha


def test_a_pool_file_backing_a_live_mount_is_never_unlinked(world):
    world.stage(world.incoming())
    world.pool.chmod(0o644)  # its own facts now fail, but the loop holds its inode
    with pytest.raises(ValueError, match="^root_image_ownership$"):
        world.stage(world.incoming())
    assert world.pool.exists() and world.roots / world.sha in world.mounter.mounts


def test_the_incoming_file_is_sealed_before_it_gets_a_pool_name(world, monkeypatch):
    order = []
    monkeypatch.setattr(environment, "ROOT_GID", os.getgid() + 1)  # forces the fchown
    monkeypatch.setattr(environment.os, "fchown", lambda *a: order.append("fchown"))
    fchmod, rename = environment.os.fchmod, environment.os.rename
    monkeypatch.setattr(environment.os, "fchmod", lambda *a: order.append("fchmod") or fchmod(*a))
    monkeypatch.setattr(environment.os, "rename", lambda *a: order.append("rename") or rename(*a))
    world.stage(world.incoming())
    assert order == ["fchown", "fchmod", "rename"]


@pytest.mark.parametrize("link", ["symlink", "hard link"])
def test_a_linked_incoming_file_is_refused_and_never_chowned_or_chmoded(world, monkeypatch, tmp_path, link):
    """The import worker holds CAP_CHOWN and CAP_DAC_OVERRIDE and seals in a directory AppManager
    writes, before any hash check: a planted link must never make the seal change another file."""
    victim = tmp_path / "victim"
    victim.write_bytes(IMAGE_BYTES)  # the incoming file's size, so only the link guard refuses
    victim.chmod(0o600)
    incoming = world.roots.parent / "downloads" / world.sha
    if link == "symlink":
        incoming.symlink_to(victim)
    else:
        os.link(victim, incoming)
    monkeypatch.setattr(environment, "ROOT_GID", os.getgid() + 1)  # a seal would fchown
    sealed = []
    monkeypatch.setattr(environment.os, "fchown", lambda *a: sealed.append("fchown"))
    monkeypatch.setattr(environment.os, "fchmod", lambda *a: sealed.append("fchmod"))
    with pytest.raises(ValueError, match="^root_image_ownership$"):
        world.stage(incoming)
    assert sealed == [] and victim.stat().st_mode & 0o777 == 0o600
    assert list(world.images.iterdir()) == [] and world.mounter.mount_calls == []


def test_staging_a_staged_root_again_mounts_nothing_reads_nothing_and_drops_the_duplicate(world):
    world.stage(world.incoming())
    duplicate = world.incoming()
    assert world.stage(duplicate) == world.roots / world.sha
    assert len(world.mounter.mount_calls) == 1 and len(world.hashes) == 1
    assert not duplicate.exists() and world.pool.exists()


def test_a_pooled_unmounted_image_is_rehashed_and_mounted_and_the_incoming_dropped(world):
    world.stage(world.incoming())
    world.mounter.mounts.clear()  # e.g. a killed stager between adoption and mount
    duplicate = world.incoming()
    assert world.stage(duplicate) == world.roots / world.sha
    assert not duplicate.exists() and len(world.hashes) == 2 and len(world.mounter.mount_calls) == 2


def test_no_incoming_file_and_no_pool_image_is_missing(world):
    with pytest.raises(ValueError, match="^root_image_missing$"):
        world.stage(world.roots.parent / "downloads" / world.sha)


@pytest.mark.parametrize("fault", ["absent", "writable", "other image", "pool mode", "no pool"])
def test_a_root_is_staged_only_by_a_read_only_mount_of_its_sealed_pool_image(world, fault):
    world.stage(world.incoming())
    where = world.roots / world.sha
    mount = world.mounter.mounts[where]
    if fault == "absent":
        del world.mounter.mounts[where]  # the directory (an empty mount point) still exists
    elif fault == "writable":
        world.mounter.mounts[where] = replace(mount, read_only=False)
    elif fault == "other image":
        world.mounter.mounts[where] = replace(mount, image=world.images / "other.squashfs")
    elif fault == "pool mode":
        world.pool.chmod(0o644)
    else:
        world.pool.unlink()
    assert where.is_dir()
    with pytest.raises(ValueError, match="^root_image_not_staged$"):
        world.staged()


def test_host_core_cannot_read_the_image_pool():
    unit = (REPO / "appliance/systemd/photo-wall-host-core.service").read_text().splitlines()
    hidden = next(line for line in unit if line.startswith("InaccessiblePaths=")).split("=", 1)[1]
    assert "-/run/photo-wall-node-storage/root-images" in hidden.split()


def test_the_image_format_is_part_of_the_base_identity(tmp_path, monkeypatch):
    def base_abi(root: Path) -> str:
        return json.loads((root / "usr/lib/photo-wall-node-base/abi.json").read_text())["base_abi"]
    base.stage_tree(REPO, tmp_path / "original")
    monkeypatch.setattr(base, "SQUASHFS_OPTIONS", (*base.SQUASHFS_OPTIONS, "-noI"))
    base.stage_tree(REPO, tmp_path / "changed")
    assert base_abi(tmp_path / "changed") != base_abi(tmp_path / "original")


def _image_builder(built: list, *, differ: bool = False, size_bytes: int | None = None):
    def build_image(archive, reference, output, *, tools, **abi):
        output.mkdir(parents=True)
        data = IMAGE_BYTES + (b"!" if differ and built else b"")
        sha = hashlib.sha256(data).hexdigest()
        (output / (sha + ".squashfs")).write_bytes(data)
        built.append(output)
        return EnvironmentImage(sha, size_bytes or len(data), output / (sha + ".squashfs"))
    return build_image


@pytest.mark.parametrize("differ", [False, True])
def test_the_component_build_ships_an_image_only_when_two_builds_agree(tmp_path, world, differ):
    built = []

    def run() -> EnvironmentImage:
        return reproducible_image(tmp_path / "app.tar", world.reference, tmp_path / "work", role="app",
                                  abi=ABI, tools="tools", build_image=_image_builder(built, differ=differ))
    if differ:
        with pytest.raises(ValueError, match="^node_components_image_not_reproducible$"):
            run()
    else:
        assert run().sha256 == world.sha
    assert len(built) == 2 and built[0] != built[1]


@pytest.mark.parametrize("role", ["app", "manager-primary"])
def test_the_component_build_ships_no_image_over_its_line(tmp_path, world, role):
    over = line("app-image" if role == "app" else "manager-image").cap_bytes + 1
    with pytest.raises(ValueError, match="^node_components_image_over_line$"):
        reproducible_image(tmp_path / "app.tar", world.reference, tmp_path / "work", role=role, abi=ABI,
                           tools="tools", build_image=_image_builder([], size_bytes=over))


# SystemdImageMounter over real-format mountinfo and sysfs fixtures.

WHERE = Path("/run/photo-wall-node-storage/app-roots/" + "c" * 64)
IMAGE = Path("/run/photo-wall-node-storage/root-images/" + "c" * 64 + ".squashfs")
OTHER = Path("/run/photo-wall-node-storage/root-images/" + "d" * 64 + ".squashfs")
LINES = ("22 1 0:21 / /run rw,nosuid,nodev shared:5 - tmpfs tmpfs rw,mode=755",
         "30 22 0:30 / /run/photo-wall-node-storage rw,nosuid,nodev shared:9 - tmpfs photo-wall-node rw")


def host(tmp_path, *, options="ro,nosuid,nodev,relatime", loop_ro="1", backing=IMAGE,
         fstype="squashfs", loop=True, where=WHERE):
    """A mountinfo with one mount at `where` (unless options is None) and its device's sysfs."""
    lines = list(LINES)
    if options is not None:
        mount_point = str(where).replace(" ", "\\040")
        lines.append(f"40 30 7:0 / {mount_point} {options} shared:12 - {fstype} /dev/loop0 ro,errors=continue")
    (tmp_path / "mountinfo").write_text("\n".join(lines) + "\n")
    device = tmp_path / "block/7:0"
    device.mkdir(parents=True, exist_ok=True)
    (device / "ro").write_text(loop_ro + "\n")
    if loop:
        (device / "loop").mkdir(exist_ok=True)
        (device / "loop/backing_file").write_text(str(backing) + "\n")
    return SystemdImageMounter(mountinfo=tmp_path / "mountinfo", sys_dev_block=tmp_path / "block")


def no_systemd(*_, **__):
    raise AssertionError("systemd-mount must not run")


def test_no_mount_at_the_point_is_absent(tmp_path):
    assert host(tmp_path, options=None).mounted(WHERE) is None


def test_the_same_image_mounted_read_only_is_returned_without_asking_pid1(tmp_path, monkeypatch):
    mounter = host(tmp_path)
    monkeypatch.setattr(image_mount.subprocess, "run", no_systemd)
    state = mounter.mounted(WHERE)
    assert state is not None and (state.image, state.fstype, state.read_only) == (IMAGE, "squashfs", True)
    assert mounter.mount(IMAGE, WHERE) == state


def test_an_escaped_mount_point_is_read_back(tmp_path):
    where = Path("/run/photo wall/app-roots/x")
    assert host(tmp_path, where=where).mounted(where) is not None


@pytest.mark.parametrize("fixture", [
    {"backing": OTHER},                                      # another image
    {"options": "rw,nosuid,nodev,relatime", "loop_ro": "0"},  # per-mount rw; the superblock says ro
    {"options": "rw,nosuid,nodev,relatime"},                 # per-mount rw over a read-only loop
    {"loop_ro": "0"},                                        # per-mount ro over a writable loop
    {"loop": False},                                         # not a loop: not an image
    {"fstype": "ext4"},
], ids=["other-image", "rw-superblock-ro", "rw-mount", "writable-loop", "not-a-loop", "not-squashfs"])
def test_anything_else_at_the_point_is_a_conflict_never_unmounted(tmp_path, monkeypatch, fixture):
    mounter = host(tmp_path, **fixture)
    monkeypatch.setattr(image_mount.subprocess, "run", no_systemd)
    state = mounter.mounted(WHERE)
    assert state is not None
    if "loop_ro" in fixture or "options" in fixture:
        assert not state.read_only
    if not fixture.get("loop", True):
        assert state.image == NO_IMAGE
    with pytest.raises(ValueError, match="^image_mount_conflict$"):
        mounter.mount(IMAGE, WHERE)


def test_pid1_is_asked_for_a_read_only_loop_mount_and_it_is_read_back(tmp_path, monkeypatch):
    mounter = host(tmp_path, options=None)
    calls = []

    def systemd_mount(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        host(tmp_path)  # PID1's mount, now visible
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(image_mount.subprocess, "run", systemd_mount)
    assert mounter.mount(IMAGE, WHERE).read_only
    [(argv, env)] = calls
    assert argv == ["/usr/bin/systemd-mount", "--no-ask-password", "--collect", "--type=squashfs",
                    "--options=ro,nodev,nosuid,loop", str(IMAGE), str(WHERE)]
    assert IMAGE_MOUNT_OPTIONS[0] == "ro" and env == {"PATH": "/usr/bin", "LANG": "C"}


def test_a_refused_mount_fails_and_an_invisible_one_is_not_visible(tmp_path, monkeypatch):
    mounter = host(tmp_path, options=None)
    monkeypatch.setattr(image_mount.subprocess, "run",
                        lambda argv, **_: subprocess.CompletedProcess(argv, 1, b"", b"no loop"))
    with pytest.raises(ValueError, match="^image_mount_failed$"):
        mounter.mount(IMAGE, WHERE)
    monkeypatch.setattr(image_mount.subprocess, "run",
                        lambda argv, **_: subprocess.CompletedProcess(argv, 0, b"", b""))
    with pytest.raises(ValueError, match="^image_mount_not_visible$"):
        mounter.mount(IMAGE, WHERE)


@pytest.mark.parametrize("fault", [subprocess.TimeoutExpired("systemd-mount", 60),
                                   OSError(errno.ENOENT, "No such file or directory")],
                         ids=["pid1-never-answers", "not-runnable"])
def test_a_mount_request_that_cannot_complete_is_a_mount_failure(tmp_path, monkeypatch, fault):
    mounter = host(tmp_path, options=None)

    def systemd_mount(argv, **_):
        raise fault
    monkeypatch.setattr(image_mount.subprocess, "run", systemd_mount)
    with pytest.raises(ValueError, match="^image_mount_failed$"):
        mounter.mount(IMAGE, WHERE)


# The online path (E2c B2): the import worker stages against the broker's measured ABI, the
# broker's launch check is the staged predicate, AppManager skips a held image, and no Node
# module parses tar.

def _command(target, old=None):
    """A stage command for `target`, its fallback the old root, as the broker hands it over."""
    from contracts.node_lifecycle import StageCommandV2, stage_digest
    from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2
    broker = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    old = old or replace(target, environment_sha256="a" * 64)
    command = StageCommandV2(uuid4(), uuid4(), "0" * 64, broker, uuid4(), uuid4(),
                             NodeProcessIdentity(100, 200, uuid4()), 1, old, target, old)
    return replace(command, command_sha256=stage_digest(command))


def _staged_old(world):
    """The running (old) root, staged from its own image: the online command's fallback."""
    data = b"the old root's image " * 64
    old = replace(world.reference, environment_sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    world.mounter.trees[old.environment_sha256] = world.mounter.trees[world.sha]
    incoming = world.images.parent / "downloads" / old.environment_sha256
    incoming.write_bytes(data)
    world.stage(incoming, old)
    return old


def test_the_online_stage_adopts_the_download_root_owned_and_read_only(world, monkeypatch):
    """B2 AC3: the import worker's staging seals the download root:root 0444 in the pool."""
    from appliance.apps import root_import
    store = world.roots.parent
    owners = []
    monkeypatch.setattr(environment, "ROOT_GID", os.getgid() + 1)  # forces the fchown
    monkeypatch.setattr(environment.os, "fchown", lambda fd, uid, gid: owners.append((uid, gid)))
    download = store / "preparation/downloads" / world.sha
    download.parent.mkdir(parents=True)
    download.write_bytes(IMAGE_BYTES)
    download.chmod(0o600)
    command = _command(world.reference, _staged_old(world))
    root_import.stage_roots(command, ABI, mounter=world.mounter, store=store, images=world.images)
    assert owners[-1] == (environment.ROOT_UID, environment.ROOT_GID)
    info = world.pool.stat()
    assert info.st_mode & 0o777 == 0o444 and info.st_uid == environment.ROOT_UID
    assert not download.exists() and world.staged() == world.roots / world.sha


def test_the_import_worker_stages_against_the_requests_abi_never_the_references(world, monkeypatch):
    """B2 AC5: the measured ABI comes from the request; a request without it is refused."""
    from appliance.apps import root_import
    from contracts.node_lifecycle import encode_stage_command
    command = _command(world.reference)
    with pytest.raises(ValueError, match="^environment_abi_mismatch$"):
        root_import.stage_roots(command, {**ABI, "base_abi": "base-v3"}, mounter=world.mounter,
                                store=world.roots.parent, images=world.images)
    assert world.mounter.mount_calls == [] and list(world.images.iterdir()) == []
    raw = encode_stage_command(command).decode()
    assert root_import.parse_request({"command": raw, **ABI})[1] == ABI
    for request in ({"command": raw}, {"command": raw, **ABI, "extra": "x"},
                    {"command": raw, **ABI, "base_abi": "../escape"}):
        with pytest.raises(ValueError):
            root_import.parse_request(request)


def test_the_broker_hands_the_import_worker_its_measured_abi(monkeypatch):
    from appliance.apps import import_worker
    written = {}
    store = SimpleNamespace(read=lambda name: None, write=lambda name, value: written.setdefault(name, value))
    worker = import_worker.RootImportWorker(store, **ABI)
    monkeypatch.setattr(worker, "ready", lambda command: False)
    monkeypatch.setattr(import_worker.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(import_worker, "systemctl_show", lambda unit: {"ActiveState": "inactive", "MainPID": "0"})
    from test_node_boot import environment as reference
    command = _command(reference("b"))
    worker.advance(command)
    assert {key: written["import-request"][key] for key in ABI} == ABI


def test_the_import_worker_has_one_bind_over_the_store(monkeypatch):
    """B2 AC1's unit half: a separate preparation bind would make adoption EXDEV (CUT-5)."""
    from appliance.apps import import_worker
    source = Path(import_worker.__file__).read_text()
    assert '"ReadWritePaths=/run/photo-wall-node-storage /run/photo-wall-root-import"' in source
    assert "ReadOnlyPaths=" not in source
    assert '"CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_CHOWN"' in source


@pytest.fixture
def driver(world):
    from appliance.apps.process_linux import SystemdAppProcessDriver
    return SystemdAppProcessDriver(world.roots, None, **ABI, images=world.images, mounter=world.mounter)


def test_the_launch_check_accepts_a_staged_root_without_walking_its_tree(world, driver, monkeypatch):
    """B2 AC2: C3 as amended; the release check only (no inventory)."""
    world.stage(world.incoming())
    monkeypatch.setattr(environment, "inventory", lambda *a: pytest.fail("the launch walked the tree"))
    assert driver.verify(world.reference) is True


@pytest.mark.parametrize("fault", ["plain directory", "writable loop", "other abi"])
def test_the_launch_check_refuses_a_root_that_is_not_staged_for_this_node(world, driver, fault):
    """B2 AC2: a plain directory `verify_root` would pass, a writable loop, a measured ABI that
    differs from the ref's."""
    if fault == "plain directory":
        tree = world.mounter.trees[world.sha]
        import shutil
        shutil.copytree(tree, world.roots / world.sha, symlinks=True)
        environment.verify_root(world.roots / world.sha, world.reference, **ABI, owner_uid=os.getuid())
        with pytest.raises(ValueError, match="^root_image_not_staged$"):
            driver.verify(world.reference)
        return
    world.stage(world.incoming())
    if fault == "writable loop":
        where = world.roots / world.sha
        world.mounter.mounts[where] = replace(world.mounter.mounts[where], read_only=False)
        with pytest.raises(ValueError, match="^root_image_not_staged$"):
            driver.verify(world.reference)
    else:
        driver.abi = {**ABI, "base_abi": "base-v3"}
        with pytest.raises(ValueError, match="^environment_abi_mismatch$"):
            driver.verify(world.reference)


def test_no_node_module_parses_tar():
    """B2 AC4: the Node stages images; tar extraction is build-side (scripts/sealed_archive.py)."""
    import ast
    found = []
    for package in ("appliance/apps", "appliance/boot"):
        for path in sorted((REPO / package).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) else [])
                found.extend(str(path.relative_to(REPO)) for name in names if name and name.split(".")[0] == "tarfile")
    assert found == []


def test_a_pooled_image_stages_with_no_incoming_file(world):
    """B2 AC6, Base half: AppManager downloaded nothing; the held, mounted image is staged."""
    world.stage(world.incoming())
    assert world.stage(world.roots.parent / "downloads" / world.sha) == world.roots / world.sha
    assert len(world.mounter.mount_calls) == 1 and len(world.hashes) == 1


def _app_manager(world, tmp_path, monkeypatch, command):
    """AppManager's preparation poll over `command`, with a recording download stand-in."""
    from appliance.node import manager_desired
    from appliance.node.manager_desired import DesiredPreparation
    from contracts.node_lifecycle import encode_stage_command
    monkeypatch.setattr(manager_desired, "ROOT_IMAGES", world.images)
    fetched = []
    monkeypatch.setattr(manager_desired, "DownloadPreparer",
                        lambda *a, **k: SimpleNamespace(prepare=lambda reference: fetched.append(reference)))
    desired = json.dumps({"scope": "preparation_read_only",
                          "commands": [json.loads(encode_stage_command(command))]}).encode()
    grant = SimpleNamespace(producer=command.producer, offer_id=command.offer_id)
    rows = {}
    preparation = DesiredPreparation.__new__(DesiredPreparation)
    preparation.directory = tmp_path / "preparation"
    (preparation.directory / "downloads").mkdir(parents=True)
    preparation.config = {"central": "http://central.test", **ABI}
    preparation.store = SimpleNamespace(read=rows.get, write=rows.__setitem__)
    preparation.session = SimpleNamespace(ensure=lambda: grant, claim=None,
                                          request=lambda method, path, body=None: (200, desired))
    preparation.observation = SimpleNamespace(flush=lambda: None, sample=lambda *a, **k: None)
    return SimpleNamespace(poll=preparation.poll, fetched=fetched, rows=rows,
                           downloads=preparation.directory / "downloads", module=manager_desired)


def test_app_manager_downloads_nothing_for_a_pooled_target(world, tmp_path, monkeypatch):
    """B2 AC6 (Q1 = R): a stage command whose target's image the pool holds downloads nothing."""
    world.stage(world.incoming())
    manager = _app_manager(world, tmp_path, monkeypatch, _command(world.reference))
    manager.poll()
    assert manager.fetched == [] and manager.rows["prepared"]["archives"] == []
    monkeypatch.setattr(manager.module, "ROOT_IMAGES", tmp_path / "empty-pool")
    manager.rows.clear()
    manager.poll()
    assert manager.fetched == [world.reference]


def test_app_manager_keeps_the_current_downloads_and_drops_only_stale_complete_ones(world, tmp_path, monkeypatch):
    """After a prepare, a complete download (a bare digest) the operation does not need is
    unlinked; the operation's own download and the preparer's `.partial` files stay."""
    manager = _app_manager(world, tmp_path, monkeypatch, _command(world.reference))
    stale, partial = "e" * 64, "f" * 64 + ".partial"
    for name in (world.sha, stale, partial):
        (manager.downloads / name).write_bytes(b"x")
    manager.poll()
    assert manager.rows["prepared"]["archives"] == [world.sha]
    assert sorted(p.name for p in manager.downloads.iterdir()) == sorted([world.sha, partial])


def test_app_manager_sees_the_image_pool_read_only():
    from appliance.node import manager_launcher
    source = Path(manager_launcher.__file__).read_text()
    assert '"BindReadOnlyPaths=/run/photo-wall-node-storage/root-images:/run/photo-wall-root-images"' in source
