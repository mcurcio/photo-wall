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
import time
from pathlib import Path

# Direct-file invocation is supported for local inspection as well as the CI
# module invocation.  Keep repository imports rooted at this checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance import build as appliance
from scripts import (
    build_player,
    build_rollback_candidate,
    build_vm_initrd,
    ci_apt_cache,
    ci_base_cache,
    fetch_ubuntu,
)

# Include the additional bounded rollback rootfs retained in the private fixture.
MIN_FREE_BYTES = 9 * 1024**3


def phase(name, function, *args, **kwargs):
    """Identify failed public build stages without echoing private inputs."""
    started = time.monotonic()
    grouped = os.environ.get("GITHUB_ACTIONS") == "true"
    if grouped:
        print(f"::group::{name}", flush=True)
    print(json.dumps({"phase": name, "status": "started"}), flush=True)
    try:
        result = function(*args, **kwargs)
    except BaseException:
        print(
            json.dumps(
                {
                    "phase": name,
                    "status": "failed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            ),
            flush=True,
        )
        raise
    else:
        print(
            json.dumps(
                {
                    "phase": name,
                    "status": "passed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            ),
            flush=True,
        )
        return result
    finally:
        if grouped:
            print("::endgroup::", flush=True)


def _new_directory(path: Path, *, label: str) -> Path:
    path = path.absolute()
    appliance.outside_git(path)
    if path.exists() or path.is_symlink():
        raise appliance.BuildError(f"{label}_exists")
    path.mkdir(mode=0o700, parents=True)
    return path


def _openssl(*argv: str) -> None:
    appliance.run(["openssl", *argv], timeout=30)


def _sign_release(signing_key: Path, manifest: Path, signature: Path) -> dict:
    """Create and size-check one raw Ed25519 release signature."""
    appliance.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-rawin",
            "-inkey",
            str(signing_key),
            "-in",
            str(manifest),
            "-out",
            str(signature),
        ],
        timeout=30,
    )
    record = _record(signature, 64)
    if record["size"] != 64:
        raise appliance.BuildError("release_signature_size")
    return record


def _prepare_and_sign_rollback_candidate(
    root: Path, bundle: Path, deployment: Path, signing_key: Path
) -> tuple[Path, dict, dict]:
    """Build the private CI candidate while the configured root is still present."""
    destination = deployment / "rollback-candidate"
    metadata = phase(
        "prepare_rollback_candidate", build_rollback_candidate.prepare, root, bundle, destination
    )
    signature = destination / "release.sig"
    signature_record = phase(
        "sign_rollback_candidate",
        _sign_release,
        signing_key,
        destination / "release.json",
        signature,
    )
    return destination, metadata, signature_record


def _fixture_deployment(destination: Path) -> tuple[Path, Path]:
    """Create public config plus a disposable TLS server fixture."""
    destination = _new_directory(destination, label="deployment")
    try:
        public, private = destination / "public", destination / "private"
        public.mkdir(mode=0o700)
        private.mkdir(mode=0o700)
        signing_key = private / "release-signing.key"
        ca_key, ca_cert = private / "ca.key.pem", public / "ca.pem"
        server_key, server_csr, server_cert = (
            private / "server.key.pem",
            private / "server.csr",
            private / "server.pem",
        )
        release_pub = public / "release.pub.pem"
        phase(
            "fixture_signing_key",
            _openssl,
            "genpkey",
            "-algorithm",
            "ED25519",
            "-out",
            str(signing_key),
        )
        _openssl("pkey", "-in", str(signing_key), "-pubout", "-out", str(release_pub))
        _openssl("genrsa", "-out", str(ca_key), "2048")
        _openssl(
            "req",
            "-x509",
            "-new",
            "-key",
            str(ca_key),
            "-sha256",
            "-days",
            "1",
            "-subj",
            "/CN=Photo Wall CI CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-out",
            str(ca_cert),
        )
        _openssl("genrsa", "-out", str(server_key), "2048")
        _openssl(
            "req",
            "-new",
            "-key",
            str(server_key),
            "-subj",
            "/CN=photo-wall.test",
            "-out",
            str(server_csr),
        )
        extensions = destination / "server.ext"
        serial = destination / "ca.srl"
        extensions.write_text("subjectAltName=DNS:photo-wall.test\n")
        _openssl(
            "x509",
            "-req",
            "-in",
            str(server_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-CAserial",
            str(serial),
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(extensions),
            "-out",
            str(server_cert),
        )
        extensions.unlink()
        serial.unlink(missing_ok=True)
        ca_key.unlink()
        server_csr.unlink()
        (public / "bootstrap.json").write_bytes(
            appliance.canonical(
                {
                    "schema": 1,
                    "release_origin": "https://photo-wall.test",
                    "time_server": "photo-wall.test",
                }
            )
        )
        (public / "public.json").write_bytes(
            appliance.canonical(
                {
                    "schema": 1,
                    "central_origin": "https://photo-wall.test",
                    "cache_dir": "/run/photo-wall/player/cache",
                    "boot_context_file": "/run/photo-wall/boot.json",
                    "ca_file": "/etc/photo-wall/ca.pem",
                }
            )
        )
        for path in (signing_key, server_key):
            path.chmod(0o600)
        for path in (ca_cert, release_pub, public / "bootstrap.json", public / "public.json"):
            path.chmod(0o600)
        return destination, signing_key
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _record(path: Path, maximum: int) -> dict:
    return appliance.checked_file(path, maximum)


def _preflight_space(path: Path) -> int:
    free = shutil.disk_usage(path).free
    if free < MIN_FREE_BYTES:
        raise appliance.BuildError("disk_space")
    return free


def _prepare_base_root(
    temporary: Path,
    repository: Path,
    diagnostics: Path,
    *,
    base_cache: Path | None,
    extracted_base_cache: Path | None,
) -> tuple[Path, dict]:
    """Obtain the pristine base root, optionally restoring its public cache."""
    cache_record = {
        "requested": extracted_base_cache is not None,
        "hit": False,
        "published": False,
        "fingerprint": None,
    }
    expected = None
    root = temporary / "root"
    if extracted_base_cache is not None:
        expected = ci_base_cache.fingerprint(repository)
        cache_record = phase(
            "extracted_base_cache_restore",
            ci_base_cache.restore,
            extracted_base_cache,
            root,
            expected,
        )
        print(
            json.dumps(
                {
                    "phase": "extracted_base_cache",
                    "status": "passed",
                    "hit": cache_record["hit"],
                    "fingerprint": expected,
                    **({"reason": cache_record["reason"]} if "reason" in cache_record else {}),
                }
            ),
            flush=True,
        )
        if cache_record["hit"]:
            return root, cache_record

    input_dir = (base_cache or temporary / "input").absolute()
    input_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    phase(
        "fetch_ubuntu",
        appliance.run,
        [sys.executable, str(repository / "scripts/fetch_ubuntu.py"), str(input_dir)],
        timeout=3600,
        log=diagnostics / "fetch-ubuntu.log",
    )
    compressed = input_dir / fetch_ubuntu.IMAGE
    base_image = temporary / "base.img"
    phase("decompress", appliance.decompress_base, compressed, base_image)
    if base_cache is None:
        shutil.rmtree(input_dir)
    phase(
        "extract",
        appliance.run,
        [sys.executable, "-m", "appliance.build", "extract", str(base_image), str(root)],
        timeout=900,
        env=dict(os.environ, PYTHONPATH=str(repository)),
        log=diagnostics / "extract.log",
    )
    base_image.unlink()

    if expected is not None:
        if extracted_base_cache.exists() or extracted_base_cache.is_symlink():
            # An exact-key cache path should be absent on a miss. Leave a
            # malformed restored path untouched and make this build usable.
            cache_record = {
                "requested": True,
                "hit": False,
                "published": False,
                "fingerprint": expected,
                "reason": "cache_exists",
            }
            print(
                json.dumps(
                    {
                        "phase": "extracted_base_cache_publish",
                        "status": "skipped",
                        "hit": False,
                        "fingerprint": expected,
                        "reason": "cache_exists",
                    }
                ),
                flush=True,
            )
        else:
            try:
                cache_record = phase(
                    "extracted_base_cache_publish",
                    ci_base_cache.publish,
                    root,
                    extracted_base_cache,
                    expected,
                )
            except (ci_base_cache.CacheError, appliance.BuildError, OSError):
                cache_record = {
                    "requested": True,
                    "hit": False,
                    "published": False,
                    "fingerprint": expected,
                    "reason": "cache_publish_failed",
                }
            print(
                json.dumps(
                    {
                        "phase": "extracted_base_cache",
                        "status": "passed",
                        "hit": False,
                        "fingerprint": expected,
                        "published": cache_record["published"],
                    }
                ),
                flush=True,
            )
    return root, cache_record


def build(
    repository: Path,
    revision: str,
    output: Path,
    *,
    deployment: Path | None = None,
    base_cache: Path | None = None,
    builder_image: str | None = None,
    central_image: str | None = None,
    worker_image: str | None = None,
    extracted_base_cache: Path | None = None,
    apt_archive_cache: Path | None = None,
) -> dict:
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise appliance.BuildError("revision_invalid")
    if worker_image is not None and (
        not worker_image.startswith("sha256:")
        or len(worker_image) != 71
        or any(c not in "0123456789abcdef" for c in worker_image[7:])
    ):
        raise appliance.BuildError("worker_image_invalid")
    repository = repository.resolve(strict=True)
    output = output.absolute()
    appliance.outside_git(output)
    if output.exists() or output.is_symlink():
        raise appliance.BuildError("output_exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    diagnostics = output.parent / "diagnostics"
    diagnostics.mkdir(mode=0o700, exist_ok=True)
    free_before = phase("disk_space", _preflight_space, output.parent)
    deployment = deployment or output.parent / (".photo-wall-ci-deployment-" + revision[:12])
    signing_key = temporary = None
    completed = False
    try:
        deployment, signing_key = phase("fixture_deployment", _fixture_deployment, deployment)
        temporary = Path(tempfile.mkdtemp(prefix=".photo-wall-ci-work-", dir=output.parent))
        appliance.outside_git(temporary)
        root, extracted_cache_record = _prepare_base_root(
            temporary,
            repository,
            diagnostics,
            base_cache=base_cache,
            extracted_base_cache=extracted_base_cache,
        )
        evidence = temporary / "package-evidence"
        cache = None if apt_archive_cache is None else ci_apt_cache.AptArchiveCache(apt_archive_cache)
        apt_cache_record = phase(
            "runtime_packages", appliance.install_runtime_packages, root, evidence, cache
        )
        player = temporary / "player"
        player_inventory = phase("player_package", build_player.build, repository, revision, player)
        source = temporary / "source"
        phase("source_export", appliance.export_source, repository, source, revision)
        bundle = temporary / "bundle"
        phase(
            "prepare_image",
            appliance.prepare,
            root,
            source,
            player,
            deployment / "public",
            evidence,
            bundle,
        )
        rollback_candidate, rollback_metadata, _rollback_signature_record = (
            _prepare_and_sign_rollback_candidate(root, bundle, deployment, signing_key)
        )
        shutil.rmtree(root)
        shutil.rmtree(player)
        shutil.rmtree(source)
        release = json.loads((bundle / "release.json").read_text())
        signature = temporary / "release.sig"
        _sign_release(signing_key, bundle / "release.json", signature)
        final = output
        final_report = phase(
            "finalize_image",
            appliance.finalize,
            bundle,
            signature,
            final,
            trusted_public=deployment / "public",
        )
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
        generic_manifest = phase(
            "generic_initramfs",
            build_vm_initrd.build,
            pi_initrd,
            generic_kernel,
            generic_modules,
            generic_dir,
            expected_size=initrd_record["size"],
            expected_sha256=initrd_record["sha256"],
        )
        image_path = output / final_report["image"]
        manifest = {
            "schema": 1,
            "kind": "ci-appliance-image",
            "source_commit": revision,
            "builder_image": builder_image,
            "central_image": central_image,
            "worker_image": worker_image,
            "extracted_base_cache": extracted_cache_record,
            "apt_archive_cache": apt_cache_record,
            "disk": {"path": str(image_path), **_record(image_path, 16 * 1024**3)},
            # These three paths intentionally remain absolute: the VM harness
            # runs on the same CI host before the output directory is uploaded.
            "bundle": str(output / "pxe/appliance"),
            "generic_boot": str(generic_dir),
            "deployment": str(deployment),
            "rollback_candidate": {"path": str(rollback_candidate), "metadata": rollback_metadata},
            "identities": {
                "release_id": hashlib.sha256(
                    (output / "pxe/appliance/release.json").read_bytes()
                ).hexdigest(),
                "rootfs_sha256": release["rootfs_sha256"],
                "configuration_sha256": release["configuration_sha256"],
                "boot_abi": release["boot_abi"],
                "generic_initrd_sha256": generic_manifest["outputs"]["initrd"]["sha256"],
                "generic_kernel_sha256": generic_manifest["outputs"]["kernel"]["sha256"],
            },
            "inputs": {"player": player_inventory, "builder": "scripts/build_ci_image.py"},
            "tool_versions": {
                "python": sys.version.split()[0],
                "packaging": build_player.packaging.__version__,
            },
            "disk_preflight": {
                "free_bytes_before_build": free_before,
                "minimum_free_bytes": MIN_FREE_BYTES,
            },
            "qualified": {"image_built": True, "generic_vm_boot": False, "physical_pi": False},
        }
        (output / "ci-image.json").write_bytes(appliance.canonical(manifest))
        completed = True
        return manifest
    finally:
        try:
            owners = (
                ()
                if temporary is None
                else (temporary / "package-evidence", temporary / "bundle/inventory")
            )
            for owner in owners:
                for name in (
                    "apt-update.log",
                    "apt-purge.log",
                    "apt-download.log",
                    "apt-download-plan.txt",
                    "apt-install.log",
                    "initramfs-build.log",
                    "pip-install.log",
                    "package-state.txt",
                ):
                    path = owner / name
                    if path.is_file() and not path.is_symlink():
                        if path.stat().st_size:
                            appliance.checked_file(path, 4 * 1024**2)
                        shutil.copyfile(path, diagnostics / name)
        finally:
            if signing_key is not None:
                signing_key.unlink(missing_ok=True)
                if not completed:
                    shutil.rmtree(deployment, ignore_errors=True)
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"), required=False)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--deployment-dir", type=Path)
    parser.add_argument("--base-cache", type=Path)
    parser.add_argument("--extracted-base-cache", type=Path)
    parser.add_argument("--apt-archive-cache", type=Path)
    parser.add_argument("--builder-image")
    parser.add_argument("--central-image")
    parser.add_argument("--worker-image")
    args = parser.parse_args()
    if not args.revision:
        parser.error("--revision or GITHUB_SHA is required")
    try:
        result = build(
            args.repository,
            args.revision,
            args.output_dir,
            deployment=args.deployment_dir,
            base_cache=args.base_cache,
            builder_image=args.builder_image,
            central_image=args.central_image,
            worker_image=args.worker_image,
            extracted_base_cache=args.extracted_base_cache,
            apt_archive_cache=args.apt_archive_cache,
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"CI appliance build failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
