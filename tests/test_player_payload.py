"""The inert Player archive and dual-manifest release keep legacy readers working."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from support.release_build import EPOCH, IMAGE_REFERENCES, REVISION, with_base_abi
from support.release_build import base_bundle as synthetic_base_bundle
from support.release_build import bootstrapper_deb as synthetic_bootstrapper_deb
from support.release_build import player_deb as synthetic_player_deb

from contracts.player_payload import (
    FORMAT,
    PayloadError,
    archive_name,
    base_abi,
    canonical_json,
    verify_archive,
)
from contracts.release import (
    MANIFEST,
    base_abi_sidecar,
    legacy_projection,
    parse_base_abi_sidecar,
)
from scripts.build_bootstrapper_deb import base_abi_bytes
from scripts.build_player_payload import build
from scripts.module_closure import first_party_packages
from scripts.package_release_artifacts import PackagingError, package, verify


def _payload(tmp_path, *, manifest_bytes: bytes | None = None, extra=None):
    files = {"app/__main__.py": b"import player.service\n",
             "app/closure.json": b"{}\n"}
    manifest = {"schema": 1, "format": FORMAT, "revision": REVISION,
                "base_abi": base_abi("20260904T000000Z", ("python3",),
                                     "sha256:" + "a" * 64),
                "entrypoint": "app", "files": {
                    name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                    for name, data in files.items()}}
    path = tmp_path / archive_name(REVISION)
    with tarfile.open(path, "w:gz") as archive:
        for name, data in (("manifest.json", manifest_bytes or canonical_json(manifest)),
                           *sorted(files.items()), *(extra or ())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return path, manifest


def test_payload_archive_and_dual_release_manifests(tmp_path):
    payload, inner = _payload(tmp_path)
    assert verify_archive(payload) == inner
    destination = tmp_path / "release"
    bundle = with_base_abi(synthetic_base_bundle(tmp_path), inner["base_abi"])
    deb = synthetic_player_deb(tmp_path)
    bootstrapper = synthetic_bootstrapper_deb(tmp_path)
    manifest = package(bundle, deb, bootstrapper, destination, revision=REVISION,
                       images=IMAGE_REFERENCES, source_date_epoch=EPOCH,
                       player_payload=payload)
    assert manifest["schema"] == 2
    assert manifest["player_payload"]["base_abi"] == inner["base_abi"]
    legacy = json.loads((destination / MANIFEST).read_bytes())
    assert legacy["schema"] == 1 and "player_payload" not in legacy
    assert legacy_projection(manifest) == legacy
    assert manifest["base_image"]["base_abi"] == inner["base_abi"]
    assert verify(destination, revision=REVISION).manifest == manifest


def test_payload_manifest_duplicate_key_is_rejected(tmp_path):
    payload, _ = _payload(tmp_path, manifest_bytes=b'{"schema":1,"schema":1}\n')
    with pytest.raises(PayloadError, match="duplicate_key"):
        verify_archive(payload)


def test_base_abi_sidecar_is_bound_to_exact_squashfs(tmp_path):
    payload, inner = _payload(tmp_path)
    bundle = with_base_abi(synthetic_base_bundle(tmp_path), inner["base_abi"])
    (bundle / "photo-wall-base.squashfs").write_bytes(b"same-name-new-build")
    with pytest.raises(PackagingError, match="base_abi_squashfs_mismatch"):
        package(bundle, synthetic_player_deb(tmp_path), synthetic_bootstrapper_deb(tmp_path),
                tmp_path / "release", revision=REVISION, images=IMAGE_REFERENCES,
                source_date_epoch=EPOCH, player_payload=payload)


def test_base_abi_sidecar_requires_canonical_duplicate_free_json():
    abi = "sha256:" + "a" * 64
    digest = "b" * 64
    assert parse_base_abi_sidecar(base_abi_sidecar(abi, digest)) == (abi, digest)
    with pytest.raises(ValueError, match="duplicate_key"):
        parse_base_abi_sidecar(b'{"schema":1,"schema":1}')
    with pytest.raises(ValueError, match="invalid"):
        parse_base_abi_sidecar(b'{"schema":true,"base_abi":"' + abi.encode()
                               + b'","squashfs_sha256":"' + digest.encode() + b'"}\n')


def test_payload_rejects_unlisted_archive_member(tmp_path):
    payload, _ = _payload(tmp_path, extra=(("app/extra.py", b"pass\n"),))
    with pytest.raises(PayloadError, match="files_mismatch"):
        verify_archive(payload)


def test_builder_uses_committed_closure_and_the_base_abi(tmp_path):
    source = Path(__file__).resolve().parents[1]
    repository = tmp_path / "repository"
    repository.mkdir()
    paths = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--", *first_party_packages(source),
         "scripts/debian_packages.py", "pyproject.toml"]
    ).decode().split("\0")
    for name in filter(None, paths):
        origin = source / name
        if origin.is_file():
            target = repository / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
    subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
                   check=True)
    revision = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    a = build(repository, revision, first)
    b = build(repository, revision, second)
    assert a.read_bytes() == b.read_bytes()
    manifest = verify_archive(a)
    assert manifest["base_abi"] == base_abi_bytes(repository).decode().strip()
    assert all(name.startswith("app/") for name in manifest["files"])
