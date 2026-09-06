"""Build a generic ARM64 VM test initramfs from the signed Pi initramfs.

This is deliberately a test artifact builder.  It never edits the production
rootfs, kernel, or module tree.  The resulting initramfs keeps the original
cpio segment order and production userspace bytes, replacing only the kernel
module tree and adding an explicitly test-only boot report hook.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

from appliance.build import BuildError, canonical, checked_file, inventory, outside_git, run

MAX_INITRD_BYTES = 256 * 1024**2
MAX_KERNEL_BYTES = 256 * 1024**2
MAX_MODULE_BYTES = 1 * 1024**3
# The generic manifest contains bounded module inventories.  The measured v2
# output is 3,365,677 bytes; keep ample room for kernel/module metadata while
# retaining a finite consumer-side read budget.
MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_FILES = 100_000
COMMAND_TIMEOUT = 300
PRODUCTION_INITRD_SIZE = 64_614_282
PRODUCTION_INITRD_SHA256 = "98435d9d6d785aea06676ceb9319284e61baeff99a78b1ef253006d9ff6d2d8e"
REQUIRED_MODULES = ("virtio_pci", "virtio_blk", "virtio_net", "loop", "squashfs",
                    "overlay", "ext4", "vfat")
PROTECTED_PREFIXES = ("usr/bin/python3.12", "usr/lib/python3.12", "etc/photo-wall",
                      "scripts/photowall")
HOOK_PATH = "scripts/init-bottom/photo-wall-evidence"
ORDER_PATH = "scripts/init-bottom/ORDER"
ORDER_ADDITION = b'/scripts/init-bottom/photo-wall-evidence "$@"\n'
HOOK_BYTES = (
    b"#!/bin/sh\n"
    b"# Test-only serial observability; production boot logic stays unchanged.\n"
    b"set -eu\n"
    b"report=/run/photo-wall/boot.json\n"
    b"if [ -f \"$report\" ]; then\n"
    b"    printf '%s\\n' 'photo-wall: boot report'\n"
    b"    cat -- \"$report\"\n"
    b"fi\n"
)


def _manifest_bytes(value: dict) -> bytes:
    """Encode and bound the manifest before publishing it."""
    payload = canonical(value)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise BuildError("manifest_limit")
    return payload


def _path_record(path: Path, maximum: int) -> dict:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return {"symlink": os.readlink(path)}
    if stat.S_ISREG(info.st_mode):
        if info.st_size == 0:
            return {"sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "size": 0}
        return checked_file(path, maximum)
    raise BuildError("initramfs_unsafe_entry")


def _protected_inventory(root: Path) -> dict[str, dict]:
    """Hash production interpreter/configuration files without following links."""
    result: dict[str, dict] = {}
    for prefix in PROTECTED_PREFIXES:
        path = root / prefix
        if not os.path.lexists(path):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
            result[prefix] = _path_record(path, MAX_INITRD_BYTES)
        elif stat.S_ISDIR(info.st_mode):
            for relative, record in inventory(path, maximum_files=MAX_FILES,
                                              maximum_bytes=MAX_INITRD_BYTES).items():
                result[prefix + "/" + relative] = record
        else:
            raise BuildError("initramfs_unsafe_entry")
    return dict(sorted(result.items()))


def _content_inventory(root: Path) -> dict[str, dict]:
    """Inventory regular/symlink contents while allowing initramfs device nodes."""
    result: dict[str, dict] = {}
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        for name in sorted(names + files):
            path = Path(directory) / name
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                result[relative] = {"symlink": os.readlink(path)}
            elif stat.S_ISREG(info.st_mode):
                entry = _path_record(path, MAX_MODULE_BYTES)
                total += entry["size"]
                result[relative] = entry
            if len(result) > MAX_FILES or total > MAX_MODULE_BYTES:
                raise BuildError("initramfs_tree_limit")
    return dict(sorted(result.items()))


def _allowed_mutation(path: str, before: dict | None, after: dict | None) -> bool:
    if path == "conf/modules":
        return True
    if path == HOOK_PATH:
        return before is None and after is not None
    if path == ORDER_PATH:
        return True
    if path == "lib" and before == {"symlink": "/usr/lib"} and after == {"symlink": "usr/lib"}:
        return True
    return path == "lib" and before == {"symlink": "usr/lib"} and after == {"symlink": "usr/lib"}


def _verify_preserved(before: dict[str, dict], after: dict[str, dict]) -> None:
    """Allow only the declared hook/config/module/root-lib substitutions."""
    for path, record in before.items():
        if path.startswith("lib/modules/") or path.startswith("usr/lib/modules/"):
            continue
        if _allowed_mutation(path, record, after.get(path)):
            continue
        if after.get(path) != record:
            raise BuildError("production_bytes_changed")
    for path, record in after.items():
        if path in before or path.startswith("lib/modules/") or path.startswith("usr/lib/modules/"):
            continue
        if not _allowed_mutation(path, before.get(path), record):
            raise BuildError("unexpected_initramfs_change")


def _segments(extracted: Path) -> tuple[list[Path], dict]:
    """Inspect unmkinitramfs output and return its actual cpio segments."""
    entries = sorted(extracted.iterdir(), key=lambda path: path.name)
    directories = [path for path in entries if path.is_dir() and not path.is_symlink()]
    files = [path for path in entries if not path.is_dir()]
    names = [path.name for path in directories]
    if names and set(names) >= {"early", "main"}:
        if files or set(names) != {"early", "main"}:
            raise BuildError("initramfs_layout")
        ordered = [extracted / "early", extracted / "main"]
        return ordered, {"kind": "early-main", "segments": [path.name for path in ordered]}
    if len(directories) == 1 and not files:
        return directories, {"kind": "single", "segments": [directories[0].name]}
    raise BuildError("initramfs_layout")


def _newc_end(data: bytes, start: int) -> int:
    """Return the end offset of one newc archive, including its trailer padding."""
    offset = start
    while True:
        if data[offset:offset + 6] not in (b"070701", b"070702") or offset + 110 > len(data):
            raise BuildError("initramfs_cpio_format")
        namesize = int(data[offset + 94:offset + 102], 16)
        filesize = int(data[offset + 54:offset + 62], 16)
        name_start = offset + 110
        name_end = name_start + namesize
        if name_end > len(data):
            raise BuildError("initramfs_cpio_truncated")
        payload = (name_end + 3) & ~3
        payload_end = payload + filesize
        if payload_end > len(data):
            raise BuildError("initramfs_cpio_truncated")
        name = data[name_start:name_end].rstrip(b"\0")
        offset = (payload_end + 3) & ~3
        if name == b"TRAILER!!!":
            return offset


def _input_encoding(source: Path) -> dict:
    """Inspect raw cpio prefixes before the compressed main archive."""
    with source.open("rb") as stream:
        data = stream.read(MAX_INITRD_BYTES + 1)
    if len(data) > MAX_INITRD_BYTES:
        raise BuildError("initramfs_limit")
    offset, early = 0, 0
    while data[offset:offset + 6] in (b"070701", b"070702"):
        offset = _newc_end(data, offset)
        early += 1
        # initramfs-tools pads the raw prefix to a larger alignment before
        # the compressed main member.  The padding is not another segment.
        while offset < len(data) and data[offset] == 0:
            offset += 1
    if offset == len(data):
        return {"early_cpio_segments": early, "main": "cpio"}
    if data[offset:offset + 2] == b"\x1f\x8b":
        return {"early_cpio_segments": early, "main": "gzip"}
    raise BuildError("initramfs_cpio_prefix")


def _normalize_root_lib(segment: Path) -> bool:
    """Turn the known absolute initrd /lib -> /usr/lib link into a safe link."""
    path = segment / "lib"
    if not path.is_symlink():
        return False
    target = os.readlink(path)
    if target == "usr/lib":
        return False
    if target != "/usr/lib":
        raise BuildError("initramfs_rootlib_link")
    path.unlink()
    path.symlink_to("usr/lib")
    return True


def _module_roots(segments: list[Path]) -> list[Path]:
    result = []
    seen: set[tuple[int, int]] = set()
    for segment in segments:
        _normalize_root_lib(segment)
        for relative in ("lib/modules", "usr/lib/modules"):
            path = segment / relative
            if not os.path.lexists(path):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise BuildError("initramfs_module_link")
            if not stat.S_ISDIR(info.st_mode):
                raise BuildError("initramfs_module_tree")
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                result.append(path)
                seen.add(key)
    return result


def _safe_source_link(source_root: Path, path: Path) -> bool:
    target = os.readlink(path)
    if os.path.isabs(target):
        return False
    candidate = Path(os.path.normpath(str(path.parent / target)))
    root = Path(os.path.normpath(str(source_root.absolute())))
    return candidate == root or root in candidate.parents


def _copy_module_tree(source: Path, destination: Path) -> list[str]:
    """Copy runtime module files, dropping only links that escape the tree."""
    skipped: list[str] = []

    def copy_directory(src: Path, dst: Path) -> None:
        dst.mkdir(mode=0o755, parents=True, exist_ok=True)
        for entry in sorted(src.iterdir(), key=lambda item: item.name):
            target = dst / entry.name
            info = entry.lstat()
            relative = entry.relative_to(source).as_posix()
            if stat.S_ISLNK(info.st_mode):
                if not _safe_source_link(source, entry):
                    skipped.append(relative)
                    continue
                target.symlink_to(os.readlink(entry))
            elif stat.S_ISDIR(info.st_mode):
                copy_directory(entry, target)
            elif stat.S_ISREG(info.st_mode):
                shutil.copy2(entry, target)
            else:
                raise BuildError("generic_module_entry")

    copy_directory(source, destination)
    return skipped


def _module_files(root: Path, release: str) -> dict[str, dict]:
    path = root / "lib/modules" / release
    if not path.is_dir() or path.is_symlink():
        raise BuildError("generic_modules_missing")
    return inventory(path, maximum_files=MAX_FILES, maximum_bytes=MAX_MODULE_BYTES)


def _module_status(root: Path, name: str, release: str) -> str | None:
    path = root / "lib/modules" / release
    for directory, _, files in os.walk(path, followlinks=False):
        if any(file.startswith(name + ".ko") for file in files):
            return "module"
    builtins = path / "modules.builtin"
    if builtins.is_file() and any(Path(line.strip()).name == name + ".ko"
                                  for line in builtins.read_text().splitlines() if line.strip()):
        return "builtin"
    return None


def _has_module(root: Path, name: str, release: str) -> bool:
    return _module_status(root, name, release) is not None


def _remove_module_trees(segments: list[Path]) -> set[str]:
    releases: set[str] = set()
    for path in _module_roots(segments):
        for entry in path.iterdir():
            if entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                releases.add(entry.name)
                shutil.rmtree(entry)
            else:
                raise BuildError("initramfs_module_tree")
    return releases


def _append_modules(segments: list[Path]) -> None:
    target = None
    for segment in reversed(segments):
        candidate = segment / "conf/modules"
        if os.path.lexists(candidate):
            target = candidate
            break
    if target is None:
        target = segments[-1] / "conf/modules"
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.write_bytes(b"")
    if target.is_symlink() or not target.is_file():
        raise BuildError("initramfs_modules_config")
    original = target.read_bytes()
    lines = original.decode("utf-8", errors="strict").splitlines()
    present = {line.strip().split()[0] for line in lines if line.strip() and not line.lstrip().startswith("#")}
    additions = [name for name in REQUIRED_MODULES if name not in present]
    if additions:
        if original and not original.endswith(b"\n"):
            original += b"\n"
        target.write_bytes(original + ("\n".join(additions) + "\n").encode())


def _add_hook(segments: list[Path]) -> None:
    target = segments[-1] / HOOK_PATH
    if os.path.lexists(target):
        raise BuildError("initramfs_hook_exists")
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    target.write_bytes(HOOK_BYTES)
    target.chmod(0o755)
    order = target.parent / "ORDER"
    if os.path.lexists(order):
        if order.is_symlink() or not order.is_file():
            raise BuildError("initramfs_order_invalid")
        original = order.read_bytes()
    else:
        original = b""
    if ORDER_ADDITION in original:
        raise BuildError("initramfs_order_exists")
    order.write_bytes(ORDER_ADDITION + original)
    if any(word in HOOK_BYTES.lower() for word in (b"password", b"token", b"secret", b"credential")):
        raise BuildError("initramfs_hook_sensitive")


def _command_to_file(argv: list[str], destination: Path, *, input_bytes: bytes | None = None,
                     maximum: int, cwd: Path | None = None,
                     timeout: int = COMMAND_TIMEOUT) -> dict:
    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            child = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                     stdout=stream, stderr=subprocess.PIPE, start_new_session=True, cwd=cwd)
            try:
                stderr = child.communicate(input=input_bytes, timeout=timeout)[1]
            except subprocess.TimeoutExpired as error:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate()
                raise BuildError("tool_timeout") from error
            if len(stderr) > 4 * 1024**2 or child.returncode:
                raise BuildError("tool_failed")
        return checked_file(destination, maximum)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def _cpio_archive(segment: Path, destination: Path) -> dict:
    paths = ["."]
    for directory, names, files in os.walk(segment, followlinks=False):
        base = Path(directory)
        for name in sorted(names + files):
            path = base / name
            if path.is_symlink() or path.is_file() or path.is_dir():
                paths.append(path.relative_to(segment).as_posix())
    names = ("\0".join(sorted(set(paths))) + "\0").encode()
    help_text = run(["cpio", "--help"], timeout=15)
    args = ["cpio", "--null", "--create", "--format=newc"]
    if b"reproducible" in help_text:
        args.append("--reproducible")
    return _command_to_file(args, destination, input_bytes=names, maximum=MAX_INITRD_BYTES, cwd=segment)


def _gzip_archive(source: Path, destination: Path) -> dict:
    return _command_to_file(["gzip", "-n", "-c", str(source)], destination,
                             maximum=MAX_INITRD_BYTES)


def _kernel_image(source: Path, destination: Path) -> dict:
    source_record = checked_file(source, MAX_KERNEL_BYTES)
    with source.open("rb") as stream:
        magic = stream.read(2)
    if magic == b"\x1f\x8b":
        record = _command_to_file(["gzip", "-dc", str(source)], destination,
                                  maximum=MAX_KERNEL_BYTES)
    else:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        record = checked_file(destination, MAX_KERNEL_BYTES)
    with destination.open("rb") as stream:
        stream.seek(0x38)
        if stream.read(4) != b"ARM\x64":
            raise BuildError("kernel_not_arm64_image")
    record["source"] = source_record
    record["compressed_input"] = magic == b"\x1f\x8b"
    return record


def _kernel_release(kernel: Path, modules: Path) -> str:
    name = kernel.name
    release = name.removeprefix("vmlinuz-")
    if release == name or not release or modules.name != release:
        raise BuildError("kernel_modules_mismatch")
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.+-_" for character in release):
        raise BuildError("kernel_release_invalid")
    return release


def _extract(source: Path, destination: Path) -> tuple[list[Path], dict]:
    destination.mkdir(mode=0o700)
    run(["unmkinitramfs", str(source), str(destination)], timeout=COMMAND_TIMEOUT)
    return _segments(destination)


def build(input_initrd: Path, generic_kernel: Path, generic_modules: Path,
          output: Path, *, expected_size: int = PRODUCTION_INITRD_SIZE,
          expected_sha256: str = PRODUCTION_INITRD_SHA256) -> dict:
    outside_git(output)
    if output.exists() or output.is_symlink():
        raise BuildError("output_exists")
    if not re_fullmatch_sha256(expected_sha256) or type(expected_size) is not int or expected_size <= 0:
        raise BuildError("expected_input_invalid")
    input_record = checked_file(input_initrd, MAX_INITRD_BYTES, expected=expected_sha256)
    if input_record["size"] != expected_size:
        raise BuildError("input_size")
    input_encoding = _input_encoding(input_initrd)
    generic_modules = generic_modules.absolute()
    if generic_modules.is_symlink() or not generic_modules.is_dir():
        raise BuildError("generic_modules_invalid")
    source_module_record = inventory(generic_modules, maximum_files=MAX_FILES,
                                    maximum_bytes=MAX_MODULE_BYTES)
    release = _kernel_release(generic_kernel, generic_modules)
    kernel_source_record = checked_file(generic_kernel, MAX_KERNEL_BYTES)
    output_parent = output.parent
    output_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".generic-boot-", dir=output_parent) as temporary:
        workspace = Path(temporary)
        original_tree = workspace / "original"
        segments, layout = _extract(input_initrd, original_tree)
        protected_before = {path.name: _protected_inventory(path) for path in segments}
        content_before = {path.name: _content_inventory(path) for path in segments}
        original_module_releases = _remove_module_trees(segments)
        _append_modules(segments)
        _add_hook(segments)
        module_segment = segments[-1]
        module_root = module_segment / "lib/modules" / release
        skipped_links = _copy_module_tree(generic_modules, module_root)
        copied_module_record = _module_files(module_segment, release)
        run(["depmod", "-b", str(module_segment), release], timeout=COMMAND_TIMEOUT)
        module_status = {name: _module_status(module_segment, name, release)
                         for name in REQUIRED_MODULES}
        for name in REQUIRED_MODULES:
            if not _has_module(module_segment, name, release):
                raise BuildError("generic_module_missing")
        for segment in segments:
            _verify_preserved(content_before[segment.name], _content_inventory(segment))
        archives = []
        for index, segment in enumerate(segments):
            cpio = workspace / f"segment-{index}.cpio"
            _cpio_archive(segment, cpio)
            # initramfs-tools keeps the early cpio prefix uncompressed.  Keep
            # that representation so splitinitramfs sees the same ordering;
            # only the modified main archive is gzip-compressed.
            if index == 0 and input_encoding["early_cpio_segments"]:
                archives.append(cpio)
            else:
                gzip = workspace / f"segment-{index}.gz"
                _gzip_archive(cpio, gzip)
                archives.append(gzip)
        initrd = workspace / "initrd.img"
        with initrd.open("xb") as destination:
            for archive in archives:
                with archive.open("rb") as source:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
        initrd_record = checked_file(initrd, MAX_INITRD_BYTES)
        reopened = workspace / "reopened"
        reopened_segments, reopened_layout = _extract(initrd, reopened)
        if reopened_layout != layout:
            raise BuildError("reopened_initramfs_layout")
        protected_after = {path.name: _protected_inventory(path) for path in reopened_segments}
        if protected_after != protected_before:
            raise BuildError("reopened_production_bytes_changed")
        for segment in reopened_segments:
            _verify_preserved(content_before[segment.name], _content_inventory(segment))
        if not any((path / HOOK_PATH).is_file() for path in reopened_segments):
            raise BuildError("reopened_hook_missing")
        reopened_module_roots = _module_roots(reopened_segments)
        reopened_releases = {entry.name for root in reopened_module_roots for entry in root.iterdir()
                             if entry.is_dir() and not entry.is_symlink()}
        if reopened_releases != {release}:
            raise BuildError("reopened_module_release")
        if any(not _has_module(segment, name, release) for segment in reopened_segments for name in REQUIRED_MODULES
               if (segment / "lib/modules" / release).is_dir()):
            raise BuildError("reopened_module_missing")
        reopened_module_status = {
            segment.name: {name: _module_status(segment, name, release) for name in REQUIRED_MODULES}
            for segment in reopened_segments if (segment / "lib/modules" / release).is_dir()
        }
        kernel = workspace / "Image"
        kernel_record = _kernel_image(generic_kernel, kernel)
        output.mkdir(mode=0o700)
        shutil.copyfile(initrd, output / "initrd.img")
        shutil.copyfile(kernel, output / "Image")
        manifest = {
            "schema": 1,
            "kind": "generic-vm-initramfs",
            "input": {"initrd": input_record, "expected_sha256": expected_sha256,
                       "expected_size": expected_size, "kernel": kernel_source_record,
                       "modules": {"release": release, "tree": source_module_record,
                                   "copied_tree": copied_module_record,
                                   "reopened_tree": {
                                       segment.name: _module_files(segment, release)
                                       for segment in reopened_segments
                                       if (segment / "lib/modules" / release).is_dir()} }},
            "outputs": {"initrd": {"name": "initrd.img", **initrd_record},
                        "kernel": {"name": "Image", **{key: value for key, value in kernel_record.items()
                                                           if key != "source"}}},
            "layout": {"original": layout, "reopened": reopened_layout,
                       "input_encoding": input_encoding},
            "preserved": {"production_files": protected_before},
            "substitutions": [
                {"kind": "kernel", "replacement": "generic ARM64 Image", "release": release},
                {"kind": "modules", "removed_releases": sorted(original_module_releases),
                 "replacement_release": release, "skipped_unsafe_links": skipped_links},
                {"kind": "observability-hook", "path": "/" + HOOK_PATH,
                 "order_path": "/" + ORDER_PATH,
                 "order_addition": ORDER_ADDITION.decode(),
                 "behavior": "print /run/photo-wall/boot.json to the serial console"},
            ],
            "validation": {"reopened": True, "protected_bytes_equal": True,
                           "required_modules": list(REQUIRED_MODULES),
                           "module_status": module_status,
                           "reopened_module_status": reopened_module_status,
                           "original_pi_modules_absent": not bool(original_module_releases & {release})},
            "qualified": {"generic_vm_boot": False, "physical_pi_boot": False},
        }
        (output / "manifest.json").write_bytes(_manifest_bytes(manifest))
        return manifest


def re_fullmatch_sha256(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-initrd", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--modules", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-input-size", type=int, default=PRODUCTION_INITRD_SIZE)
    parser.add_argument("--expected-input-sha256", default=PRODUCTION_INITRD_SHA256)
    args = parser.parse_args()
    result = build(args.input_initrd, args.kernel, args.modules, args.output_dir,
                   expected_size=args.expected_input_size,
                   expected_sha256=args.expected_input_sha256)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
