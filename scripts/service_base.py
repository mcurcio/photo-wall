"""Retain the media worker's native dependencies independently of application builds."""
from __future__ import annotations

import argparse
import hashlib
import re
import tarfile
import tempfile
from pathlib import Path

from scripts import ci_images

MARKER = b"# END MEDIA OS DEFINITION\n"
ARCHITECTURES = ("amd64", "arm64")


def _prefix(recipe: bytes) -> bytes:
    if recipe.count(MARKER) != 1:
        raise ci_images.ImageError("Dockerfile must contain one media OS definition boundary")
    return recipe.split(MARKER, 1)[0] + MARKER


def _identity(recipe: bytes, architecture: str) -> str:
    if architecture not in ARCHITECTURES:
        raise ci_images.ImageError("unsupported media OS architecture")
    return hashlib.sha256(b"photo-wall-media-os-v1\0" + architecture.encode()
                          + b"\0" + _prefix(recipe)).hexdigest()


def id(repository: Path, architecture: str) -> str:
    """Identify only the standalone native definition and its target architecture."""
    return _identity((repository / "Dockerfile").read_bytes(), architecture)


def previous_id(repository: Path, revision: str, architecture: str) -> str | None:
    """Read the historical recipe as data without executing historical Python."""
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ci_images.ImageError("comparison must be a full Git commit")
    if revision == "0" * 40:
        return None
    with tempfile.TemporaryDirectory(prefix="pw-media-definition-") as temporary:
        archive = Path(temporary) / "source.tar"
        ci_images.run(["git", "-C", str(repository), "archive", "--format=tar",
                       "--output", str(archive), revision], timeout=60)
        if archive.stat().st_size > 128 * 1024**2:
            raise ci_images.ImageError("comparison source archive too large")
        with tarfile.open(archive) as source:
            try:
                member = source.getmember("Dockerfile")
            except KeyError:
                return None
            if not member.isfile() or member.size > 1024**2:
                raise ci_images.ImageError("invalid historical Dockerfile")
            stream = source.extractfile(member)
            assert stream is not None
            recipe = stream.read()
    if MARKER not in recipe:
        return None  # The explicit native definition is being introduced.
    return _identity(recipe, architecture)


def prepare(repository: Path, namespace: str, architecture: str, compare: str, *,
            force: bool = False, publish: bool = False,
            require_published: bool = False) -> dict[str, str]:
    current = id(repository, architecture)
    previous = previous_id(repository, compare, architecture)
    ref = ci_images.reference(namespace, "media-system", current)
    selected = ci_images.select(ref, allow_build=force or previous != current)
    platform = "linux/" + architecture
    if selected is not None:
        return {"image": selected, "imageid": ci_images.pull(selected, platform=platform),
                "built": "false", "key": current}
    if require_published and not publish:
        raise ci_images.ImageError(
            "The changed media OS definition needs a published base before jobs can share it. "
            "Run the trusted base preparation workflow; this run cannot publish registry images."
        )
    ci_images.run(["docker", "buildx", "build", "--builder", "default", "--load",
                   "--platform", platform, "--target", "media-os", "--label",
                   "org.opencontainers.image.source=https://github.com/" + namespace,
                   "--file", str(repository / "Dockerfile"), "--tag", ref,
                   str(repository)], timeout=3600)
    config = ci_images.image_id(ref)
    # Qualify native executables and the worker's private default configuration
    # before publication, without granting the candidate any network access.
    ci_images.run([
        "docker", "run", "--rm", "--platform", platform, "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--user", "10001:10001", ref,
        "sh", "-ec", "sha256sum -c /etc/photo-wall/conversion-binaries.sha256 "
        "&& ffmpeg -version >/dev/null && ffprobe -version >/dev/null "
        "&& python -c \"import json, pathlib, stat; "
        "p = pathlib.Path('/etc/photo-wall/private/connections.json'); "
        "assert json.loads(p.read_text()) == {'schema': 1, 'connections': []}; "
        "assert p.stat().st_uid == 10001 and stat.S_IMODE(p.stat().st_mode) == 0o600; "
        "assert pathlib.Path('/etc/photo-wall/packages.tsv').stat().st_size > 0\"",
    ], timeout=120)
    if publish:
        # Workflow concurrency serializes cooperating publishers of this definition.
        if ci_images.resolve(ref) is not None:
            raise ci_images.ImageError("refusing to overwrite an existing media OS definition")
        ci_images.run(["docker", "push", ref], timeout=1200)
        selected = ci_images.resolve(ref)
        if selected is None:
            raise ci_images.ImageError("published media OS base disappeared")
        if ci_images.pull(selected, platform=platform) != config:
            raise ci_images.ImageError("published media OS base differs from local candidate")
    return {"image": selected or ref, "imageid": config, "built": "true", "key": current}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    parser.add_argument("--compare", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--require-published", action="store_true")
    args = parser.parse_args()
    try:
        ci_images.emit(prepare(args.repository.resolve(strict=True), args.namespace,
                               args.architecture, args.compare, force=args.force,
                               publish=args.publish, require_published=args.require_published))
    except (ci_images.ImageError, OSError, tarfile.TarError) as error:
        parser.exit(1, f"Media OS preparation failed: {error}\n")


if __name__ == "__main__":
    main()
