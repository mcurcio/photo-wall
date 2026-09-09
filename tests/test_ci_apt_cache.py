"""Portable boundaries for the untrusted CI APT archive cache."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from appliance import build as appliance
from scripts.ci_apt_cache import (
    AptArchiveCache,
    CacheError,
    completion_receipt,
    parse_plan,
)


def _plan(content: bytes, name: str = "fixture_1.0_arm64.deb") -> bytes:
    digest = hashlib.sha256(content).hexdigest()
    origin = f"https://snapshot.ubuntu.com/ubuntu/{appliance.SNAPSHOT}"
    return (
        f"'{origin}/pool/main/f/fixture/{name}' {name} {len(content)} "
        f"SHA256:{digest}\n"
    ).encode()


def _archives(path: Path) -> Path:
    (path / "partial").mkdir(parents=True)
    return path


def test_plan_requires_snapshot_uri_safe_name_bounds_and_sha256():
    content = b"public fixture"
    record = parse_plan(_plan(content))[0]
    assert record.name == "fixture_1.0_arm64.deb"
    assert record.sha256 == hashlib.sha256(content).hexdigest()
    assert parse_plan(b"") == ()
    for changed in (
        _plan(content).replace(b"SHA256:", b"MD5Sum:"),
        _plan(content).replace(b"https://snapshot.ubuntu.com", b"https://example.invalid"),
        _plan(content).replace(b"fixture_1.0_arm64.deb", b"../escape.deb"),
        _plan(content) + _plan(content),
    ):
        with pytest.raises(CacheError):
            parse_plan(changed)


def test_cache_roundtrip_uses_plan_digest_not_manifest_authority(tmp_path):
    content = b"authenticated package bytes"
    name = "fixture_1.0_arm64.deb"
    plan_data = _plan(content, name)
    cache = AptArchiveCache(tmp_path / "cache")
    plan = cache.plan(plan_data)
    source = _archives(tmp_path / "source")
    (source / name).write_bytes(content)
    assert cache.publish(source, plan)["published"] is True

    target = _archives(tmp_path / "target")
    restored = cache.restore(target, plan)
    assert restored["restored"] == 1
    assert (target / name).read_bytes() == content
    assert (target / name).stat().st_mode & 0o777 == 0o644

    manifest_path = cache.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    digest = next(iter(manifest["objects"]))
    manifest["objects"] = {"0" * 64: manifest["objects"][digest]}
    manifest_path.write_bytes(appliance.canonical(manifest))
    rejected = _archives(tmp_path / "rejected")
    assert cache.restore(rejected, plan)["reason"] == "cache_invalid"
    assert not (rejected / name).exists()


def test_valid_cache_cannot_override_a_different_current_plan(tmp_path):
    name = "fixture_1.0_arm64.deb"
    old, current = b"old signed package", b"current signed package"
    cache = AptArchiveCache(tmp_path / "cache")
    source = _archives(tmp_path / "source")
    (source / name).write_bytes(old)
    assert cache.publish(source, cache.plan(_plan(old, name)))["published"] is True
    target = _archives(tmp_path / "target")
    restored = cache.restore(target, cache.plan(_plan(current, name)))
    assert restored["restored"] == 0
    assert not (target / name).exists()


def test_cache_sanitizes_unplanned_final_bytes_even_when_cache_is_missing(tmp_path):
    content = b"authenticated package bytes"
    name = "fixture_1.0_arm64.deb"
    cache = AptArchiveCache(tmp_path / "missing-cache")
    plan = cache.plan(_plan(content, name))
    archives = _archives(tmp_path / "archives")
    (archives / name).write_bytes(b"X" * len(content))
    result = cache.restore(archives, plan)
    assert result == {
        "requested": True, "hit": False, "restored": 0, "reason": "cache_invalid"
    }
    assert not (archives / name).exists()


def test_cache_rejects_linked_manifest_and_replaces_owned_state_atomically(tmp_path):
    first, second = b"first package", b"second package"
    name = "fixture_1.0_arm64.deb"
    cache = AptArchiveCache(tmp_path / "cache")
    source = _archives(tmp_path / "source")
    (source / name).write_bytes(first)
    assert cache.publish(source, cache.plan(_plan(first, name)))["published"] is True
    (source / name).write_bytes(second)
    assert cache.publish(source, cache.plan(_plan(second, name)))["published"] is True
    assert not (tmp_path / ".cache.replaced").exists()

    manifest = cache.path / "manifest.json"
    manifest.unlink()
    manifest.symlink_to(source / name)
    target = _archives(tmp_path / "target")
    assert cache.restore(target, cache.plan(_plan(second, name)))["reason"] == "cache_invalid"
    assert not (target / name).exists()


def test_failed_publication_preserves_progress_and_cannot_issue_completion_receipt(tmp_path):
    first, second = b"completed first download", b"completed replacement download"
    name = "fixture_1.0_arm64.deb"
    cache = AptArchiveCache(tmp_path / "cache")
    source = _archives(tmp_path / "source")
    (source / name).write_bytes(first)
    assert cache.publish(source, cache.plan(_plan(first, name)))["complete"] is True
    backup = tmp_path / ".cache.replaced"
    backup.mkdir()
    (source / name).write_bytes(second)
    failed = cache.publish(source, cache.plan(_plan(second, name)))
    assert failed == {
        "requested": True,
        "published": False,
        "complete": False,
        "objects": 0,
        "reason": "cache_publish_failed",
    }
    restored = _archives(tmp_path / "restored")
    assert cache.restore(restored, cache.plan(_plan(first, name)))["restored"] == 1
    manifest = tmp_path / "ci-image.json"
    manifest.write_text(json.dumps({"apt_archive_cache": {"publish": failed}}))
    assert completion_receipt(manifest) is False


def test_completion_receipt_requires_exact_types_and_complete_current_plan(tmp_path):
    path = tmp_path / "ci-image.json"
    publish = {
        "requested": True,
        "published": True,
        "complete": True,
        "objects": 2,
        "planned": 3,
    }
    path.write_text(json.dumps({"apt_archive_cache": {"publish": publish}}))
    assert completion_receipt(path) is True
    publish["complete"] = 1
    path.write_text(json.dumps({"apt_archive_cache": {"publish": publish}}))
    assert completion_receipt(path) is False
