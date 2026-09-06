"""Build one signed appliance image and its generic-VM boot companion in CI.

This is orchestration only. The bounded package, Ubuntu input, appliance and
generic-initramfs builders own their respective formats and validations. The
deployment directory contains disposable fixture private keys for the same CI
job and must never be uploaded as an artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Direct-file invocation is supported for local inspection as well as the CI
# module invocation.  Keep repository imports rooted at this checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance import build as appliance
from scripts import build_player, build_vm_initrd, fetch_ubuntu

MIN_FREE_BYTES = 8 * 1024**3


def _new_directory(path: Path, *, label: str) -> Path:
    path = path.absolute()
    appliance.outside_git(path)
    if path.exists() or path.is_symlink():
        raise appliance.BuildError(f"{label}_exists")
    path.mkdir(mode=0o700, parents=True)
    return path


def _openssl(*argv: str) -> None:
    appliance.run(["openssl", *argv], timeout=30)


def _fixture_deployment(destination: Path) -> tuple[Path, Path]:
    """Create public config plus a disposable TLS server fixture."""
    destination = _new_directory(destination, label="deployment")
    public, private = destination / "public", destination / "private"
    public.mkdir(mode=0o700)
    private.mkdir(mode=0o700)
    signing_key = private / "release-signing.key"
    ca_key, ca_cert = private / "ca.key.pem", public / "ca.pem"
    server_key, server_csr, server_cert = private / "server.key.pem", private / "server.csr", private / "server.pem"
    release_pub = public / "release.pub.pem"
    _openssl("genpkey", "-algorithm", "ED25519", "-out", str(signing_key))
    _openssl("pkey", "-in", str(signing_key), "-pubout", "-out", str(release_pub))
    _openssl("genrsa", "-out", str(ca_key), "2048")
    _openssl("req", "-x509", "-new", "-key", str(ca_key), "-sha256", "-days", "1",
             "-subj", "/CN=Photo Wall CI CA", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-out", str(ca_cert))
    _openssl("genrsa", "-out", str(server_key), "2048")
    _openssl("req", "-new", "-key", str(server_key), "-subj", "/CN=photo-wall.test", "-out", str(server_csr))
    extensions = destination / "server.ext"
    serial = destination / "ca.srl"
    extensions.write_text("subjectAltName=DNS:photo-wall.test\n")
    _openssl("x509", "-req", "-in", str(server_csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
             "-CAcreateserial", "-CAserial", str(serial), "-days", "1", "-sha256",
             "-extfile", str(extensions), "-out", str(server_cert))
    extensions.unlink()
    serial.unlink(missing_ok=True)
    ca_key.unlink()
    server_csr.unlink()
    (public / "bootstrap.json").write_bytes(appliance.canonical({
        "schema": 1, "release_origin": "https://photo-wall.test", "time_server": "photo-wall.test"}))
    (public / "public.json").write_bytes(appliance.canonical({
        "schema": 1, "central_origin": "https://photo-wall.test",
        "state_dir": "/var/lib/photo-wall/player", "ca_file": "/etc/photo-wall/ca.pem"}))
    for path in (signing_key, server_key):
        path.chmod(0o600)
    for path in (ca_cert, release_pub, public / "bootstrap.json", public / "public.json"):
        path.chmod(0o600)
    return destination, signing_key


def _record(path: Path, maximum: int) -> dict:
    return appliance.checked_file(path, maximum)


def _preflight_space(path: Path) -> int:
    free = shutil.disk_usage(path).free
    if free < MIN_FREE_BYTES:
        raise appliance.BuildError("disk_space")
    return free


def build(repository: Path, revision: str, output: Path, *, deployment: Path | None = None,
          base_cache: Path | None = None, builder_image: str | None = None,
          central_image: str | None = None) -> dict:
    if not isinstance(revision, str) or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise appliance.BuildError("revision_invalid")
    repository = repository.resolve(strict=True)
    output = output.absolute()
    appliance.outside_git(output)
    if output.exists() or output.is_symlink():
        raise appliance.BuildError("output_exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    free_before = _preflight_space(output.parent)
    deployment = deployment or output.parent / (".photo-wall-ci-deployment-" + revision[:12])
    deployment, signing_key = _fixture_deployment(deployment)
    temporary = Path(tempfile.mkdtemp(prefix=".photo-wall-ci-work-", dir=output.parent))
    appliance.outside_git(temporary)
    keep_cache = base_cache is not None
    try:
        input_dir = (base_cache or temporary / "input").absolute()
        input_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        appliance.run([sys.executable, str(repository / "scripts/fetch_ubuntu.py"), str(input_dir)], timeout=3600)
        compressed = input_dir / fetch_ubuntu.IMAGE
        base_image = temporary / "base.img"
        appliance.decompress_base(compressed, base_image)
        if not keep_cache:
            shutil.rmtree(input_dir)
        root = temporary / "root"
        appliance.run([sys.executable, "-m", "appliance.build", "extract",
                       str(base_image), str(root)], timeout=900,
                      env=dict(os.environ, PYTHONPATH=str(repository)))
        base_image.unlink()
        evidence = temporary / "package-evidence"
        appliance.install_runtime_packages(root, evidence)
        player = temporary / "player"
        player_inventory = build_player.build(repository, revision, player)
        source = temporary / "source"
        appliance.export_source(repository, source, revision)
        bundle = temporary / "bundle"
        appliance.prepare(root, source, player, deployment / "public", evidence, bundle)
        shutil.rmtree(root)
        shutil.rmtree(player)
        shutil.rmtree(source)
        release = json.loads((bundle / "release.json").read_text())
        signature = temporary / "release.sig"
        appliance.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(signing_key),
                       "-in", str(bundle / "release.json"), "-out", str(signature)], timeout=30)
        if _record(signature, 64)["size"] != 64:
            raise appliance.BuildError("release_signature_size")
        final = output
        final_report = appliance.finalize(bundle, signature, final, trusted_public=deployment / "public")
        signing_key.unlink(missing_ok=True)
        generic_dir = output / "generic-boot"
        pi_initrd = output / "pxe/initrd.img"
        kernels = sorted(path for path in Path("/boot").glob("vmlinuz-*-generic") if path.is_file())
        if not kernels:
            raise appliance.BuildError("generic_kernel_missing")
        generic_kernel = kernels[-1]
        release_name = generic_kernel.name.removeprefix("vmlinuz-")
        generic_modules = Path("/lib/modules") / release_name
        if not generic_modules.is_dir() or generic_modules.is_symlink():
            raise appliance.BuildError("generic_modules_missing")
        initrd_record = _record(pi_initrd, build_vm_initrd.MAX_INITRD_BYTES)
        generic_manifest = build_vm_initrd.build(
            pi_initrd, generic_kernel, generic_modules, generic_dir,
            expected_size=initrd_record["size"], expected_sha256=initrd_record["sha256"])
        image_path = output / final_report["image"]
        manifest = {
            "schema": 1,
            "kind": "ci-appliance-image",
            "source_commit": revision,
            "builder_image": builder_image,
            "central_image": central_image,
            "disk": {"path": str(image_path), **_record(image_path, 16 * 1024**3)},
            # These three paths intentionally remain absolute: the VM harness
            # runs on the same CI host before the output directory is uploaded.
            "bundle": str(output / "pxe/appliance"),
            "generic_boot": str(generic_dir),
            "deployment": str(deployment),
            "identities": {"release_id": hashlib.sha256((output / "pxe/appliance/release.json").read_bytes()).hexdigest(),
                           "rootfs_sha256": release["rootfs_sha256"],
                           "configuration_sha256": release["configuration_sha256"],
                           "boot_abi": release["boot_abi"],
                           "generic_initrd_sha256": generic_manifest["outputs"]["initrd"]["sha256"],
                           "generic_kernel_sha256": generic_manifest["outputs"]["kernel"]["sha256"]},
            "inputs": {"player": player_inventory, "builder": "scripts/build_ci_image.py"},
            "tool_versions": {"python": sys.version.split()[0],
                               "packaging": build_player.packaging.__version__},
            "disk_preflight": {"free_bytes_before_build": free_before,
                               "minimum_free_bytes": MIN_FREE_BYTES},
            "qualified": {"image_built": True, "generic_vm_boot": False, "physical_pi": False},
        }
        (output / "ci-image.json").write_bytes(appliance.canonical(manifest))
        return manifest
    finally:
        signing_key.unlink(missing_ok=True)
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"), required=False)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--deployment-dir", type=Path)
    parser.add_argument("--base-cache", type=Path)
    parser.add_argument("--builder-image")
    parser.add_argument("--central-image")
    args = parser.parse_args()
    if not args.revision:
        parser.error("--revision or GITHUB_SHA is required")
    try:
        result = build(args.repository, args.revision, args.output_dir,
                       deployment=args.deployment_dir, base_cache=args.base_cache,
                       builder_image=args.builder_image, central_image=args.central_image)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"CI appliance build failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
