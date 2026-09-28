"""Operator release-artifact packaging: presence checks and hash correctness, the manifest's
image digests, byte-for-byte reproducibility, and `verify` against the declared release
(contracts/release.py).

The published set is the netboot base bundle tarball + that bundle's boot tree alone + the
Player `.deb` + the bootstrapper `.deb` -- no signed rootfs, no `release.pub.pem`.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time

import pytest
from support.release_build import EPOCH, IMAGE_REFERENCES, REVISION, cmdline_template, digest, write
from support.release_build import base_bundle as _synthetic_base_bundle
from support.release_build import bootstrapper_deb as _synthetic_bootstrapper_deb
from support.release_build import player_deb as _synthetic_player_deb

from central.assets.os_image import _SQUASHFS_MEMBER, _SUMS_MEMBER
from central.origins.github import _asset_urls, _parse_manifest
from contracts.release import (
    BASE_BOOT,
    BASE_CHECKSUMS,
    BASE_ROOT,
    BASE_SQUASHFS,
    BOOT_REQUIRED,
    BOOT_ROOT,
    CHECKSUMS,
    CMDLINE,
    CMDLINE_PLACEHOLDER,
    FILES,
    IMAGES,
    MANIFEST,
    base_member,
    boot_member,
    tarball_name,
)
from scripts.package_release_artifacts import PackagingError, verify
from scripts.package_release_artifacts import package as _package


def package(bundle, player_deb, bootstrapper_deb, destination, *, revision,
            images=IMAGE_REFERENCES, source_date_epoch=EPOCH):
    return _package(bundle, player_deb, bootstrapper_deb, destination, revision=revision,
                    images=images, source_date_epoch=source_date_epoch)


_write = write


def test_package_produces_expected_bundle_and_debs(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    bootstrapper_deb = _synthetic_bootstrapper_deb(tmp_path)
    destination = tmp_path / "release"

    result = package(bundle, player_deb, bootstrapper_deb, destination, revision=REVISION)

    assert result["revision"] == REVISION
    base_tarball = destination / tarball_name(BASE_ROOT, REVISION)
    assert base_tarball.is_file()
    with tarfile.open(base_tarball) as archive:
        names = set(archive.getnames())
    assert "photo-wall-base/photo-wall-base.squashfs" in names
    assert "photo-wall-base/boot/config.txt" in names
    assert "photo-wall-base/boot/kernel_2712.img" in names
    assert "photo-wall-base/SHA256SUMS" in names

    assert (destination / player_deb.name).read_bytes() == player_deb.read_bytes()
    assert (destination / bootstrapper_deb.name).read_bytes() == bootstrapper_deb.read_bytes()

    assert result["base_image"]["filename"] == base_tarball.name
    assert result["player_deb"]["filename"] == player_deb.name
    assert result["player_deb"]["version"] == "0.1.0+gdeadbeef"
    assert result["bootstrapper_deb"]["filename"] == bootstrapper_deb.name
    assert result["bootstrapper_deb"]["version"] == "0.1.0+gdeadbeef"

    # No signature/trust-anchor artifacts anywhere in the published set
    # (UX over security, no signing).
    for path in destination.iterdir():
        assert "release.pub.pem" not in path.name
        assert not path.name.endswith(".sig")

    # No leftover staging directory.
    assert not (destination / ".staging").exists()


def test_sha256sums_matches_actual_produced_bytes(tmp_path):
    """Mutation probe: corrupting a produced artifact must break this check."""
    bundle = _synthetic_base_bundle(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    bootstrapper_deb = _synthetic_bootstrapper_deb(tmp_path)
    destination = tmp_path / "release"
    package(bundle, player_deb, bootstrapper_deb, destination, revision=REVISION)

    sums_path = destination / "SHA256SUMS"
    entries = {}
    for line in sums_path.read_text().splitlines():
        digest, name = line.split("  ", 1)
        entries[name] = digest

    assert entries.keys() == {
        path.name for path in destination.iterdir() if path.is_file() and path.name != "SHA256SUMS"
    }
    for name, digest in entries.items():
        actual = hashlib.sha256((destination / name).read_bytes()).hexdigest()
        assert actual == digest, f"SHA256SUMS entry for {name} does not match its real bytes"


def test_manifest_shas_match_actual_produced_bytes(tmp_path):
    """Mutation probe: corrupting a produced artifact must break the manifest too."""
    bundle = _synthetic_base_bundle(tmp_path)
    player_deb = _synthetic_player_deb(tmp_path)
    bootstrapper_deb = _synthetic_bootstrapper_deb(tmp_path)
    destination = tmp_path / "release"
    result = package(bundle, player_deb, bootstrapper_deb, destination, revision=REVISION)

    for key in FILES:
        produced = destination / result[key]["filename"]
        assert hashlib.sha256(produced.read_bytes()).hexdigest() == result[key]["sha256"]


def _boot_files(tarball, directory):
    """Each regular file beneath `directory` in `tarball`, by name relative to it, as bytes."""
    with tarfile.open(tarball) as archive:
        return {member.name.removeprefix(directory): archive.extractfile(member).read()
                for member in archive.getmembers()
                if member.isfile() and member.name.startswith(directory + "/")}


def test_the_boot_tarball_is_the_base_tarballs_boot_tree_alone(tmp_path):
    """One build: the boot tarball carries exactly the base tarball's boot/, byte for byte, under
    BOOT_ROOT, and nothing else -- so staging TFTP needs no base squashfs download."""
    bundle = _synthetic_base_bundle(tmp_path)
    destination = tmp_path / "release"
    result = package(bundle, _synthetic_player_deb(tmp_path),
                     _synthetic_bootstrapper_deb(tmp_path), destination, revision=REVISION)

    boot_tarball = destination / tarball_name(BOOT_ROOT, REVISION)
    assert result["boot_image"]["filename"] == boot_tarball.name
    with tarfile.open(boot_tarball) as archive:
        names = set(archive.getnames())
    assert names == {BOOT_ROOT, boot_member(BASE_BOOT),
                     f"{boot_member(BASE_BOOT)}/overlays",
                     *(f"{boot_member(BASE_BOOT)}/{path.relative_to(bundle / 'boot')}"
                       for path in (bundle / "boot").rglob("*") if path.is_file())}
    assert {f"{boot_member(BASE_BOOT)}/{name}" for name in BOOT_REQUIRED} <= names

    boot = _boot_files(boot_tarball, boot_member(BASE_BOOT))
    assert boot == _boot_files(destination / result["base_image"]["filename"],
                               base_member(BASE_BOOT))
    assert boot == {f"/{path.relative_to(bundle / 'boot')}": path.read_bytes()
                    for path in (bundle / "boot").rglob("*") if path.is_file()}

    sums = dict(reversed(line.split("  ", 1))
                for line in (destination / CHECKSUMS).read_text().splitlines())
    assert sums[boot_tarball.name] == hashlib.sha256(boot_tarball.read_bytes()).hexdigest()
    assert sums[boot_tarball.name] == result["boot_image"]["sha256"]
    assert json.loads((destination / MANIFEST).read_bytes())["boot_image"] == result["boot_image"]


def test_missing_base_squashfs_fails_clearly(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    (bundle / "photo-wall-base.squashfs").unlink()
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="base_squashfs_missing"):
        package(bundle, _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision=REVISION)


def test_missing_base_boot_tree_fails_clearly(tmp_path):
    import shutil as _shutil

    bundle = _synthetic_base_bundle(tmp_path)
    _shutil.rmtree(bundle / "boot")
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="base_boot_tree_missing"):
        package(bundle, _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision=REVISION)


def test_missing_bundle_dir_fails_clearly(tmp_path):
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="base_bundle_missing"):
        package(tmp_path / "no-bundle", _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision=REVISION)


def test_missing_player_deb_fails_clearly(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    missing_deb = tmp_path / "photo-wall-player_0.1.0+gdeadbeef_arm64.deb"
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="player_deb_missing"):
        package(bundle, missing_deb, _synthetic_bootstrapper_deb(tmp_path),
                destination, revision=REVISION)


def test_missing_bootstrapper_deb_fails_clearly(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    missing_deb = tmp_path / "photo-wall-bootstrapper_0.1.0+gdeadbeef_arm64.deb"
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="bootstrapper_deb_missing"):
        package(bundle, _synthetic_player_deb(tmp_path), missing_deb,
                destination, revision=REVISION)


def test_unparseable_deb_filename_still_packages_with_no_version(tmp_path):
    """Version parsing is best-effort informational, never load-bearing."""
    bundle = _synthetic_base_bundle(tmp_path)
    odd_deb = tmp_path / "player.deb"
    _write(odd_deb, b"fake-deb-bytes" * 100)
    destination = tmp_path / "release"

    result = package(bundle, odd_deb, _synthetic_bootstrapper_deb(tmp_path),
                     destination, revision=REVISION)

    assert result["player_deb"]["version"] is None
    assert (destination / "player.deb").is_file()


def test_non_deb_player_package_rejected(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    not_a_deb = tmp_path / "photo-wall-player_0.1.0.tar.gz"
    _write(not_a_deb, b"not-a-deb")
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="player_deb_invalid"):
        package(bundle, not_a_deb, _synthetic_bootstrapper_deb(tmp_path),
                destination, revision=REVISION)


def test_invalid_revision_rejected(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    destination = tmp_path / "release"

    with pytest.raises(PackagingError, match="revision_invalid"):
        package(bundle, _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision="not-a-sha")


def test_destination_must_not_already_exist(tmp_path):
    bundle = _synthetic_base_bundle(tmp_path)
    destination = tmp_path / "release"
    destination.mkdir()

    with pytest.raises(PackagingError, match="destination_exists"):
        package(bundle, _synthetic_player_deb(tmp_path),
                _synthetic_bootstrapper_deb(tmp_path), destination, revision=REVISION)


# --- the manifest's images, reproducibility, and the declared release ---------------------------

def _packaged(tmp_path, name="release", **overrides):
    root = tmp_path / f"{name}-inputs"
    destination = tmp_path / name
    manifest = package(_synthetic_base_bundle(root), _synthetic_player_deb(root),
                       _synthetic_bootstrapper_deb(root), destination, revision=REVISION,
                       **overrides)
    return destination, manifest


def test_the_manifest_carries_each_declared_image_by_digest(tmp_path):
    destination, manifest = _packaged(tmp_path)
    expected = {name: {"repository": f"ghcr.io/owner/repo/{name}", "digest": digest(name)}
                for name in IMAGES}
    assert manifest["images"] == expected
    assert json.loads((destination / MANIFEST).read_bytes())["images"] == expected
    assert set(manifest) == {"schema", "revision", *FILES, "images"}


@pytest.mark.parametrize("images", [
    {"central": IMAGE_REFERENCES["central"]},                                  # one missing
    {**IMAGE_REFERENCES, "extra": IMAGE_REFERENCES["central"]},                # undeclared
    {**IMAGE_REFERENCES, "central": "ghcr.io/owner/repo/central:v0.9.1"},      # a tag, no digest
    {**IMAGE_REFERENCES, "central": "ghcr.io/owner/repo/central@sha256:abc"},  # a short digest
    {**IMAGE_REFERENCES, "central": "GHCR.io/Owner/central@" + digest("central")},
], ids=["missing", "undeclared", "tag", "short-digest", "upper-case"])
def test_packaging_refuses_anything_but_every_declared_image_pinned_by_digest(tmp_path, images):
    with pytest.raises(PackagingError, match="image"):
        _packaged(tmp_path, images=images)
    assert not (tmp_path / "release").exists()                  # refused before writing


def test_packaging_the_same_build_twice_yields_the_same_bytes(tmp_path, monkeypatch):
    """So a re-run's seal finds the assets its failed attempt attached (by sha256). The second
    run packages later, from inputs whose file times differ, as two artifact downloads' do."""
    first, _ = _packaged(tmp_path, "first")
    later = time.time() + 86_400
    monkeypatch.setattr(time, "time", lambda: later)
    second_inputs = tmp_path / "second-inputs"
    bundle = _synthetic_base_bundle(second_inputs)
    for path in bundle.rglob("*"):
        os.utime(path, (1_000_000_000, 1_000_000_000))
    package(bundle, _synthetic_player_deb(second_inputs),
            _synthetic_bootstrapper_deb(second_inputs), tmp_path / "second", revision=REVISION)
    for path in sorted(first.iterdir()):
        assert path.read_bytes() == (tmp_path / "second" / path.name).read_bytes(), path.name
    for root in (BASE_ROOT, BOOT_ROOT):
        with tarfile.open(first / tarball_name(root, REVISION)) as archive:
            members = archive.getmembers()
        assert {(member.mtime, member.uid, member.gid, member.uname, member.gname)
                for member in members} == {(EPOCH, 0, 0, "", "")}, root


def test_verify_admits_exactly_the_packaged_release(tmp_path):
    destination, manifest = _packaged(tmp_path)
    packaged = verify(destination, revision=REVISION)
    assert {asset.name for asset in packaged.assets} == {
        MANIFEST, CHECKSUMS, *(manifest[key]["filename"] for key in FILES)}
    for asset in packaged.assets:
        assert hashlib.sha256(asset.path.read_bytes()).hexdigest() == asset.sha256
        assert asset.path.stat().st_size == asset.size
    assert [(image.name, image.reference) for image in packaged.images] == [
        (name, IMAGE_REFERENCES[name]) for name in IMAGES]


def _rewrite_manifest(destination, edit):
    """Edit the manifest and keep SHA256SUMS consistent with it, so only the edit is wrong."""
    manifest = json.loads((destination / MANIFEST).read_bytes())
    edit(manifest)
    (destination / MANIFEST).write_text(json.dumps(manifest))
    sums = [line for line in (destination / CHECKSUMS).read_text().splitlines()
            if not line.endswith(f"  {MANIFEST}")]
    sums.append(f"{hashlib.sha256((destination / MANIFEST).read_bytes()).hexdigest()}  "
                f"{MANIFEST}")
    (destination / CHECKSUMS).write_text("\n".join(sums) + "\n")


def _drop(key):
    return lambda manifest: manifest.pop(key)


@pytest.mark.parametrize("damage, reason", [
    (lambda d, m: (d / m["bootstrapper_deb"]["filename"]).unlink(), "missing"),
    (lambda d, m: (d / m["boot_image"]["filename"]).unlink(), "missing photo-wall-boot"),
    (lambda d, m: (d / CHECKSUMS).unlink(), "missing"),
    (lambda d, m: (d / "stray.txt").write_text("x"), "undeclared stray.txt"),
    (lambda d, m: (d / m["player_deb"]["filename"]).write_bytes(b"other"), "digest_mismatch"),
    (lambda d, m: (d / CHECKSUMS).write_text("0" * 64 + "  manifest.json\n"),
     "checksums_mismatch"),
    (lambda d, m: _rewrite_manifest(d, _drop("images")), "manifest_keys_invalid"),
    (lambda d, m: _rewrite_manifest(d, _drop("bootstrapper_deb")), "manifest_keys_invalid"),
    (lambda d, m: _rewrite_manifest(d, _drop("boot_image")), "manifest_keys_invalid"),
    (lambda d, m: _rewrite_manifest(d, lambda x: x.update(extra=1)), "manifest_keys_invalid"),
    (lambda d, m: _rewrite_manifest(d, lambda x: x.update(schema=2)), "schema"),
    (lambda d, m: _rewrite_manifest(d, lambda x: x.update(revision="b" * 40)), "revision"),
    (lambda d, m: _rewrite_manifest(d, lambda x: x["images"].pop("central")), "images"),
    (lambda d, m: _rewrite_manifest(
        d, lambda x: x["images"]["central"].update(digest="sha256:" + "0" * 7)), "images"),
    (lambda d, m: _rewrite_manifest(
        d, lambda x: x["player_deb"].update(filename="../escape.deb")), "record_invalid"),
    (lambda d, m: _rewrite_manifest(
        d, lambda x: x["player_deb"].update(sha256="0" * 64)), "digest_mismatch"),
], ids=["missing-deb", "missing-boot", "missing-checksums", "stray-file", "changed-deb",
        "checksums", "no-images", "no-bootstrapper", "no-boot-image", "unknown-key", "schema",
        "revision", "image-missing", "image-digest", "path-escape", "recorded-sha"])
def test_verify_refuses_any_difference_from_the_declared_release(tmp_path, damage, reason):
    destination, manifest = _packaged(tmp_path)
    damage(destination, manifest)
    with pytest.raises(PackagingError, match=reason):
        verify(destination, revision=REVISION)


def _retar(destination, manifest, keep=lambda name: True, extra=(), key="base_image"):
    """Rewrite `key`'s tarball with only the members `keep` admits, plus `extra` (name, bytes)
    members in place of any of that name, and record it in the manifest and SHA256SUMS: only
    the layout is wrong."""
    tarball = destination / manifest[key]["filename"]
    replaced = {name for name, _ in extra}
    with tarfile.open(tarball) as source:
        members = [(member, source.extractfile(member).read() if member.isfile() else None)
                   for member in source.getmembers()
                   if keep(member.name) and member.name not in replaced]
    with tarfile.open(tarball, "w:gz") as archive:
        for member, data in members:
            archive.addfile(member, io.BytesIO(data) if data is not None else None)
        for name, data in extra:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    data = tarball.read_bytes()
    record = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    _rewrite_manifest(destination, lambda manifest: manifest[key].update(record))
    sums = [line for line in (destination / CHECKSUMS).read_text().splitlines()
            if not line.endswith(f"  {tarball.name}")]
    sums.append(f"{record['sha256']}  {tarball.name}")
    (destination / CHECKSUMS).write_text("\n".join(sums) + "\n")


@pytest.mark.parametrize("keep, extra, reason", [
    (lambda name: not name.startswith(base_member(BASE_BOOT)), (), "missing:photo-wall-base/boot"),
    (lambda name: name != base_member(BASE_SQUASHFS), (), "missing:photo-wall-base/photo-wall"),
    (lambda name: name != base_member(BASE_CHECKSUMS), (), "missing:photo-wall-base/SHA256SUMS"),
    (lambda name: True, (("stray.txt", b"x"),), "member_invalid:stray.txt"),
    (lambda name: True, ((f"{BASE_ROOT}/../escape", b"x"),), "member_invalid"),
], ids=["no-boot", "no-squashfs", "no-sums", "outside-root", "traversal"])
def test_verify_refuses_a_base_tarball_outside_the_declared_layout(tmp_path, keep, extra,
                                                                   reason):
    destination, manifest = _packaged(tmp_path)
    verify(destination, revision=REVISION)
    _retar(destination, json.loads((destination / MANIFEST).read_bytes()), keep, extra)
    with pytest.raises(PackagingError, match=f"base_tarball_{reason}"):
        verify(destination, revision=REVISION)


_BOOT = boot_member(BASE_BOOT)


@pytest.mark.parametrize("keep, extra, reason", [
    *((lambda name, required=required: name != f"{_BOOT}/{required}", (),
       f"missing:{_BOOT}/{required}") for required in BOOT_REQUIRED),
    (lambda name: not name.startswith(_BOOT), (), f"missing:{_BOOT}/"),
    (lambda name: True, ((f"{_BOOT}/stray.txt", b"x"),), f"mismatch:{_BOOT}/stray.txt"),
    (lambda name: name != f"{_BOOT}/overlays/fake.dtbo", (),
     f"mismatch:{_BOOT}/overlays/fake.dtbo"),
    (lambda name: True, ((f"{_BOOT}/initrd.img", b"another build's initrd"),),
     f"mismatch:{_BOOT}/initrd.img"),
    (lambda name: True, ((boot_member("SHA256SUMS"), b"x"),),
     f"member_invalid:{boot_member('SHA256SUMS')}"),
    (lambda name: True, (("stray.txt", b"x"),), "member_invalid:stray.txt"),
    (lambda name: True, ((f"{_BOOT}/../../escape", b"x"),), "member_invalid"),
], ids=[*(f"no-{name}" for name in BOOT_REQUIRED), "no-boot", "extra-file", "missing-file",
        "other-initrd", "beside-boot", "outside-root", "traversal"])
def test_verify_refuses_a_boot_tarball_that_is_not_the_base_tarballs_boot_tree(
        tmp_path, keep, extra, reason):
    destination, _ = _packaged(tmp_path)
    verify(destination, revision=REVISION)
    _retar(destination, json.loads((destination / MANIFEST).read_bytes()), keep, extra,
           key="boot_image")
    with pytest.raises(PackagingError, match=f"boot_tarball_{reason}"):
        verify(destination, revision=REVISION)


_TEMPLATE = cmdline_template()


@pytest.mark.parametrize("cmdline", [
    f"# TEMPLATE -- substitute {CMDLINE_PLACEHOLDER}\n{_TEMPLATE}",
    f"# {_TEMPLATE}",
    f"{_TEMPLATE}photowall.debug=1\n",
    _TEMPLATE.replace(CMDLINE_PLACEHOLDER, "http://photo-wall/"),
    _TEMPLATE.replace("\n", f" {CMDLINE_PLACEHOLDER}\n"),
    _TEMPLATE.replace("\n", "\r\n"),
    _TEMPLATE.rstrip("\n") + " " * 4096 + "\n",
], ids=["comment-line", "commented-out", "two-lines", "no-placeholder", "placeholder-twice",
        "crlf", "too-long"])
def test_verify_refuses_a_cmdline_that_is_not_the_one_line_template(tmp_path, cmdline):
    """The firmware passes cmdline.txt verbatim: a consumer that replaces the placeholder must
    get one bootable line, so anything but one line holding the placeholder once is refused."""
    destination, _ = _packaged(tmp_path)
    _retar(destination, json.loads((destination / MANIFEST).read_bytes()),
           extra=((f"{_BOOT}/{CMDLINE}", cmdline.encode()),), key="boot_image")
    with pytest.raises(PackagingError, match=f"boot_tarball_cmdline_invalid:{_BOOT}/{CMDLINE}"):
        verify(destination, revision=REVISION)


def test_the_packaged_cmdline_is_the_builders_one_line_template(tmp_path):
    """A trailing newline is allowed but not needed; the seal publishes the builder's template."""
    assert _TEMPLATE.count("\n") == 1 and _TEMPLATE.count(CMDLINE_PLACEHOLDER) == 1
    destination, manifest = _packaged(tmp_path)
    assert _boot_files(destination / manifest["boot_image"]["filename"], _BOOT)[
        f"/{CMDLINE}"] == _TEMPLATE.encode()
    unterminated = _TEMPLATE.rstrip("\n").encode()
    for key, root in (("base_image", base_member(BASE_BOOT)), ("boot_image", _BOOT)):
        _retar(destination, json.loads((destination / MANIFEST).read_bytes()),
               extra=((f"{root}/{CMDLINE}", unterminated),), key=key)
    verify(destination, revision=REVISION)


def test_verify_refuses_a_base_tarball_whose_cmdline_is_not_the_boot_tarballs(tmp_path):
    """The base tarball's boot/ is held to the boot tarball's rules through their equality."""
    destination, _ = _packaged(tmp_path)
    _retar(destination, json.loads((destination / MANIFEST).read_bytes()),
           extra=((f"{base_member(BASE_BOOT)}/{CMDLINE}", b"# TEMPLATE\n" + _TEMPLATE.encode()),))
    with pytest.raises(PackagingError, match=f"boot_tarball_mismatch:{_BOOT}/{CMDLINE}"):
        verify(destination, revision=REVISION)


def test_verify_refuses_a_boot_tree_beside_a_base_tarball_of_another_build(tmp_path):
    """Kernel and initrd can never mismatch: a base tarball whose boot/ differs is refused."""
    destination, _ = _packaged(tmp_path)
    _retar(destination, json.loads((destination / MANIFEST).read_bytes()),
           extra=((f"{base_member(BASE_BOOT)}/kernel_2712.img", b"another build's kernel"),))
    with pytest.raises(PackagingError, match=f"boot_tarball_mismatch:{_BOOT}/kernel_2712.img"):
        verify(destination, revision=REVISION)


def test_the_packaged_tarball_is_the_declared_layout_central_reads(tmp_path):
    """The members Central extracts (central/assets/os_image.py) are the declared ones."""
    destination, manifest = _packaged(tmp_path)
    with tarfile.open(destination / manifest["base_image"]["filename"]) as archive:
        names = set(archive.getnames())
    assert {BASE_ROOT, base_member(BASE_SQUASHFS), base_member(BASE_CHECKSUMS),
            base_member(BASE_BOOT), f"{base_member(BASE_BOOT)}/config.txt"} <= names
    assert (_SQUASHFS_MEMBER, _SUMS_MEMBER) == (base_member(BASE_SQUASHFS),
                                                base_member(BASE_CHECKSUMS))


def test_central_reads_the_packaged_manifest_as_a_deployable_release(tmp_path):
    """The images block is additive: Central's parser (central/origins/github.py) ignores it and
    finds the .deb and the base tarball the packager attached."""
    destination, manifest = _packaged(tmp_path)
    assets = _asset_urls([{"name": path.name, "browser_download_url": f"https://x/{path.name}"}
                          for path in destination.iterdir()])
    parsed = _parse_manifest((destination / MANIFEST).read_bytes(), assets, None)
    assert parsed.package_problem is None
    assert parsed.package.sha256 == manifest["player_deb"]["sha256"]
    assert parsed.os_image.sha256 == manifest["base_image"]["sha256"]
