"""Bounded untrusted transport cache for CI appliance package archives.

APT remains the resolver and authenticates the current snapshot metadata.  This
module admits cache bytes only when they match APT's fresh SHA-256 acquisition
plan; the cache manifest is inventory, never trust authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from appliance import build as appliance

SCHEMA = 1
KIND = "photo-wall-ci-apt-archives"
MAX_MANIFEST_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 4 * 1024**2
MAX_PLAN_BYTES = 256 * 1024
MAX_OBJECTS = 1024
MAX_OBJECT_BYTES = 512 * 1024**2
MAX_TOTAL_BYTES = 2 * 1024**3
OBJECT_NAME = re.compile(r"([0-9a-f]{64})\.deb")
ARCHIVE_NAME = re.compile(r"[a-z0-9][A-Za-z0-9+.%:~_-]{0,250}\.deb")


class CacheError(ValueError):
    """The APT plan or cache violates its bounded public contract."""


@dataclass(frozen=True)
class Archive:
    uri: str
    name: str
    size: int
    sha256: str


def fingerprint() -> dict:
    return {
        "base_sha256": appliance.BASE_SHA256,
        "snapshot": appliance.SNAPSHOT,
        "runtime_packages": list(appliance.RUNTIME_PACKAGES),
        "architecture": platform.machine(),
    }


def parse_plan(data: bytes, *, origin: str | None = None) -> tuple[Archive, ...]:
    """Parse APT's empty-cache, ForceHash=SHA256 acquisition output."""
    if not isinstance(data, bytes) or len(data) > MAX_PLAN_BYTES:
        raise CacheError("apt_plan_size")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise CacheError("apt_plan_encoding") from error
    expected = origin or f"https://snapshot.ubuntu.com/ubuntu/{appliance.SNAPSHOT}/"
    expected_url = urlsplit(expected)
    try:
        expected_port = expected_url.port
    except ValueError as error:
        raise CacheError("apt_plan_origin") from error
    if (expected_url.scheme not in ("http", "https") or not expected_url.hostname
            or expected_url.username is not None or expected_url.password is not None
            or expected_url.query or expected_url.fragment):
        raise CacheError("apt_plan_origin")
    prefix = expected_url.path.rstrip("/") + "/"
    records: list[Archive] = []
    names: set[str] = set()
    total = 0
    for line in text.splitlines():
        if not line:
            continue
        try:
            fields = shlex.split(line, posix=True)
        except ValueError as error:
            raise CacheError("apt_plan_line") from error
        if len(fields) != 4:
            raise CacheError("apt_plan_line")
        uri, name, size_text, digest_text = fields
        location = urlsplit(uri)
        try:
            location_port = location.port
        except ValueError as error:
            raise CacheError("apt_plan_uri") from error
        lowered_path = location.path.lower()
        if (location.scheme != expected_url.scheme or location.hostname != expected_url.hostname
                or location_port != expected_port or location.username is not None
                or location.password is not None or location.query or location.fragment
                or not location.path.startswith(prefix + "pool/")
                or any(part in (".", "..") for part in location.path.split("/"))
                or "%2f" in lowered_path or "%5c" in lowered_path):
            raise CacheError("apt_plan_uri")
        if not ARCHIVE_NAME.fullmatch(name) or name in names:
            raise CacheError("apt_plan_name")
        if not size_text.isascii() or not size_text.isdecimal():
            raise CacheError("apt_plan_size")
        size = int(size_text)
        if not 0 < size <= MAX_OBJECT_BYTES:
            raise CacheError("apt_plan_size")
        algorithm, separator, digest = digest_text.partition(":")
        if (separator != ":" or algorithm != "SHA256" or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            raise CacheError("apt_plan_hash")
        total += size
        if len(records) >= MAX_OBJECTS or total > MAX_TOTAL_BYTES:
            raise CacheError("apt_plan_limit")
        names.add(name)
        records.append(Archive(uri, name, size, digest))
    return tuple(records)


def _validated_file(
    path: Path,
    maximum: int,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    capture: bool = False,
    destination: Path | None = None,
) -> tuple[dict, bytes | None]:
    """Read one stable no-follow regular file and optionally copy it atomically."""
    fd = None
    temporary_fd = None
    temporary_name = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not 0 < before.st_size <= maximum):
            raise CacheError("cache_file_invalid")
        if expected_size is not None and before.st_size != expected_size:
            raise CacheError("cache_file_invalid")
        if destination is not None:
            if destination.exists() or destination.is_symlink():
                raise CacheError("cache_target_exists")
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=".apt-cache-", dir=destination.parent
            )
        digest = hashlib.sha256()
        data = bytearray() if capture else None
        total = 0
        while block := os.read(fd, min(1024 * 1024, maximum + 1 - total)):
            total += len(block)
            if total > maximum:
                raise CacheError("cache_file_invalid")
            digest.update(block)
            if data is not None:
                data.extend(block)
            if temporary_fd is not None:
                view = memoryview(block)
                while view:
                    written = os.write(temporary_fd, view)
                    if written <= 0:
                        raise CacheError("cache_file_invalid")
                    view = view[written:]
        after = os.fstat(fd)
        actual = digest.hexdigest()
        if (total != before.st_size or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or (expected_sha256 is not None and actual != expected_sha256)):
            raise CacheError("cache_file_invalid")
        if temporary_fd is not None:
            os.fsync(temporary_fd)
            os.fchmod(temporary_fd, 0o644)
            os.close(temporary_fd)
            temporary_fd = None
            if destination.exists() or destination.is_symlink():
                raise CacheError("cache_target_exists")
            os.replace(temporary_name, destination)
            temporary_name = None
        return {"sha256": actual, "size": total}, None if data is None else bytes(data)
    except OSError as error:
        raise CacheError("cache_file_invalid") from error
    finally:
        if fd is not None:
            os.close(fd)
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _manifest(cache: Path) -> dict[str, dict]:
    if cache.is_symlink() or not cache.is_dir():
        raise CacheError("cache_missing")
    root_entries = []
    for path in cache.iterdir():
        if len(root_entries) >= 3:
            raise CacheError("cache_layout")
        root_entries.append(path.name)
    if set(root_entries) != {"manifest.json", "objects"}:
        raise CacheError("cache_layout")
    objects = cache / "objects"
    if objects.is_symlink() or not objects.is_dir():
        raise CacheError("cache_layout")
    _record, raw = _validated_file(cache / "manifest.json", MAX_MANIFEST_BYTES, capture=True)
    assert raw is not None
    try:
        manifest = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise CacheError("cache_manifest") from error
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema", "kind", "fingerprint", "objects"}
            or type(manifest["schema"]) is not int or manifest["schema"] != SCHEMA
            or manifest["kind"] != KIND
            or manifest["fingerprint"] != fingerprint()
            or not isinstance(manifest["objects"], dict)):
        raise CacheError("cache_manifest")
    paths = []
    for path in objects.iterdir():
        if len(paths) >= MAX_OBJECTS:
            raise CacheError("cache_limit")
        paths.append(path)
    if len(paths) != len(manifest["objects"]):
        raise CacheError("cache_limit")
    inventory: dict[str, dict] = {}
    total = 0
    for path in paths:
        match = OBJECT_NAME.fullmatch(path.name)
        if match is None:
            raise CacheError("cache_object_name")
        digest = match.group(1)
        expected = manifest["objects"].get(digest)
        if (not isinstance(expected, dict) or set(expected) != {"size"}
                or type(expected["size"]) is not int
                or not 0 < expected["size"] <= MAX_OBJECT_BYTES):
            raise CacheError("cache_manifest")
        record, _data = _validated_file(
            path, MAX_OBJECT_BYTES, expected_size=expected["size"], expected_sha256=digest
        )
        if record["size"] != expected["size"]:
            raise CacheError("cache_object_size")
        total += record["size"]
        if total > MAX_TOTAL_BYTES:
            raise CacheError("cache_limit")
        inventory[digest] = record
    return inventory


def _copy_verified(source: Path, destination: Path, record: Archive) -> None:
    _validated_file(
        source,
        MAX_OBJECT_BYTES,
        expected_size=record.size,
        expected_sha256=record.sha256,
        destination=destination,
    )


def completion_receipt(path: Path) -> bool:
    """Accept only a bounded CI manifest proving a complete current-plan publish."""
    try:
        _record, raw = _validated_file(Path(path), MAX_RECEIPT_BYTES, capture=True)
        assert raw is not None
        manifest = json.loads(raw)
        cache = manifest["apt_archive_cache"]
        publish = cache["publish"]
    except (CacheError, OSError, ValueError, TypeError, KeyError, UnicodeError,
            RecursionError):
        return False
    return (
        isinstance(cache, dict)
        and isinstance(publish, dict)
        and set(publish) == {
            "requested", "published", "complete", "objects", "planned"
        }
        and publish["requested"] is True
        and publish["published"] is True
        and publish["complete"] is True
        and type(publish["objects"]) is int
        and type(publish["planned"]) is int
        and 0 < publish["objects"] <= publish["planned"] <= MAX_OBJECTS
    )


class AptArchiveCache:
    """Restore and publish plan-matched objects under one owned cache path."""

    def __init__(self, path: Path, *, origin: str | None = None):
        self.path = Path(path).absolute()
        appliance.outside_git(self.path)
        self.origin = origin

    def plan(self, data: bytes) -> tuple[Archive, ...]:
        return parse_plan(data, origin=self.origin)

    def restore(self, archives: Path, plan: tuple[Archive, ...]) -> dict:
        archives = Path(archives).absolute()
        if archives.is_symlink() or not archives.is_dir():
            raise CacheError("cache_target_invalid")
        restored = 0
        for record in plan:
            destination = archives / record.name
            if not destination.exists() and not destination.is_symlink():
                continue
            try:
                current, _data = _validated_file(
                    destination,
                    MAX_OBJECT_BYTES,
                    expected_size=record.size,
                    expected_sha256=record.sha256,
                )
                restored += 1
                continue
            except (CacheError, OSError):
                pass
            destination.unlink(missing_ok=True)
        try:
            inventory = _manifest(self.path)
        except (CacheError, OSError):
            return {"requested": True, "hit": restored > 0, "restored": restored,
                    "reason": "cache_invalid"}
        for record in plan:
            destination = archives / record.name
            if destination.exists() or destination.is_symlink():
                continue
            if record.sha256 not in inventory:
                continue
            try:
                _copy_verified(self.path / "objects" / (record.sha256 + ".deb"),
                               destination, record)
            except (CacheError, OSError):
                continue
            restored += 1
        return {"requested": True, "hit": restored > 0, "restored": restored,
                "available": len(inventory), "planned": len(plan)}

    def publish(self, archives: Path, plan: tuple[Archive, ...]) -> dict:
        archives = Path(archives).absolute()
        if archives.is_symlink() or not archives.is_dir():
            raise CacheError("cache_source_invalid")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".photo-wall-apt-cache-", dir=self.path.parent))
        backup = self.path.parent / ("." + self.path.name + ".replaced")
        try:
            objects = staging / "objects"
            objects.mkdir(mode=0o700)
            accepted: dict[str, dict] = {}
            completed = 0
            for record in plan:
                source = archives / record.name
                if not source.exists() or source.is_symlink():
                    continue
                try:
                    if record.sha256 in accepted:
                        _validated_file(
                            source,
                            MAX_OBJECT_BYTES,
                            expected_size=record.size,
                            expected_sha256=record.sha256,
                        )
                    else:
                        _copy_verified(
                            source, objects / (record.sha256 + ".deb"), record
                        )
                except (CacheError, OSError):
                    continue
                accepted[record.sha256] = {"size": record.size}
                completed += 1
            if not accepted:
                return {"requested": True, "published": False, "objects": 0,
                        "complete": False, "reason": "cache_empty"}
            (staging / "manifest.json").write_bytes(appliance.canonical({
                "schema": SCHEMA, "kind": KIND, "fingerprint": fingerprint(),
                "objects": dict(sorted(accepted.items())),
            }))
            _manifest(staging)
            if backup.exists() or backup.is_symlink():
                raise CacheError("cache_backup_exists")
            replaced = False
            if self.path.exists() or self.path.is_symlink():
                if self.path.is_symlink() or not self.path.is_dir():
                    raise CacheError("cache_path_invalid")
                os.replace(self.path, backup)
                replaced = True
            try:
                os.replace(staging, self.path)
            except BaseException:
                if replaced:
                    os.replace(backup, self.path)
                raise
            if replaced:
                shutil.rmtree(backup)
            return {
                "requested": True,
                "published": True,
                "complete": completed == len(plan),
                "objects": len(accepted),
                "planned": len(plan),
            }
        except (CacheError, OSError):
            return {"requested": True, "published": False, "objects": 0,
                    "complete": False, "reason": "cache_publish_failed"}
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-completion-receipt", type=Path, required=True)
    args = parser.parse_args()
    if not completion_receipt(args.verify_completion_receipt):
        parser.exit(1, "APT cache completion receipt invalid\n")


if __name__ == "__main__":
    main()
