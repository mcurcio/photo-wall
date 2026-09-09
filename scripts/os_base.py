"""Versioned installed OS baselines, independent of application and deployment inputs."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from appliance import build as appliance
from scripts import ci_base_cache as archives
from scripts import fetch_ubuntu

SCHEMA = 1
KIND = "photo-wall-installed-os-base"
MAX_MANIFEST_BYTES = 4 * 1024**2
DEFINITION_FILES = (
    "appliance/os_definition.json", "appliance/os_packages.py",
    "appliance/Dockerfile.builder", "appliance/build-tools.txt",
    "scripts/os_base.py", "scripts/ci_base_cache.py", "scripts/fetch_ubuntu.py",
)
# These symbols own OS extraction and installation. Hash their ASTs rather than
# unrelated final-image/application assembly in the same legacy module.
OS_SYMBOLS = (
    "BuildError", "outside_git", "canonical", "checked_file", "inventory", "run",
    "Partition", "mbr", "read_mbr", "decompress_base", "extract_base", "_root",
    "in_root", "install_runtime_packages", "MAX_RAW_BYTES", "MIB", "SECTOR",
)
FORBIDDEN_ROOTS = (*archives.FORBIDDEN_ROOTS, "usr/share/photo-wall", "home/ubuntu")
BUILDER_ID = re.compile(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}\Z")
REQUIRED_EVIDENCE = (
    "base-packages.tsv", "packages.tsv", "package-state.txt", "deb-hashes.json",
    "tool-binaries.json", "verified-input.json", "SHA256SUMS", "SHA256SUMS.gpg",
    "ubuntu-cdimage.asc", "apt-download-plan.txt", "os-sanitization.json",
)
# Public python3-twisted 24.3.0-1ubuntu0.1 test data from the pinned Ubuntu
# image. Remove only these exact bytes, never exempt paths from secret scanning.
PACKAGE_TEST_FIXTURES = {
    "usr/lib/python3/dist-packages/twisted/test/key.pem.no_trailing_newline":
        "380626aa2b5e4a799153f2c8faf6f38f080765fe0b2ab59c7865bf6167f26604",
}


class BaseError(ValueError):
    """A baseline is missing, incompatible, corrupt, or unsafe; never a cache miss."""


def definition(repository: Path) -> dict:
    """Read the complete OS recipe identity from this tree without importing it."""
    repository = Path(repository).resolve(strict=True)
    config = json.loads(archives._regular(repository / "appliance/os_definition.json", 64 * 1024))
    if (not isinstance(config, dict) or config.get("schema") != 1
            or config.get("architecture") != "arm64"
            or not isinstance(config.get("runtime_packages"), list)
            or not config["runtime_packages"]
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name)
                   for name in config["runtime_packages"])):
        raise BaseError("os_definition_invalid")
    files = {name: archives._sha256(repository / name) for name in DEFINITION_FILES}
    source = archives._regular(repository / "appliance/build.py", 4 * 1024**2).decode()
    nodes = {}
    for node in ast.parse(source).body:
        name = getattr(node, "name", None)
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
        if name in OS_SYMBOLS:
            nodes[name] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    if set(nodes) != set(OS_SYMBOLS):
        raise BaseError("os_recipe_incomplete")
    return {"schema": SCHEMA, "inputs": config, "files": files, "recipe": nodes}


def definition_id(repository: Path) -> str:
    return hashlib.sha256(appliance.canonical(definition(repository))).hexdigest()


def _identity(expected: dict) -> str:
    return hashlib.sha256(appliance.canonical(expected)).hexdigest()


def _builder(value: str) -> None:
    if not isinstance(value, str) or not BUILDER_ID.fullmatch(value):
        raise BaseError("os_base_builder_not_immutable")


def _evidence(root: Path, evidence: Path, expected: dict) -> dict:
    for name in REQUIRED_EVIDENCE:
        path = evidence / name
        if path.is_symlink() or not path.is_file():
            raise BaseError("os_base_evidence_missing")
    _validate_sanitization(root, evidence)
    if (evidence / "package-state.txt").stat().st_size:
        raise BaseError("os_base_package_incomplete")
    debs = appliance.inventory(evidence / "debs", maximum_bytes=8 * 1024**3)
    if json.loads(archives._regular(evidence / "deb-hashes.json", 4 * 1024**2)) != debs:
        raise BaseError("os_base_deb_inventory_mismatch")
    apt_lists = appliance.inventory(evidence / "apt-lists", maximum_bytes=2 * 1024**3)
    if (not any(name.endswith("_InRelease") for name in apt_lists)
            or not any("_Packages" in name for name in apt_lists)):
        raise BaseError("os_base_authenticated_indexes_missing")
    upstream = json.loads(archives._regular(evidence / "verified-input.json", 64 * 1024))
    if (upstream.get("sha256") != expected["inputs"]["base_sha256"]
            or upstream.get("size") != expected["inputs"]["base_bytes"]):
        raise BaseError("os_base_upstream_evidence_mismatch")
    packages = archives._regular(evidence / "packages.tsv", 4 * 1024**2).decode()
    package_names = {line.split("\t")[0] for line in packages.splitlines()}
    if not set(expected["inputs"]["runtime_packages"]) <= package_names:
        raise BaseError("os_base_native_packages_missing")
    boot = appliance.inventory(root / "boot/firmware", maximum_files=10_000,
                               maximum_bytes=2 * 1024**3)
    if not boot:
        raise BaseError("os_base_boot_missing")
    return {"files": appliance.inventory(evidence, maximum_bytes=8 * 1024**3),
            "boot_files": boot}


def publish(root: Path, evidence: Path, destination: Path, expected: dict, *,
            builder_image: str) -> dict:
    """Publish a complete unconfigured installed baseline to a new directory."""
    _builder(builder_image)
    root, evidence, destination = (Path(p).absolute() for p in (root, evidence, destination))
    appliance.outside_git(destination)
    if destination.exists() or destination.is_symlink():
        raise BaseError("os_base_destination_exists")
    if any(p == destination or p.resolve() in destination.resolve().parents for p in (root, evidence)):
        raise BaseError("os_base_destination_inside_input")
    provenance = _evidence(root, evidence, expected)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".os-base-publish-", dir=destination.parent))
    try:
        root_record = archives.create_archive(root, temporary / "root.tar",
                                             forbidden_roots=FORBIDDEN_ROOTS, reject_private=True)
        evidence_record = archives.create_archive(evidence, temporary / "evidence.tar",
                                                 forbidden_roots=(), reject_private=True)
        manifest = {"schema": SCHEMA, "kind": KIND, "definition": expected,
                    "definition_id": _identity(expected), "builder_image": builder_image,
                    "root": {"name": "root.tar", **root_record},
                    "evidence": {"name": "evidence.tar", **evidence_record},
                    "provenance": provenance}
        payload = appliance.canonical(manifest)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise BaseError("os_base_manifest_oversized")
        (temporary / "manifest.json").write_bytes(payload)
        # This bundle contains public build inputs only and is consumed outside
        # the root-owned builder container by the registry carrier publisher.
        for path in temporary.iterdir():
            path.chmod(0o644)
        temporary.chmod(0o755)
        os.replace(temporary, destination)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify(bundle: Path, expected: dict) -> dict:
    """Check identity, both content hashes and archive safety without extraction."""
    bundle = Path(bundle)
    if bundle.is_symlink() or not bundle.is_dir():
        raise BaseError("os_base_missing")
    try:
        manifest = json.loads(archives._regular(bundle / "manifest.json", MAX_MANIFEST_BYTES))
        if (not isinstance(manifest, dict)
                or set(manifest) != {"schema", "kind", "definition", "definition_id", "builder_image",
                                     "root", "evidence", "provenance"}
                or type(manifest["schema"]) is not int or manifest["schema"] != SCHEMA
                or manifest["kind"] != KIND or manifest["definition"] != expected
                or manifest["definition_id"] != _identity(expected)):
            raise BaseError("os_base_definition_mismatch")
        _builder(manifest["builder_image"])
        for field in ("root", "evidence"):
            record = manifest[field]
            if (not isinstance(record, dict) or set(record) != {"name", "sha256", "size"}
                    or record["name"] != field + ".tar"):
                raise BaseError("os_base_manifest_invalid")
            archive = bundle / record["name"]
            if archives._checked_archive(archive) != {key: record[key] for key in ("sha256", "size")}:
                raise BaseError("os_base_archive_identity")
            archives._validate_archive(archive, forbidden_roots=FORBIDDEN_ROOTS if field == "root" else (),
                                       reject_private=True)
        return manifest
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        if isinstance(error, BaseError):
            raise
        raise BaseError("os_base_invalid") from error


def restore(bundle: Path, destination: Path, evidence_destination: Path, expected: dict) -> dict:
    """Restore the exact selected baseline; missing/corrupt inputs stop the build."""
    manifest = verify(bundle, expected)
    destinations = [Path(destination).absolute(), Path(evidence_destination).absolute()]
    if (destinations[0] == destinations[1] or destinations[0] in destinations[1].parents
            or destinations[1] in destinations[0].parents):
        raise BaseError("os_base_destinations_overlap")
    for path in destinations:
        appliance.outside_git(path)
        if path.exists() or path.is_symlink():
            raise BaseError("os_base_destination_exists")
    temporary = []
    published = []
    try:
        for path, field in zip(destinations, ("root", "evidence"), strict=True):
            path.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".os-base-restore-", dir=path.parent))
            temporary.append(stage)
            archives.extract_archive(Path(bundle) / manifest[field]["name"], stage)
        if _evidence(temporary[0], temporary[1], expected) != manifest["provenance"]:
            raise BaseError("os_base_provenance_mismatch")
        for stage, path in zip(temporary, destinations, strict=True):
            os.replace(stage, path)
            published.append(path)
        return manifest
    except BaseException:
        for path in (*temporary, *published):
            shutil.rmtree(path, ignore_errors=True)
        raise


def _package_fixture_path(root: Path, relative: str) -> Path | None:
    """Resolve the declared fixture without traversing symlinked ancestors."""
    path = root
    if path.is_symlink() or not path.is_dir():
        raise BaseError("os_base_fixture_unsafe_path")
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return None
        expected_type = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
        if not expected_type(mode):
            raise BaseError("os_base_fixture_unsafe_path")
    return path


def _validate_sanitization(root: Path, evidence: Path) -> None:
    report = json.loads(archives._regular(evidence / "os-sanitization.json", 64 * 1024))
    if (not isinstance(report, dict)
            or set(report) != {"schema", "removed_package_test_fixtures"}
            or type(report["schema"]) is not int or report["schema"] != 1
            or not isinstance(report["removed_package_test_fixtures"], list)):
        raise BaseError("os_base_sanitization_invalid")
    seen = set()
    for item in report["removed_package_test_fixtures"]:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or not isinstance(item["path"], str) or item["path"] in seen
                or item["path"] not in PACKAGE_TEST_FIXTURES
                or item["sha256"] != PACKAGE_TEST_FIXTURES[item["path"]]):
            raise BaseError("os_base_sanitization_invalid")
        seen.add(item["path"])
    for relative in PACKAGE_TEST_FIXTURES:
        if _package_fixture_path(root, relative) is not None:
            raise BaseError("os_base_package_fixture_remaining")


def _sanitize_host_state(root: Path) -> list[dict[str, str]]:
    """Discard machine identities and exact public test fixtures before retention."""
    removed = []
    for relative, expected_hash in PACKAGE_TEST_FIXTURES.items():
        path = _package_fixture_path(root, relative)
        if path is None:
            continue
        if hashlib.sha256(archives._regular(path, 64 * 1024)).hexdigest() != expected_hash:
            raise BaseError("os_base_package_fixture_changed")
        path.unlink()
        removed.append({"path": relative, "sha256": expected_hash})
    for directory in ("etc/ssl/private", "root/.ssh", "root/.gnupg", "home/ubuntu"):
        path = root / directory
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    for path in (root / "etc/ssh").glob("ssh_host_*"):
        path.unlink()
    for relative in ("etc/machine-id", "var/lib/dbus/machine-id", "var/lib/systemd/random-seed",
                     "var/lib/urandom/random-seed", "etc/resolv.conf"):
        path = root / relative
        path.unlink(missing_ok=True)
    (root / "etc/machine-id").write_text("")
    (root / "etc/resolv.conf").symlink_to("/run/systemd/resolve/stub-resolv.conf")
    return removed


def build(repository: Path, output: Path, *, builder_image: str) -> dict:
    """Fetch Ubuntu, install native packages and publish before any app configuration."""
    repository, output = Path(repository).resolve(strict=True), Path(output).absolute()
    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise BaseError("os_base_linux_arm64_required")
    appliance.outside_git(output)
    _builder(builder_image)
    if output.exists() or output.is_symlink():
        raise BaseError("os_base_destination_exists")
    expected = definition(repository)
    if (expected["inputs"]["base_sha256"] != fetch_ubuntu.DIGEST
            or expected["inputs"]["base_sha256"] != appliance.BASE_SHA256
            or expected["inputs"]["base_bytes"] != appliance.BASE_BYTES
            or expected["inputs"]["snapshot"] != appliance.SNAPSHOT
            or tuple(expected["inputs"]["runtime_packages"]) != appliance.RUNTIME_PACKAGES):
        raise BaseError("os_base_upstream_pin_mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".os-base-build-", dir=output.parent) as scratch:
        workspace = Path(scratch)
        source, root, evidence = workspace / "input", workspace / "root", workspace / "evidence"
        evidence.mkdir()
        try:
            appliance.run([sys.executable, str(repository / "scripts/fetch_ubuntu.py"), str(source)],
                          timeout=3600, log=evidence / "fetch-ubuntu.log")
            for name in ("verified-input.json", "SHA256SUMS", "SHA256SUMS.gpg", "ubuntu-cdimage.asc"):
                shutil.copyfile(source / name, evidence / name)
            raw = workspace / "base.img"
            appliance.decompress_base(source / fetch_ubuntu.IMAGE, raw)
            shutil.rmtree(source)
            appliance.run([sys.executable, "-m", "appliance.build", "extract", str(raw), str(root)],
                          timeout=900, log=evidence / "extract.log",
                          env=dict(os.environ, PYTHONPATH=str(repository)))
            raw.unlink()
            appliance.install_runtime_packages(root, evidence)
            removed = _sanitize_host_state(root)
            (evidence / "os-sanitization.json").write_bytes(appliance.canonical({
                "schema": 1, "removed_package_test_fixtures": removed}))
            return publish(root, evidence, output, expected, builder_image=builder_image)
        finally:
            diagnostics = output.parent / "diagnostics"
            diagnostics.mkdir(mode=0o755, exist_ok=True)
            for name in ("fetch-ubuntu.log", "extract.log", "apt-update.log", "apt-purge.log",
                         "apt-download.log", "apt-install.log", "os-sanitization.json"):
                path = evidence / name
                if path.is_file() and not path.is_symlink():
                    data = path.read_bytes()
                    if len(data) > 4 * appliance.MIB:
                        raise BaseError("os_base_diagnostic_limit")
                    target = diagnostics / ("os-base-" + name)
                    target.write_bytes(data)
                    target.chmod(0o644)



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    identity = commands.add_parser("identity")
    identity.add_argument("--repository", type=Path, required=True)
    prepare = commands.add_parser("build")
    prepare.add_argument("--repository", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--builder-image", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--repository", type=Path, required=True)
    check.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "identity":
        result = {"definition_id": definition_id(args.repository)}
    elif args.command == "verify":
        result = verify(args.bundle, definition(args.repository))
    else:
        result = build(args.repository, args.output, builder_image=args.builder_image)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
