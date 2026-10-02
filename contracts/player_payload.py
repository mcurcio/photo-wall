"""Data-only Player application archive contract shared by the builder and base executor.

The digest checks here establish byte consistency, not publisher authenticity. A base image
must compare ``base_abi`` with its own build-time value before activating an archive.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from collections.abc import Iterable
from pathlib import Path
from typing import Final

FORMAT: Final = "pw-player-data-v1"
SCHEMA: Final = 1
ENTRYPOINT: Final = "app"
LAUNCHER: Final = "python3 -I -B <app-dir>"
MAX_ARCHIVE_BYTES: Final = 256 * 1024 * 1024
MAX_MANIFEST_BYTES: Final = 1024 * 1024
MAX_EXPANDED_BYTES: Final = 64 * 1024 * 1024
MAX_FILE_BYTES: Final = 4 * 1024 * 1024
MAX_MEMBERS: Final = 2048
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


class PayloadError(ValueError):
    """A Player archive violates its inert-data layout or recorded content."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("ascii")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PayloadError("manifest_duplicate_key")
        result[key] = value
    return result


def base_abi(snapshot: str, runtime_packages: Iterable[str],
             launcher_contract_digest: str) -> str:
    """Compatibility cohort of the base-owned launcher and pinned Debian runtime declaration.

    The base build must materialize this value from the declaration it actually installed; the
    archive's claim alone cannot establish compatibility. A snapshot change conservatively
    changes the ABI even if the package names remain the same.
    """
    packages = tuple(sorted(runtime_packages))
    if (not isinstance(snapshot, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", snapshot)
            or not isinstance(launcher_contract_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", launcher_contract_digest) is None
            or len(set(packages)) != len(packages)
            or not packages or not all(isinstance(name, str) and
                                      re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", name)
                                      for name in packages)):
        raise PayloadError("base_abi_inputs_invalid")
    body = {"schema": SCHEMA, "launcher": LAUNCHER,
            "launcher_contract_digest": launcher_contract_digest,
            "debian_snapshot": snapshot,
            "runtime_packages": packages}
    return "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()


def archive_name(revision: str) -> str:
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise PayloadError("revision_invalid")
    return f"photo-wall-player-payload-{revision}.tar.gz"


def _file_name(name: object) -> bool:
    if not isinstance(name, str) or not name.startswith("app/") or "\\" in name:
        return False
    parts = name.split("/")
    return (len(parts) >= 2 and all(part not in ("", ".", "..") and
                                    all(32 <= ord(character) < 127 for character in part)
                                    for part in parts)
            and (name.endswith(".py") or name == "app/closure.json"))


def validate_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema", "format", "revision", "base_abi", "entrypoint", "files"
    }:
        raise PayloadError("manifest_keys_invalid")
    if (type(manifest["schema"]) is not int or manifest["schema"] != SCHEMA
            or manifest["format"] != FORMAT
            or not isinstance(manifest["revision"], str)
            or _REVISION.fullmatch(manifest["revision"]) is None
            or not isinstance(manifest["base_abi"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["base_abi"]) is None
            or manifest["entrypoint"] != ENTRYPOINT):
        raise PayloadError("manifest_contract_invalid")
    files = manifest["files"]
    if not isinstance(files, dict) or not 2 <= len(files) <= MAX_MEMBERS - 1:
        raise PayloadError("manifest_files_invalid")
    if not {"app/__main__.py", "app/closure.json"} <= set(files):
        raise PayloadError("manifest_entrypoint_missing")
    total = 0
    for name, record in files.items():
        if (not _file_name(name) or not isinstance(record, dict)
                or set(record) != {"sha256", "size"}
                or not isinstance(record["sha256"], str)
                or _SHA256.fullmatch(record["sha256"]) is None
                or type(record["size"]) is not int
                or not 0 < record["size"] <= MAX_FILE_BYTES):
            raise PayloadError("manifest_files_invalid")
        total += record["size"]
    if total > MAX_EXPANDED_BYTES:
        raise PayloadError("manifest_files_too_large")
    return manifest


def verify_archive(path: Path) -> dict:
    """Read a bounded archive without extraction; reject any active or ambiguous tar member."""
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise PayloadError("archive_limit")
    seen: dict[str, dict[str, int | str]] = {}
    manifest: dict | None = None
    expanded = 0
    try:
        with tarfile.open(path, mode="r|gz") as archive:
            for member in archive:
                if not seen and member.name != "manifest.json":
                    raise PayloadError("archive_manifest_order")
                if (len(seen) >= MAX_MEMBERS or member.name in seen
                        or not member.isfile() or member.mode != 0o644
                        or member.uid != 0 or member.gid != 0
                        or (member.name != "manifest.json" and not _file_name(member.name))):
                    raise PayloadError("archive_member_invalid")
                limit = MAX_MANIFEST_BYTES if member.name == "manifest.json" else MAX_FILE_BYTES
                if not 0 < member.size <= limit:
                    raise PayloadError("archive_member_limit")
                expanded += member.size
                if expanded > MAX_EXPANDED_BYTES + MAX_MANIFEST_BYTES:
                    raise PayloadError("archive_expanded_limit")
                content = archive.extractfile(member)
                if content is None:
                    raise PayloadError("archive_member_invalid")
                digest = hashlib.sha256()
                body = bytearray()
                total = 0
                with content:
                    while block := content.read(min(1024 * 1024, limit + 1 - total)):
                        total += len(block)
                        if total > limit:
                            raise PayloadError("archive_member_limit")
                        digest.update(block)
                        if member.name == "manifest.json":
                            body.extend(block)
                if total != member.size:
                    raise PayloadError("archive_member_truncated")
                seen[member.name] = {"sha256": digest.hexdigest(), "size": total}
                if member.name == "manifest.json":
                    try:
                        manifest = validate_manifest(json.loads(body, object_pairs_hook=_unique_object))
                    except (UnicodeError, json.JSONDecodeError):
                        raise PayloadError("manifest_invalid") from None
    except (tarfile.TarError, OSError, EOFError):
        raise PayloadError("archive_invalid") from None
    if manifest is None or set(seen) != {"manifest.json", *manifest["files"]}:
        raise PayloadError("archive_files_mismatch")
    for name, record in manifest["files"].items():
        if seen[name] != record:
            raise PayloadError("archive_digest_mismatch")
    return manifest
