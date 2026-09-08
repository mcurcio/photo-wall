"""Linux, regular-file-only appliance assembly. Private signing keys stay outside."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import lzma
import os
import re
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from contracts.release import MAX_ROOTFS_BYTES, Release, configuration_digest

BASE_SHA256 = "790652faeb4f61ce7bb12f5cb61734595c61d3cd882915b8b5f9918106c80d37"
BASE_BYTES = 1_257_196_128
MAX_RAW_BYTES = 12 * 1024**3
MIB = 1024**2
SECTOR = 512
RUNTIME_PACKAGES = (
    "python3.12", "python3.12-venv", "python3-gi", "python3-gst-1.0",
    "python3-opengl", "gir1.2-gtk-3.0", "gir1.2-gst-plugins-base-1.0",
    "gstreamer1.0-plugins-base", "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad", "gstreamer1.0-libav", "libgl1-mesa-dri",
    "libegl1", "weston", "chrony", "ca-certificates", "openssl",
    "initramfs-tools", "busybox-initramfs", "iproute2", "kmod",
)
SNAPSHOT = "20260905T000000Z"


class BuildError(ValueError):
    """Sanitized build failure; detailed public tooling logs stay in the bundle."""


def outside_git(path: Path) -> None:
    resolved = path.absolute().resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise BuildError("artifact_inside_git")


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n").encode()


def checked_file(path: Path, maximum: int, *, expected: str | None = None) -> dict:
    """Hash one stable regular file without following its leaf symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise BuildError("artifact_limit")
        total, digest = 0, hashlib.sha256()
        while block := os.read(fd, MIB):
            total += len(block)
            if total > maximum:
                raise BuildError("artifact_limit")
            digest.update(block)
        after = os.fstat(fd)
        if (total != before.st_size or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns):
            raise BuildError("artifact_changed")
        actual = digest.hexdigest()
        if expected is not None and actual != expected:
            raise BuildError("artifact_hash")
        return {"sha256": actual, "size": total}
    finally:
        os.close(fd)


def inventory(root: Path, *, maximum_files: int = 100_000,
              maximum_bytes: int = 16 * 1024**3) -> dict[str, dict]:
    """Record relative regular files/symlinks without traversing symlink dirs."""
    if root.is_symlink() or not root.is_dir():
        raise BuildError("unsafe_tree")
    result, total = {}, 0
    for directory, names, files in os.walk(root, followlinks=False):
        for name in sorted(names + files):
            path = Path(directory) / name
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                result[relative] = {"symlink": os.readlink(path)}
            elif stat.S_ISREG(info.st_mode):
                if info.st_size == 0:
                    entry = {"sha256": hashlib.sha256(b"").hexdigest(), "size": 0}
                else:
                    entry = checked_file(path, maximum_bytes)
                total += entry["size"]
                result[relative] = entry
            elif not stat.S_ISDIR(info.st_mode):
                raise BuildError("unsafe_tree")
            if total > maximum_bytes or len(result) > maximum_files:
                raise BuildError("tree_limit")
    return dict(sorted(result.items()))


def run(argv: list[str], *, timeout: int = 600, log: Path | None = None,
        env: dict | None = None) -> bytes:
    """Run a tool with a finite deadline and bounded persisted output."""
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True, env=env)
    output, deadline = bytearray(), time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(child.stdout, selectors.EVENT_READ)
            while poll.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise BuildError("tool_timeout")
                for key, _ in poll.select(min(0.2, left)):
                    block = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not block:
                        poll.unregister(key.fileobj)
                    else:
                        output.extend(block)
                        if len(output) > 4 * MIB:
                            raise BuildError("tool_output_limit")
            try:
                code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise BuildError("tool_timeout") from None
            if code:
                raise BuildError("tool_failed")
            return bytes(output)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        child.stdout.close()
        if log is not None:
            log.write_bytes(output[:4 * MIB])


@dataclass(frozen=True)
class Partition:
    kind: int
    start: int
    sectors: int

    def __post_init__(self):
        if (type(self.kind) is not int or self.kind not in (0x0C, 0x83)
                or type(self.start) is not int or self.start < 2048
                or type(self.sectors) is not int or self.sectors <= 0
                or self.start + self.sectors > 2**32 - 1):
            raise BuildError("partition_invalid")


def mbr(partitions: tuple[Partition, ...], disk_id: bytes) -> bytes:
    if not 1 <= len(partitions) <= 4 or len(disk_id) != 4:
        raise BuildError("partition_invalid")
    last, result = 2048, bytearray(SECTOR)
    result[440:444] = disk_id
    for index, partition in enumerate(partitions):
        if partition.start < last:
            raise BuildError("partition_overlap")
        last = partition.start + partition.sectors
        result[446 + index * 16:462 + index * 16] = struct.pack(
            "<B3sB3sII", 0x80 if index == 0 else 0, b"\xfe\xff\xff",
            partition.kind, b"\xfe\xff\xff", partition.start, partition.sectors)
    result[510:512] = b"\x55\xaa"
    return bytes(result)


def read_mbr(image: Path) -> tuple[Partition, ...]:
    fd = os.open(image, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        data = os.read(fd, SECTOR)
        if not stat.S_ISREG(info.st_mode) or len(data) != SECTOR or data[510:] != b"\x55\xaa":
            raise BuildError("partition_invalid")
        values = []
        for index in range(4):
            entry = data[446 + index * 16:462 + index * 16]
            _, _, kind, _, start, sectors = struct.unpack("<B3sB3sII", entry)
            if kind:
                values.append(Partition(kind, start, sectors))
            elif any(entry):
                raise BuildError("partition_invalid")
        mbr(tuple(values), data[440:444])
        if (values[-1].start + values[-1].sectors) * SECTOR > info.st_size:
            raise BuildError("partition_truncated")
        return tuple(values)
    finally:
        os.close(fd)


def decompress_base(source: Path, destination: Path) -> None:
    outside_git(destination)
    record = checked_file(source, BASE_BYTES, expected=BASE_SHA256)
    if record["size"] != BASE_BYTES:
        raise BuildError("base_size")
    total, deadline = 0, time.monotonic() + 600
    zeros = bytes(MIB)
    created = False
    try:
        with lzma.open(source, "rb") as incoming, destination.open("xb") as outgoing:
            created = True
            while block := incoming.read(MIB):
                total += len(block)
                if total > MAX_RAW_BYTES or time.monotonic() > deadline:
                    raise BuildError("base_limit")
                # Sparse holes preserve exact logical bytes while avoiding
                # allocating the upstream image's unused zero-filled capacity.
                if block == zeros[:len(block)]:
                    outgoing.seek(len(block), os.SEEK_CUR)
                else:
                    outgoing.write(block)
            outgoing.truncate(total)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if not total:
            raise BuildError("base_empty")
        partitions = read_mbr(destination)
        if len(partitions) != 2 or tuple(p.kind for p in partitions) != (0x0C, 0x83):
            raise BuildError("base_layout")
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def create_disk(boot_tree: Path, destination: Path, *,
                fat_mib: int = 1536, source_epoch: int) -> dict:
    """Generate only a new regular image; never accept an existing device/path."""
    outside_git(destination)
    if (type(source_epoch) is not int or not 315532800 <= source_epoch <= 2**31 - 1
            or type(fat_mib) is not int or not 64 <= fat_mib <= 2048):
        raise BuildError("disk_limits")
    tree = inventory(boot_tree, maximum_files=10_000, maximum_bytes=fat_mib * MIB)
    if not tree or any("symlink" in value for value in tree.values()):
        raise BuildError("boot_tree_invalid")
    if sum(value["size"] for value in tree.values()) > fat_mib * MIB * 0.8:
        raise BuildError("boot_partition_full")
    if destination.exists() or destination.is_symlink():
        raise BuildError("output_exists")
    if os.geteuid() != 0:
        raise BuildError("disk_owner_required")
    boot = Partition(0x0C, 2048, fat_mib * MIB // SECTOR)
    disk_id = hashlib.sha256(canonical(tree)).digest()[:4]
    environment = dict(os.environ, SOURCE_DATE_EPOCH=str(source_epoch),
                       E2FSPROGS_FAKE_TIME=str(source_epoch), TZ="UTC")
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix=".disk-", dir=destination.parent) as scratch:
            temp = Path(scratch)
            fat = temp / "boot.fat"
            with fat.open("xb") as stream:
                stream.truncate(boot.sectors * SECTOR)
            run(["mkfs.vfat", "--invariant", "-F", "32", "-n", "PWBOOT", str(fat)], env=environment)
            # mcopy recursion includes boot subdirectories, but shell expansion is never used.
            for path in sorted(boot_tree.iterdir()):
                run(["mcopy", "-s", "-p", "-m", "-i", str(fat), str(path), "::/"], env=environment)
            with destination.open("xb") as target:
                created = True
                target.truncate((boot.start + boot.sectors) * SECTOR)
                target.write(mbr((boot,), disk_id))
                for partition, file in ((boot, fat),):
                    target.seek(partition.start * SECTOR)
                    with file.open("rb") as source:
                        shutil.copyfileobj(source, target, MIB)
                target.flush()
                os.fsync(target.fileno())
            if read_mbr(destination) != (boot,):
                raise BuildError("disk_verify")
            # Reopen the completed disk through a read-only file-backed appliance.
            # A child process bounds QEMU and all descendants by one deadline.
            run([sys.executable, "-m", "appliance.build", "verify-disk", str(destination), str(boot_tree)],
                timeout=600)
            return dict(checked_file(destination, 20 * 1024**3),
                        partitions=[vars(boot)])
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def _guest_inventory(guest, directory: str) -> dict:
    actual, total = {}, 0
    entries = guest.find(directory)
    if len(entries) > 20_000:
        raise BuildError("boot_tree_limit")
    for relative in entries:
        relative = relative.lstrip("/")
        path = directory.rstrip("/") + "/" + relative
        if guest.is_file(path, followsymlinks=False):
            size = guest.filesize(path)
            total += size
            if len(actual) >= 10_000 or total > 2 * 1024**3:
                raise BuildError("boot_tree_limit")
            actual[relative] = dict(size=size, sha256=guest.checksum("sha256", path))
        elif not guest.is_dir(path, followsymlinks=False):
            raise BuildError("boot_tree_invalid")
    return actual


def execution_inventory() -> dict:
    modules = {"appliance/build.py": Path(__file__)}
    for name in ("appliance", "appliance.bootstrap", "appliance.updates", "contracts",
                 "contracts.release", "scripts.ci_apt_cache"):
        module = importlib.import_module(name)
        relative = name.replace(".", "/") + ("/__init__.py" if hasattr(module, "__path__") else ".py")
        modules[relative] = Path(module.__file__)
    return {name: checked_file(path, 4 * MIB) for name, path in modules.items()}


def verify_executing_source(record: dict) -> None:
    if (not isinstance(record, dict) or not isinstance(record.get("files"), dict)
            or any(record["files"].get(name) != value for name, value in execution_inventory().items())):
        raise BuildError("executing_source_mismatch")


def verify_release_boot(rootfs: Path, boot_tree: Path, revision: str) -> None:
    """Bind staged boot bytes to the exact authenticated rootfs, including initrd."""
    import guestfs

    expected = inventory(boot_tree, maximum_files=10_000, maximum_bytes=2 * 1024**3)
    guest = guestfs.GuestFS(python_return_dict=True)
    try:
        guest.set_memsize(512)
        guest.add_drive_opts(str(rootfs), readonly=True, format="raw")
        guest.launch()
        filesystems = guest.list_filesystems()
        if filesystems != {"/dev/sda": "squashfs"}:
            raise BuildError("rootfs_filesystem")
        guest.mount_ro("/dev/sda", "/")
        if _guest_inventory(guest, "/boot/firmware") != expected:
            raise BuildError("authenticated_boot_mismatch")
        source = "/usr/share/photo-wall/build/source.json"
        if not guest.is_file(source, followsymlinks=False) or not 0 < guest.filesize(source) <= 4 * MIB:
            raise BuildError("authenticated_source_missing")
        record = json.loads(guest.read_file(source))
        if record.get("revision") != revision:
            raise BuildError("authenticated_revision_mismatch")
        verify_executing_source(record)
        guest.shutdown()
    finally:
        guest.close()


def verify_disk(image: Path, boot_tree: Path) -> None:
    """Reopen the exact read-only boot partition; there is no local state volume."""
    import guestfs

    read_mbr(image)
    expected = inventory(boot_tree, maximum_files=10_000, maximum_bytes=2 * 1024**3)
    guest = guestfs.GuestFS(python_return_dict=True)
    try:
        guest.set_memsize(512)
        guest.add_drive_opts(str(image), readonly=True, format="raw")
        guest.launch()
        filesystems = guest.list_filesystems()
        boots = [device for device, kind in filesystems.items() if kind == "vfat"]
        if len(boots) != 1 or len(filesystems) != 1:
            raise BuildError("disk_verify")
        guest.mount_ro(boots[0], "/")
        if _guest_inventory(guest, "/") != expected:
            raise BuildError("disk_boot_mismatch")
        guest.umount_all()
        guest.shutdown()
    finally:
        guest.close()


def squash(root: Path, destination: Path, source_epoch: int) -> dict:
    outside_git(destination)
    if destination.exists() or destination.is_symlink():
        raise BuildError("output_exists")
    try:
        run(["mksquashfs", str(root), str(destination), "-noappend", "-comp", "xz",
             "-b", "1M", "-processors", "2", "-no-progress", "-mkfs-time", str(source_epoch),
             "-all-time", str(source_epoch)], timeout=900)
        result = checked_file(destination, MAX_ROOTFS_BYTES)
        run(["unsquashfs", "-s", str(destination)])
        return result
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def extract_base(image: Path, destination: Path) -> None:
    """Export a verified raw Ubuntu image with filesystem metadata preserved.

    Invoke this operation in its own deadline-controlled process: libguestfs uses
    a file-backed QEMU appliance, whose descendants belong to that process group.
    """
    import guestfs

    outside_git(destination)
    if destination.exists() or destination.is_symlink():
        raise BuildError("output_exists")
    checked_file(image, MAX_RAW_BYTES)
    read_mbr(image)
    destination.mkdir(mode=0o700)
    archive = destination.parent / (destination.name + ".tar")
    if archive.exists() or archive.is_symlink():
        raise BuildError("output_exists")
    guest = guestfs.GuestFS(python_return_dict=True)
    try:
        guest.add_drive_opts(str(image), readonly=True, format="raw")
        guest.launch()
        filesystems = guest.list_filesystems()
        roots = [device for device, kind in filesystems.items() if kind == "ext4"]
        boots = [device for device, kind in filesystems.items() if kind == "vfat"]
        if len(roots) != 1 or len(boots) != 1:
            raise BuildError("base_layout")
        guest.mount_ro(roots[0], "/")
        guest.tar_out("/", str(archive), numericowner=True, xattrs=True, acls=True)
        checked_file(archive, MAX_RAW_BYTES)
        run(["tar", "--numeric-owner", "--xattrs", "--xattrs-include=*", "--acls",
             "-xpf", str(archive), "-C", str(destination)], timeout=600)
        archive.unlink()
        guest.umount_all()
        guest.mount_ro(boots[0], "/")
        guest.tar_out("/", str(archive), numericowner=True)
        checked_file(archive, 1024 * MIB)
        boot = destination / "boot/firmware"
        boot.mkdir(mode=0o755)
        run(["tar", "--numeric-owner", "-xpf", str(archive), "-C", str(boot)])
        guest.shutdown()
    finally:
        guest.close()
        archive.unlink(missing_ok=True)


def _root(root: Path) -> Path:
    root = root.absolute()
    if (root == Path("/") or root.is_symlink() or not root.is_dir()
            or not (root / "usr/bin/python3.12").is_file()
            or 'VERSION_ID="24.04"' not in (root / "etc/os-release").read_text()):
        raise BuildError("root_invalid")
    return root


def in_root(root: Path, *argv: str, timeout: int = 900, log: Path | None = None) -> bytes:
    return run(["chroot", str(_root(root)), *argv], timeout=timeout, log=log,
               env=dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C.UTF-8"))


def install_runtime_packages(root: Path, evidence: Path, archive_cache=None) -> dict:
    """Resolve and retain the snapshot package closure inside the owned root."""
    root = _root(root)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    if Path("/tool-packages.tsv").is_file():
        shutil.copyfile("/tool-packages.tsv", evidence / "tool-packages.tsv")
    tool_inventory = {}
    for name in ("python3.12", "guestfish", "mksquashfs", "mkfs.ext4", "mkfs.vfat", "tar", "openssl"):
        executable = shutil.which(name)
        if not executable:
            raise BuildError("tool_missing")
        tool_inventory[name] = checked_file(Path(executable).resolve(), 256 * MIB)
    (evidence / "tool-binaries.json").write_bytes(canonical(tool_inventory))
    for name, major, minor in (("null", 1, 3), ("zero", 1, 5), ("random", 1, 8), ("urandom", 1, 9)):
        path = root / "dev" / name
        if not path.exists():
            os.mknod(path, stat.S_IFCHR | 0o666, os.makedev(major, minor))
        path.chmod(0o666)
    resolver = root / "etc/resolv.conf"
    resolver.unlink(missing_ok=True)
    shutil.copyfile("/etc/resolv.conf", resolver)
    policy = root / "usr/sbin/policy-rc.d"
    policy.write_text("#!/bin/sh\nexit 101\n")
    policy.chmod(0o755)
    apt = root / "etc/apt"
    for path in (apt / "sources.list.d").glob("*"):
        if path.is_file() or path.is_symlink():
            path.unlink()
    apt_config = apt / "apt.conf.d/99-photo-wall-transport"
    apt_config.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    apt_config.write_text(
        'Acquire::Retries "2";\n'
        'Acquire::http::Timeout "30";\n'
        'Acquire::https::Timeout "30";\n')
    apt_config.chmod(0o644)
    (apt / "sources.list").write_text("".join(
        f"deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg target=Packages] "
        f"https://snapshot.ubuntu.com/ubuntu/{SNAPSHOT} {suite} main universe restricted multiverse\n"
        for suite in ("noble", "noble-updates", "noble-security")))
    if not (evidence / "base-packages.tsv").exists():
        (evidence / "base-packages.tsv").write_bytes(in_root(
            root, "dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\n"))
    installed = (evidence / "base-packages.tsv").read_text().splitlines()
    names = {line.split("\t")[0] for line in installed}
    held = sorted(name for name in names if name.startswith(("linux-image", "linux-modules", "linux-firmware")))
    if not held:
        raise BuildError("kernel_missing")
    in_root(root, "apt-mark", "hold", *held)
    in_root(root, "apt-get", "-o", "APT::Update::Error-Mode=any", "update",
            log=evidence / "apt-update.log")
    remove = sorted(names & {"cloud-init", "snapd", "openssh-server", "unattended-upgrades",
                             "landscape-common", "ubuntu-server", "ubuntu-server-raspi"})
    if remove:
        in_root(root, "apt-get", "purge", "-y", *remove, log=evidence / "apt-purge.log")
    # Never let the extracted base or an untrusted accelerator populate APT's
    # final archive directory without matching the fresh authenticated plan.
    in_root(root, "apt-get", "clean")
    plan_dir = root / "tmp/photo-wall-apt-plan"
    if plan_dir.exists() or plan_dir.is_symlink():
        raise BuildError("apt_plan_exists")
    (plan_dir / "partial").mkdir(mode=0o700, parents=True)
    try:
        plan = in_root(
            root, "apt-get", "-o", "Dir::Cache::archives=/tmp/photo-wall-apt-plan",
            "-o", "Acquire::ForceHash=SHA256", "--print-uris", "--download-only",
            "--quiet=2", "install", "-y", "--no-install-recommends", *RUNTIME_PACKAGES,
        )
    finally:
        shutil.rmtree(plan_dir, ignore_errors=True)
    cache_record = {"requested": False}
    archives = root / "var/cache/apt/archives"
    cache_plan = None
    if archive_cache is not None:
        cache_plan = archive_cache.plan(plan)
        (evidence / "apt-download-plan.txt").write_bytes(plan)
        cache_record = {**archive_cache.restore(archives, cache_plan), "publish": None}
    try:
        in_root(root, "apt-get", "--download-only", "install", "-y", "--no-install-recommends",
                *RUNTIME_PACKAGES, log=evidence / "apt-download.log")
    finally:
        if archive_cache is not None:
            try:
                cache_record["publish"] = archive_cache.publish(archives, cache_plan)
            except Exception:
                # A cache accelerator cannot replace or mask APT's outcome.
                cache_record["publish"] = {
                    "requested": True, "published": False, "objects": 0,
                    "complete": False, "reason": "cache_publish_failed",
                }
    debs = evidence / "debs"
    debs.mkdir(mode=0o700, exist_ok=True)
    for package in sorted((root / "var/cache/apt/archives").glob("*.deb")):
        shutil.copy2(package, debs / package.name)
    (evidence / "deb-hashes.json").write_bytes(canonical(inventory(debs)))
    in_root(root, "apt-get", "install", "-y", "--no-install-recommends", *RUNTIME_PACKAGES,
            log=evidence / "apt-install.log")
    (evidence / "packages.tsv").write_bytes(in_root(
        root, "dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\n"))
    (evidence / "package-state.txt").write_bytes(in_root(root, "dpkg", "--audit"))
    if (evidence / "package-state.txt").stat().st_size:
        raise BuildError("package_incomplete")
    shutil.copytree(root / "var/lib/apt/lists", evidence / "apt-lists", dirs_exist_ok=True)
    return cache_record


def configure_root(root: Path, source: Path, wheelhouse: Path, public: Path,
                   evidence: Path) -> str:
    """Install public common files and the offline Player-only dependency closure."""
    from appliance.bootstrap import BootConfig, read_regular

    root = _root(root)
    if {path.name for path in public.iterdir()} != {"public.json", "bootstrap.json", "ca.pem", "release.pub.pem"}:
        raise BuildError("public_inputs_invalid")
    inputs = {name: read_regular(public / name, MIB) for name in (
        "public.json", "bootstrap.json", "ca.pem", "release.pub.pem")}
    config_hash = configuration_digest(inputs)
    bootstrap = json.loads(inputs["bootstrap.json"])
    # Reuse the runtime origin/time syntax validation before generating any script.
    BootConfig(bootstrap["release_origin"], bootstrap["time_server"], "0" * 64, config_hash, public)
    config_dir = root / "etc/photo-wall"
    config_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name, data in inputs.items():
        (config_dir / name).write_bytes(data)
        (config_dir / name).chmod(0o644)
    wheel_inventory = inventory(wheelhouse, maximum_files=256, maximum_bytes=1024**3)
    if ("requirements.txt" not in wheel_inventory or any("symlink" in value for value in wheel_inventory.values())
            or any(not ((name.startswith("wheels/") and name.endswith(".whl"))
                        or name in ("requirements.txt", "source.tar", "inventory.json"))
                   for name in wheel_inventory)):
        raise BuildError("wheelhouse_invalid")
    wheel_stage = root / "tmp/photo-wall-wheels"
    wheel_stage.mkdir(mode=0o700)
    shutil.copyfile(wheelhouse / "requirements.txt", wheel_stage / "requirements.txt")
    shutil.copytree(wheelhouse / "wheels", wheel_stage / "wheels")
    in_root(root, "python3.12", "-m", "venv", "--system-site-packages", "/opt/photo-wall/venv")
    in_root(root, "/opt/photo-wall/venv/bin/python", "-m", "pip", "install", "--no-index",
            "--require-hashes", "--find-links", "/tmp/photo-wall-wheels/wheels", "-r",
            "/tmp/photo-wall-wheels/requirements.txt", log=evidence / "pip-install.log")
    shutil.rmtree(wheel_stage)
    site = root / "opt/photo-wall/venv/lib/python3.12/site-packages"
    if any((site / name).exists() for name in ("central", "media")):
        raise BuildError("player_package_boundary")
    (evidence / "python-packages.json").write_bytes(in_root(
        root, "/opt/photo-wall/venv/bin/python", "-m", "pip", "list", "--format=json"))
    for package, modules in (("appliance", ("__init__", "bootstrap", "updates")),
                             ("contracts", ("__init__", "release"))):
        # The Player's complete wheel must precede these minimal distro modules.
        # The hook separately installs their initramfs-only stdlib copies.
        target = root / "usr/lib/python3/dist-packages" / package
        target.mkdir(mode=0o755, parents=True, exist_ok=True)
        for module in modules:
            shutil.copyfile(source / package / (module + ".py"), target / (module + ".py"))
    in_root(root, "/opt/photo-wall/venv/bin/python", "-c",
            "import player.service,gi,OpenGL;from pathlib import Path;"
            "player.service.load_config(Path('/etc/photo-wall/public.json'));"
            "gi.require_version('Gtk','3.0');"
            "gi.require_version('Gst','1.0');from gi.repository import Gtk,Gst;"
            "Gst.init(None);print('GTK',Gtk.get_major_version(),Gtk.get_minor_version());"
            "print('GStreamer',Gst.version_string());"
            "assert all(Gst.ElementFactory.find(n) for n in "
            "('h264parse','avdec_h264','jpegdec','videoconvert','videoscale','appsink'))",
            log=evidence / "native-import.log")
    in_root(root, "python3.12", "-I", "-c",
            "import appliance.bootstrap,appliance.updates,contracts.release;"
            "assert 'dist-packages' in appliance.bootstrap.__file__",
            log=evidence / "bootstrap-import.log")
    for directory in ("hooks", "scripts"):
        for path in (source / "appliance/initramfs" / directory).iterdir():
            target = root / "etc/initramfs-tools" / directory / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o755)
    for name in ("weston.service", "player.service", "accept-trial.service", "trial-recovery.service"):
        target = root / "etc/systemd/system" / ("photo-wall-" + name)
        shutil.copyfile(source / "appliance/systemd" / name, target)
        if name == "trial-recovery.service":
            # This unit is activated only by accept-trial failure, never at boot.
            continue
        wants = root / "etc/systemd/system/multi-user.target.wants" / target.name
        wants.parent.mkdir(parents=True, exist_ok=True)
        wants.unlink(missing_ok=True)
        wants.symlink_to("/etc/systemd/system/" + target.name)
    manager = root / "etc/systemd/system.conf.d/20-photo-wall-watchdog.conf"
    manager.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "appliance/systemd/watchdog.conf", manager)
    manager.chmod(0o644)
    weston = root / "etc/xdg/weston"
    weston.mkdir(parents=True, exist_ok=True)
    # Generate compositor routing through the same canonical Player helper used
    # for GTK windows. This keeps HDMI and headless Virtual connector names in
    # one bounded configuration list.
    ini = in_root(root, "/opt/photo-wall/venv/bin/python", "-c",
                  "from player.output_discovery import weston_ini;"
                  "print(weston_ini(), end='')")
    (weston / "weston.ini").write_bytes(ini)
    passwd = (root / "etc/passwd").read_text()
    if not any(line.startswith("wall:") for line in passwd.splitlines()):
        in_root(root, "useradd", "--uid", "10001", "--user-group", "--no-create-home",
                "--shell", "/usr/sbin/nologin", "wall")
    for group in ("video", "render", "input"):
        if any(line.startswith(group + ":") for line in (root / "etc/group").read_text().splitlines()):
            in_root(root, "usermod", "-a", "-G", group, "wall")
    chrony = root / "etc/chrony/chrony.conf"
    chrony.write_text("server " + bootstrap["time_server"] + " iburst\n"
                      "makestep 1.0 3\nrtcsync\nlogdir /run/chrony\n")
    netplan = root / "etc/netplan"
    for path in netplan.glob("*.yaml"):
        path.unlink()
    (netplan / "10-photo-wall.yaml").write_text(
        "network:\n  version: 2\n  renderer: networkd\n  ethernets:\n"
        "    wired:\n      match:\n        name: 'e*'\n      dhcp4: true\n      optional: true\n")
    (netplan / "10-photo-wall.yaml").chmod(0o600)
    (root / "etc/fstab").write_text("# Verified RAM root is mounted by Photo Wall initramfs.\n")
    (root / "etc/hostname").write_text("photo-wall\n")
    (root / "etc/machine-id").write_bytes(b"")
    resolver = root / "etc/resolv.conf"
    resolver.unlink(missing_ok=True)
    resolver.symlink_to("/run/systemd/resolve/stub-resolv.conf")
    for name in ("getty@.service", "serial-getty@.service", "ssh.service", "ssh.socket",
                 "cloud-init.service", "cloud-init-local.service", "cloud-config.service",
                 "cloud-final.service", "apt-daily.timer", "apt-daily-upgrade.timer"):
        path = root / "etc/systemd/system" / name
        path.unlink(missing_ok=True)
        path.symlink_to("/dev/null")
    journal = root / "etc/systemd/journald.conf.d"
    journal.mkdir(exist_ok=True)
    (journal / "photo-wall.conf").write_text("[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n")
    obsolete_state = root / "var/lib/photo-wall"
    if obsolete_state.exists() and list(obsolete_state.iterdir()):
        raise BuildError("common_state_not_empty")
    return config_hash


def boot_abi(root: Path, boot_tree: Path, kernel: str) -> tuple[str, dict]:
    if not re.fullmatch(r"[a-zA-Z0-9.+-]{1,100}", kernel):
        raise BuildError("kernel_invalid")
    # Generated initramfs and policy/config digests are kept outside this inventory.
    files = {"modules/" + key: value for key, value in inventory(root / "usr/lib/modules" / kernel).items()}
    firmware = root / "usr/lib/firmware"
    if firmware.is_dir():
        files.update({"firmware/" + key: value for key, value in inventory(firmware).items()})
    for key, value in inventory(boot_tree).items():
        if (key.endswith((".dtb", ".dtbo", ".elf", ".dat", ".bin"))
                or key in ("vmlinuz", "kernel8.img", "kernel_2712.img")):
            files["boot/" + key] = value
    # These exact bytes are staged into the guest before policy generation and
    # copied into the initramfs. Keep generated initramfs/policy out of the
    # inventory so the expected ABI has no derived-value cycle.
    boot_logic = {
        "appliance/__init__.py": root / "usr/lib/python3/dist-packages/appliance/__init__.py",
        "appliance/bootstrap.py": root / "usr/lib/python3/dist-packages/appliance/bootstrap.py",
        "appliance/updates.py": root / "usr/lib/python3/dist-packages/appliance/updates.py",
        "contracts/__init__.py": root / "usr/lib/python3/dist-packages/contracts/__init__.py",
        "contracts/release.py": root / "usr/lib/python3/dist-packages/contracts/release.py",
        "initramfs/hooks/photo-wall": root / "etc/initramfs-tools/hooks/photo-wall",
        "initramfs/scripts/photowall": root / "etc/initramfs-tools/scripts/photowall",
    }
    for name, path in boot_logic.items():
        try:
            files["bootstrap-logic/" + name] = checked_file(path, 4 * MIB)
        except (FileNotFoundError, NotADirectoryError):
            raise BuildError("boot_logic_missing") from None
    if not files or not any(key.startswith("boot/") for key in files):
        raise BuildError("boot_abi_empty")
    return hashlib.sha256(canonical(files)).hexdigest(), files


def manifest(rootfs: Path, *, revision: str, boot_abi: str,
             configuration_sha256: str) -> Release:
    record = checked_file(rootfs, MAX_ROOTFS_BYTES)
    return Release(revision=revision, boot_abi=boot_abi, configuration_sha256=configuration_sha256,
                   rootfs_sha256=record["sha256"], rootfs_size=record["size"])


def export_source(repository: Path, destination: Path, revision: str) -> None:
    """Export only committed appliance inputs, with exact Git/file provenance."""
    outside_git(destination)
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise BuildError("revision_invalid")
    if run(["git", "-C", str(repository), "status", "--porcelain"]):
        raise BuildError("source_dirty")
    actual = run(["git", "-C", str(repository), "rev-parse", "HEAD"]).decode().strip()
    if actual != revision:
        raise BuildError("revision_mismatch")
    epoch = int(run(["git", "-C", str(repository), "show", "-s", "--format=%ct", revision]).strip())
    if destination.exists() or destination.is_symlink():
        raise BuildError("output_exists")
    destination.mkdir(mode=0o700)
    paths = ["contracts/__init__.py", "contracts/release.py"]
    tracked = run(["git", "-C", str(repository), "ls-tree", "-r", "--name-only", revision,
                   "appliance"]).decode().splitlines()
    paths.extend(tracked)
    if "appliance/bootstrap.py" not in paths or "appliance/systemd/player.service" not in paths:
        raise BuildError("source_incomplete")
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.write_bytes(run(["git", "-C", str(repository), "show", revision + ":" + relative]))
    record = dict(schema=1, revision=revision, source_epoch=epoch, files=inventory(destination))
    (destination / "source-inventory.json").write_bytes(canonical(record))


def _source_record(source: Path) -> dict:
    from appliance.bootstrap import read_regular

    record = json.loads(read_regular(source / "source-inventory.json", 4 * MIB))
    if (not isinstance(record, dict)
            or set(record) != {"schema", "revision", "source_epoch", "files"}
            or type(record["schema"]) is not int or record["schema"] != 1
            or not isinstance(record["revision"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", record["revision"])
            or type(record["source_epoch"]) is not int or not 0 < record["source_epoch"] < 2**31):
        raise BuildError("source_invalid")
    actual = inventory(source)
    actual.pop("source-inventory.json")
    if actual != record["files"]:
        raise BuildError("source_changed")
    return record


def verify_initramfs(contents: bytes) -> None:
    """Check exact bootstrap packages without confusing Linux media drivers."""
    paths = set(contents.decode("utf-8").splitlines())
    required = {"usr/bin/python3.12", "scripts/photowall", "etc/photo-wall/boot-policy.json"}
    prefix = "usr/lib/python3.12/"
    modules = {"appliance/__init__.py", "appliance/bootstrap.py", "appliance/updates.py",
               "contracts/__init__.py", "contracts/release.py"}
    required.update(prefix + name for name in modules)
    if not required <= paths:
        raise BuildError("initramfs_incomplete")
    for path in paths:
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        package = relative.split("/", 1)[0]
        if (package in {"central", "media", "gi", "pydantic", "PIL"}
                or (package in {"appliance", "contracts"} and "/" in relative and relative not in modules)):
            raise BuildError("initramfs_boundary")


def scrub_root(root: Path) -> None:
    root = _root(root)
    for relative in ("var/log", "var/lib/cloud", "var/lib/snapd", "var/snap", "var/cache/apt",
                     "var/lib/apt/lists", "tmp", "root/.cache"):
        directory = root / relative
        if directory.is_symlink():
            directory.unlink()
            directory.mkdir(mode=0o755)
        elif directory.exists():
            for entry in directory.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
    (root / "tmp").chmod(0o1777)
    for path in (root / "etc/ssh").glob("ssh_host_*"):
        path.unlink()
    for relative in ("var/lib/systemd/random-seed", "var/lib/dbus/machine-id",
                     "boot/firmware/user-data", "boot/firmware/meta-data", "boot/firmware/network-config"):
        (root / relative).unlink(missing_ok=True)
    (root / "etc/machine-id").write_bytes(b"")
    # Preserve distribution copyright/license records even when dropping manuals.
    for relative in ("usr/share/man", "usr/share/info", "usr/share/doc"):
        directory = root / relative
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.name != "copyright":
                path.unlink()
    shadow = root / "etc/shadow"
    if shadow.exists():
        locked = []
        for line in shadow.read_text().splitlines():
            fields = line.split(":")
            if len(fields) >= 2:
                fields[1] = "!"
            locked.append(":".join(fields))
        shadow.write_text("\n".join(locked) + "\n")


def _resolve_root_path(root: Path, path: Path) -> Path:
    """Resolve distro absolute links within its root, never the builder host."""
    pending, resolved, links = list(path.relative_to(root).parts), [], 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not resolved:
                raise BuildError("firmware_link_outside")
            resolved.pop()
            continue
        candidate = root.joinpath(*resolved, part)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise BuildError("firmware_link_missing")
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                resolved = []
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
        else:
            resolved.append(part)
    result = root.joinpath(*resolved)
    if not result.exists():
        raise BuildError("firmware_link_missing")
    return result


def profile_firmware(root: Path, evidence: Path) -> None:
    """Keep the declared Pi5 firmware families and their complete link closure."""
    firmware = _root(root) / "usr/lib/firmware"
    before = inventory(firmware)
    prior = evidence / "firmware-profile.json"
    if prior.exists():
        from appliance.bootstrap import read_regular

        record = json.loads(read_regular(prior, 4 * MIB))
        if record.get("profile") == "pi5-onboard-wired-v1" and record.get("retained") == before:
            return
    families = {"brcm", "cypress", "synaptics", "raspberrypi", "rtl_nic"}
    keep = {name for name in before if "/" not in name or name.split("/", 1)[0] in families}
    pending = list(keep)
    rewritten = {}
    while pending:
        name = pending.pop()
        if "symlink" not in before[name]:
            continue
        target = _resolve_root_path(root, firmware / name)
        try:
            relative = target.relative_to(firmware).as_posix()
        except ValueError:
            raise BuildError("firmware_link_outside") from None
        required = {relative} if relative in before else {key for key in before if key.startswith(relative + "/")}
        if not required:
            raise BuildError("firmware_link_missing")
        # Ubuntu selects some Pi firmware through /etc/alternatives. Bind that
        # selection directly inside the immutable firmware inventory/boot ABI.
        link = os.path.relpath(target, (firmware / name).parent)
        if before[name]["symlink"] != link:
            rewritten[name] = dict(before=before[name]["symlink"], after=link)
        for key in required - keep:
            keep.add(key)
            pending.append(key)
    removed = {name: value for name, value in before.items() if name not in keep}
    for name in removed:
        (firmware / name).unlink()
    for name, change in rewritten.items():
        (firmware / name).unlink()
        (firmware / name).symlink_to(change["after"])
    for directory in sorted((path for path in firmware.rglob("*") if path.is_dir() and not path.is_symlink()),
                            key=lambda path: len(path.parts), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    retained = inventory(firmware)
    expected = {name: ({"symlink": rewritten[name]["after"]} if name in rewritten else before[name]) for name in keep}
    if retained != expected:
        raise BuildError("firmware_profile_changed")
    (evidence / "firmware-profile.json").write_bytes(canonical(dict(
        schema=1, profile="pi5-onboard-wired-v1", retained=retained, removed=removed, rewritten=rewritten,
        removed_bytes=sum(value.get("size", 0) for value in removed.values()))))


def prepare(root: Path, source: Path, wheelhouse: Path, public: Path,
            package_evidence: Path, destination: Path) -> dict:
    """Finish an unsigned common bundle from an isolated customized Ubuntu root."""
    outside_git(destination)
    record = _source_record(source)
    verify_executing_source(record)
    from appliance.bootstrap import read_regular

    player_record = json.loads(read_regular(wheelhouse / "inventory.json", 4 * MIB))
    if player_record.get("revision") != record["revision"]:
        raise BuildError("player_revision_mismatch")
    checked_file(wheelhouse / "source.tar", 16 * MIB, expected=player_record["source_sha256"])
    checked_file(wheelhouse / "requirements.txt", MIB, expected=player_record["requirements_sha256"])
    if destination.exists() or destination.is_symlink():
        raise BuildError("output_exists")
    root = _root(root)
    destination.mkdir(mode=0o700)
    evidence = destination / "inventory"
    shutil.copytree(package_evidence, evidence)
    config_hash = configure_root(root, source, wheelhouse, public, evidence)
    profile_firmware(root, evidence)
    kernels = sorted(path.name for path in (root / "usr/lib/modules").iterdir() if path.is_dir())
    if kernels != ["6.8.0-1047-raspi"]:
        raise BuildError("kernel_unexpected")
    kernel = kernels[0]
    firmware = root / "boot/firmware"
    # Common Pi firmware uses the selected Ubuntu kernel and matching DTBs.
    shutil.copyfile(root / "boot" / ("vmlinuz-" + kernel), firmware / "vmlinuz")
    (firmware / "config.txt").write_text(
        "[all]\narm_64bit=1\nkernel=vmlinuz\ncmdline=cmdline.txt\n"
        "initramfs initrd.img followkernel\ndtoverlay=vc4-kms-v3d\n"
        "disable_fw_kms_setup=1\ndisable_overscan=1\ndisable_splash=1\n"
        "dtparam=audio=off\nenable_uart=0\n")
    (firmware / "cmdline.txt").write_text(
        "boot=photowall ip=dhcp root=/dev/ram0 rw console=tty3 quiet loglevel=3 "
        "vt.global_cursor_default=0 logo.nologo panic=10 watchdog_core.nowayout=1 "
        "bcm2835_wdt.nowayout=1\n")
    abi, abi_inventory = boot_abi(root, firmware, kernel)
    policy = dict(schema=1, boot_abi=abi, configuration_sha256=config_hash)
    (root / "etc/photo-wall/boot-policy.json").write_bytes(canonical(policy))
    initconfig = root / "etc/initramfs-tools/conf.d/photo-wall"
    initconfig.write_text("BOOT=photowall\nMODULES=most\nCOMPRESS=gzip\n")
    in_root(root, "mkinitramfs", "-o", "/boot/firmware/initrd.img", kernel,
            log=evidence / "initramfs-build.log")
    initrd = checked_file(firmware / "initrd.img", 256 * MIB)
    contents = in_root(root, "lsinitramfs", "/boot/firmware/initrd.img")
    (evidence / "initramfs-files.txt").write_bytes(contents)
    verify_initramfs(contents)
    scrub_root(root)
    embedded = root / "usr/share/photo-wall/build"
    embedded.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in ("base-packages.tsv", "packages.tsv", "deb-hashes.json", "python-packages.json"):
        shutil.copyfile(evidence / name, embedded / name)
    for name in ("tool-packages.tsv", "tool-binaries.json", "firmware-profile.json"):
        if (evidence / name).is_file():
            shutil.copyfile(evidence / name, embedded / name)
    (embedded / "source.json").write_bytes(canonical(record))
    (embedded / "player-package.json").write_bytes((wheelhouse / "inventory.json").read_bytes())
    (embedded / "boot-abi.json").write_bytes(canonical(abi_inventory))
    rootfs = destination / "rootfs.squashfs"
    squash(root, rootfs, record["source_epoch"])
    release = manifest(rootfs, revision=record["revision"], boot_abi=abi, configuration_sha256=config_hash)
    rootfs.rename(destination / release.rootfs_name)
    (destination / "release.json").write_bytes(release.encode())
    shutil.copytree(firmware, destination / "boot", symlinks=True)
    shutil.copytree(public, destination / "public")
    report = dict(schema=1, revision=record["revision"], source_epoch=record["source_epoch"],
                  base_sha256=BASE_SHA256, base_size=BASE_BYTES, boot_abi=abi,
                  configuration_sha256=config_hash, rootfs_sha256=release.rootfs_sha256,
                  rootfs_size=release.rootfs_size, initramfs=initrd, files=inventory(destination / "boot"),
                  qualified=dict(rootfs_build=True, native_import=True, vm_boot=False, physical_pi=False))
    (destination / "build.json").write_bytes(canonical(report))
    return report


def finalize(bundle: Path, signature: Path, destination: Path, *, trusted_public: Path) -> dict:
    """Authenticate an externally signed bundle before creating the common disk."""
    from appliance.bootstrap import read_regular
    from appliance.updates import verify_release

    outside_git(destination)
    build = json.loads(read_regular(bundle / "build.json", 4 * MIB))
    public = bundle / "public"
    names = {"public.json", "bootstrap.json", "ca.pem", "release.pub.pem"}
    if ({path.name for path in trusted_public.iterdir()} != names
            or {path.name for path in public.iterdir()} != names):
        raise BuildError("public_inputs_invalid")
    inputs = {name: read_regular(trusted_public / name, MIB) for name in names}
    if any(read_regular(public / name, MIB) != value for name, value in inputs.items()):
        raise BuildError("bundle_public_mismatch")
    config_hash = configuration_digest(inputs)
    release = verify_release(read_regular(bundle / "release.json", 8192),
                             read_regular(signature, 64), trusted_public / "release.pub.pem",
                             build["boot_abi"], config_hash)
    checked_file(bundle / release.rootfs_name, release.rootfs_size, expected=release.rootfs_sha256)
    if inventory(bundle / "boot") != build["files"]:
        raise BuildError("boot_tree_changed")
    # build.json is diagnostic metadata. Authenticate the staged kernel, DTBs,
    # configuration and initramfs through the copy inside the signed rootfs.
    run([sys.executable, "-m", "appliance.build", "verify-release-boot",
         str(bundle / release.rootfs_name), str(bundle / "boot"), release.revision], timeout=600)
    if destination.exists() or destination.is_symlink():
        raise BuildError("output_exists")
    destination.mkdir(mode=0o700)
    tree = destination / "pxe"
    shutil.copytree(bundle / "boot", tree)
    common = tree / "appliance"
    common.mkdir()
    for path in (bundle / "release.json", bundle / release.rootfs_name):
        shutil.copyfile(path, common / path.name)
    shutil.copyfile(signature, common / "release.sig")
    image = destination / f"photo-wall-pi5-{release.revision}-{config_hash[:16]}.img"
    result = create_disk(tree, image, source_epoch=build["source_epoch"])
    report = dict(schema=1, release_id=release.release_id, image=image.name,
                  image_sha256=result["sha256"], image_size=result["size"],
                  pxe_files=inventory(tree), build=build)
    (destination / "artifact.json").write_bytes(canonical(report))
    (destination / "SHA256SUMS").write_text(result["sha256"] + "  " + image.name + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    disk = commands.add_parser("disk")
    disk.add_argument("boot_tree", type=Path)
    disk.add_argument("destination", type=Path)
    disk.add_argument("--source-epoch", type=int, required=True)
    verify_disk_parser = commands.add_parser("verify-disk")
    verify_disk_parser.add_argument("image", type=Path)
    verify_disk_parser.add_argument("boot_tree", type=Path)
    verify_boot_parser = commands.add_parser("verify-release-boot")
    verify_boot_parser.add_argument("rootfs", type=Path)
    verify_boot_parser.add_argument("boot_tree", type=Path)
    verify_boot_parser.add_argument("revision")
    extract = commands.add_parser("extract")
    extract.add_argument("image", type=Path)
    extract.add_argument("destination", type=Path)
    decompress = commands.add_parser("decompress")
    decompress.add_argument("image", type=Path)
    decompress.add_argument("destination", type=Path)
    packages = commands.add_parser("packages")
    packages.add_argument("root", type=Path)
    packages.add_argument("evidence", type=Path)
    export = commands.add_parser("export-source")
    export.add_argument("repository", type=Path)
    export.add_argument("destination", type=Path)
    export.add_argument("revision")
    unsigned = commands.add_parser("prepare")
    for name in ("root", "source", "wheelhouse", "public", "package_evidence", "destination"):
        unsigned.add_argument(name, type=Path)
    finish = commands.add_parser("finalize")
    for name in ("bundle", "signature", "destination"):
        finish.add_argument(name, type=Path)
    finish.add_argument("--trusted-public", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify-release-boot":
        verify_release_boot(args.rootfs, args.boot_tree, args.revision)
        result = {"authenticated_boot_verified": True}
    elif args.command == "verify-disk":
        verify_disk(args.image, args.boot_tree)
        result = {"disk_verified": True}
    elif args.command == "extract":
        extract_base(args.image, args.destination)
        result = {"extracted": True}
    elif args.command == "decompress":
        decompress_base(args.image, args.destination)
        result = {"decompressed": True}
    elif args.command == "packages":
        install_runtime_packages(args.root, args.evidence)
        result = {"runtime_packages": True}
    elif args.command == "export-source":
        export_source(args.repository, args.destination, args.revision)
        result = {"source_revision": args.revision}
    elif args.command == "prepare":
        result = prepare(args.root, args.source, args.wheelhouse, args.public,
                         args.package_evidence, args.destination)
    elif args.command == "finalize":
        result = finalize(args.bundle, args.signature, args.destination, trusted_public=args.trusted_public)
    else:
        result = create_disk(args.boot_tree, args.destination, source_epoch=args.source_epoch)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
