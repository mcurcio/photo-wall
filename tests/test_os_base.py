"""Identity, corruption and metadata contracts for retained installed OS baselines."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from appliance import build as appliance
from scripts import ci_base_cache, os_base

REPOSITORY = Path(__file__).resolve().parents[1]
BUILDER = "ghcr.io/example/builder@sha256:" + "b" * 64


def _tree(tmp_path):
    repository = tmp_path / "repository"
    for name in (*os_base.DEFINITION_FILES, "appliance/build.py"):
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / name, target)
    return repository


def _inputs(tmp_path):
    root, evidence = tmp_path / "root", tmp_path / "evidence"
    (root / "boot/firmware").mkdir(parents=True)
    (root / "boot/firmware/kernel8.img").write_bytes(b"kernel")
    (root / "usr/bin").mkdir(parents=True)
    tool = root / "usr/bin/tool"
    tool.write_bytes(b"tool")
    tool.chmod(0o751)
    (root / "usr/bin/other").hardlink_to(tool)
    (root / "usr/lib-link").symlink_to("/usr/lib")
    evidence.mkdir()
    for name in os_base.REQUIRED_EVIDENCE:
        (evidence / name).write_text("evidence\n")
    (evidence / "package-state.txt").write_text("")
    expected = os_base.definition(REPOSITORY)
    (evidence / "debs").mkdir()
    (evidence / "debs/package.deb").write_bytes(b"package bytes")
    (evidence / "deb-hashes.json").write_bytes(appliance.canonical(appliance.inventory(evidence / "debs")))
    (evidence / "apt-lists").mkdir()
    (evidence / "apt-lists/noble_InRelease").write_bytes(b"signed release")
    (evidence / "apt-lists/noble_Packages").write_bytes(b"authenticated packages")
    (evidence / "verified-input.json").write_bytes(appliance.canonical({
        "sha256": expected["inputs"]["base_sha256"], "size": expected["inputs"]["base_bytes"]}))
    (evidence / "packages.tsv").write_text("".join(
        f"{name}\t1.0\tarm64\n" for name in expected["inputs"]["runtime_packages"]))
    return root, evidence, expected


def _portable_bundle(tmp_path):
    root, evidence, expected = _inputs(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = {"schema": 1, "kind": os_base.KIND, "definition": expected,
                "definition_id": os_base._identity(expected), "builder_image": BUILDER,
                "provenance": os_base._evidence(root, evidence, expected)}
    for field, directory in (("root", root), ("evidence", evidence)):
        path = bundle / (field + ".tar")
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
            archive.add(directory, arcname=".")
        manifest[field] = {"name": path.name, **appliance.checked_file(path, 1024**2)}
    (bundle / "manifest.json").write_bytes(appliance.canonical(manifest))
    return bundle, expected


def test_identity_ignores_application_and_final_assembly(tmp_path):
    repository = _tree(tmp_path)
    before = os_base.definition_id(repository)
    for name in ("player/application.py", "uv.lock", "appliance/bootstrap.py", "scripts/build_ci_image.py"):
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("application changes\n")
    build = repository / "appliance/build.py"
    build.write_text(build.read_text().replace('def configure_root(', 'def changed_configure_root('))
    assert os_base.definition_id(repository) == before
    assert len(before) == 64


@pytest.mark.parametrize("change", ["native", "snapshot", "builder", "extraction", "installation"])
def test_os_inputs_invalidate_identity(tmp_path, change):
    repository = _tree(tmp_path)
    before = os_base.definition_id(repository)
    if change in {"native", "snapshot"}:
        path = repository / "appliance/os_definition.json"
        value = json.loads(path.read_text())
        if change == "native":
            value["runtime_packages"].append("libnew1")
        else:
            value["snapshot"] = "20260906T000000Z"
        path.write_text(json.dumps(value))
    elif change == "builder":
        path = repository / "appliance/build-tools.txt"
        path.write_text(path.read_text() + "# updated\n")
    else:
        path = repository / "appliance/build.py"
        value = path.read_text()
        symbol = "extract_base" if change == "extraction" else "install_runtime_packages"
        start = value.index("def " + symbol + "(")
        end = value.index('\n\n\ndef ', start)
        value = value[:start] + value[start:end].replace('raise BuildError(', 'raise RuntimeError(') + value[end:]
        path.write_text(value)
    assert os_base.definition_id(repository) != before


def test_verify_requires_installed_kind_and_exact_definition(tmp_path):
    bundle, expected = _portable_bundle(tmp_path)
    assert os_base.verify(bundle, expected)["kind"] == os_base.KIND
    with pytest.raises(os_base.BaseError, match="definition_mismatch"):
        os_base.verify(bundle, dict(expected, extra=True))
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    manifest["kind"] = ci_base_cache.KIND
    (bundle / "manifest.json").write_bytes(appliance.canonical(manifest))
    with pytest.raises(os_base.BaseError, match="definition_mismatch"):
        os_base.verify(bundle, expected)


@pytest.mark.parametrize("field", ["root", "evidence"])
def test_corrupt_archives_stop_before_creating_destinations(tmp_path, field):
    bundle, expected = _portable_bundle(tmp_path)
    archive = bundle / (field + ".tar")
    with archive.open("ab") as output:
        output.write(b"corrupt")
    with pytest.raises(os_base.BaseError, match="archive_identity"):
        os_base.restore(bundle, tmp_path / "restored", tmp_path / "restored-evidence", expected)
    assert not (tmp_path / "restored").exists()
    assert not (tmp_path / "restored-evidence").exists()


def test_missing_base_never_builds_or_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(appliance, "install_runtime_packages", lambda *a: pytest.fail("APT invoked"))
    with pytest.raises(os_base.BaseError, match="os_base_missing"):
        os_base.restore(tmp_path / "missing", tmp_path / "out", tmp_path / "evidence", {})


@pytest.mark.parametrize("name,content", [
    ("etc/photo-wall/bootstrap.json", b"{}"),
    ("etc/ssl/private/private.key", b"secret"),
    ("tmp/signing.pem", b"-----BEGIN PRIVATE KEY-----\nsecret"),
    ("home/ubuntu/config", b"personal"),
])
def test_unsafe_archives_rejected_even_with_valid_hashes(tmp_path, name, content):
    bundle, expected = _portable_bundle(tmp_path)
    path = tmp_path / "root" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    archive_path = bundle / "root.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(tmp_path / "root", arcname=".")
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["root"].update(appliance.checked_file(archive_path, 1024**2))
    manifest_path.write_bytes(appliance.canonical(manifest))
    with pytest.raises(os_base.BaseError, match="os_base_invalid"):
        os_base.verify(bundle, expected)


def _gnu_tar():
    result = subprocess.run(["tar", "--version"], capture_output=True, text=True, check=False)
    return result.returncode == 0 and result.stdout.startswith("tar (GNU tar)")


@pytest.mark.skipif(not _gnu_tar(), reason="production metadata preservation requires GNU tar")
def test_publish_restore_metadata_and_native_provenance(tmp_path):
    root, evidence, expected = _inputs(tmp_path)
    bundle = tmp_path / "bundle"
    manifest = os_base.publish(root, evidence, bundle, expected, builder_image=BUILDER)
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o755
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in bundle.iterdir())
    restored, restored_evidence = tmp_path / "restored", tmp_path / "restored-evidence"
    assert os_base.restore(bundle, restored, restored_evidence, expected) == manifest
    tool = restored / "usr/bin/tool"
    assert tool.read_bytes() == b"tool"
    assert stat.S_IMODE(tool.stat().st_mode) == 0o751
    assert tool.stat().st_ino == (restored / "usr/bin/other").stat().st_ino
    assert os.readlink(restored / "usr/lib-link") == "/usr/lib"
    assert (restored / "boot/firmware/kernel8.img").read_bytes() == b"kernel"
    assert appliance.inventory(restored_evidence) == manifest["provenance"]["files"]
    assert hashlib.sha256((bundle / "root.tar").read_bytes()).hexdigest() == manifest["root"]["sha256"]


@pytest.mark.parametrize("damage", ["deb", "indexes", "upstream", "packages"])
def test_incomplete_native_provenance_cannot_be_published(tmp_path, damage):
    root, evidence, expected = _inputs(tmp_path)
    if damage == "deb":
        (evidence / "debs/package.deb").write_bytes(b"changed")
    elif damage == "indexes":
        (evidence / "apt-lists/noble_InRelease").unlink()
    elif damage == "upstream":
        (evidence / "verified-input.json").write_text('{}')
    else:
        (evidence / "packages.tsv").write_text('incomplete\t1\tarm64\n')
    with pytest.raises(os_base.BaseError):
        os_base.publish(root, evidence, tmp_path / "bundle", expected, builder_image=BUILDER)
    assert not (tmp_path / "bundle").exists()


def test_host_identity_is_removed_without_changing_os_files(tmp_path):
    root = tmp_path / "root"
    for name in ("etc/ssl/private/snakeoil.key", "root/.ssh/known_hosts", "home/ubuntu/profile",
                 "etc/ssh/ssh_host_ed25519_key", "etc/machine-id", "var/lib/dbus/machine-id",
                 "var/lib/systemd/random-seed", "etc/resolv.conf"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private build host state')
    os_base._sanitize_host_state(root)
    assert (root / "etc/machine-id").read_bytes() == b""
    assert not (root / "etc/ssl/private").exists()
    assert not (root / "etc/ssh/ssh_host_ed25519_key").exists()
    assert not (root / "var/lib/systemd/random-seed").exists()
    assert os.readlink(root / "etc/resolv.conf") == "/run/systemd/resolve/stub-resolv.conf"


def test_failed_native_preparation_retains_only_bounded_public_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(os_base.platform, "system", lambda: "Linux")
    monkeypatch.setattr(os_base.platform, "machine", lambda: "aarch64")

    def fail_fetch(argv, *, timeout, log):
        log.write_bytes(b"HTTP 503 public upstream failure\n")
        raise appliance.BuildError("tool_failed")

    monkeypatch.setattr(appliance, "run", fail_fetch)
    output = tmp_path / "bundle"
    with pytest.raises(appliance.BuildError, match="tool_failed"):
        os_base.build(REPOSITORY, output, builder_image=BUILDER)
    assert not output.exists()
    assert (tmp_path / "diagnostics/os-base-fetch-ubuntu.log").read_bytes() == (
        b"HTTP 503 public upstream failure\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["diagnostics"]
