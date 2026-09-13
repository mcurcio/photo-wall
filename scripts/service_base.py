"""Retain the media worker's native dependencies independently of application builds."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

MARKER = b"# END MEDIA OS DEFINITION\n"
ARCHITECTURES = ("amd64", "arm64")
MIB = 1024**2
DIGEST = r"sha256:[a-f0-9]{64}"


class ImageError(ValueError):
    pass


class CommandError(ImageError):
    pass


def run(command: list[str], *, timeout: int = 600) -> bytes:
    """Run a registry tool with a finite deadline and bounded, persisted output.

    Failures raise CommandError carrying the reason and the trailing log so
    resolve() can distinguish a missing artifact from a registry outage.
    """
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True)
    output, deadline = bytearray(), time.monotonic() + timeout
    reason: str | None = None
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(child.stdout, selectors.EVENT_READ)
            while poll.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    reason = "tool_timeout"
                    break
                for key, _ in poll.select(min(0.2, left)):
                    block = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not block:
                        poll.unregister(key.fileobj)
                    else:
                        output.extend(block)
                        if len(output) > 4 * MIB:
                            reason = "tool_output_limit"
                            break
                if reason is not None:
                    break
            if reason is None:
                try:
                    code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    reason = "tool_timeout"
                else:
                    if code:
                        reason = "tool_failed"
        if reason is not None:
            detail = bytes(output[:4 * MIB]).decode("utf-8", errors="replace")
            raise CommandError(f"{reason}: {detail[-4000:]}")
        return bytes(output)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        child.stdout.close()


def reference(namespace: str, kind: str, identity: str, *, stage: str = "definition") -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*", namespace):
        raise ImageError("expected a lowercase owner/repository registry namespace")
    if kind not in ("builder", "base", "media-system") or not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ImageError("invalid image identity")
    if stage not in ("definition", "candidate"):
        raise ImageError("invalid image stage")
    return f"ghcr.io/{namespace}/appliance-{kind}:{stage}-{identity}"


def resolve(ref: str) -> str | None:
    try:
        result = run(["docker", "buildx", "imagetools", "inspect", ref], timeout=120)
    except CommandError as error:
        # An unavailable registry is not a missing artifact. In particular never
        # interpret authorization failures, 429, or 5xx as permission to rebuild.
        detail = str(error).lower()
        if re.search(r"manifest[_ ]unknown|name_unknown|: not found(?:\s|$)", detail):
            return None
        raise
    match = re.search(rb"^Digest:\s+(sha256:[a-f0-9]{64})\s*$", result, re.MULTILINE)
    if match is None:
        raise ImageError("registry returned no canonical manifest digest")
    return ref.rsplit(":", 1)[0] + "@" + match[1].decode()


def select(ref: str, *, allow_build: bool) -> str | None:
    resolved = resolve(ref)
    if resolved is None and not allow_build:
        raise ImageError(
            f"Required artifact {ref} is missing. Restore it or run workflow_dispatch "
            "with prepare_base=true. Application-only builds cannot prepare OS dependencies."
        )
    return resolved


def pull(ref: str, *, platform: str = "linux/arm64") -> str:
    if platform not in ("linux/amd64", "linux/arm64"):
        raise ImageError("unsupported CI image platform")
    run(["docker", "pull", "--platform", platform, ref], timeout=1200)
    return image_id(ref)


def image_id(ref: str) -> str:
    result = run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], timeout=60)
    value = result.decode().strip()
    if re.fullmatch(DIGEST, value) is None:
        raise ImageError("Docker returned no immutable config identity")
    return value


def emit(values: dict[str, str]) -> None:
    print(json.dumps(values, sort_keys=True))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as stream:
            for key, value in values.items():
                if "\n" in key + value:
                    raise ImageError("invalid workflow output")
                stream.write(f"{key}={value}\n")


def _prefix(recipe: bytes) -> bytes:
    if recipe.count(MARKER) != 1:
        raise ImageError("Dockerfile must contain one media OS definition boundary")
    return recipe.split(MARKER, 1)[0] + MARKER


def _identity(recipe: bytes, architecture: str) -> str:
    if architecture not in ARCHITECTURES:
        raise ImageError("unsupported media OS architecture")
    return hashlib.sha256(b"photo-wall-media-os-v1\0" + architecture.encode()
                          + b"\0" + _prefix(recipe)).hexdigest()


def id(repository: Path, architecture: str) -> str:
    """Identify only the standalone native definition and its target architecture."""
    return _identity((repository / "Dockerfile").read_bytes(), architecture)


def previous_id(repository: Path, revision: str, architecture: str) -> str | None:
    """Read the historical recipe as data without executing historical Python."""
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ImageError("comparison must be a full Git commit")
    if revision == "0" * 40:
        return None
    with tempfile.TemporaryDirectory(prefix="pw-media-definition-") as temporary:
        archive = Path(temporary) / "source.tar"
        run(["git", "-C", str(repository), "archive", "--format=tar",
             "--output", str(archive), revision], timeout=60)
        if archive.stat().st_size > 128 * 1024**2:
            raise ImageError("comparison source archive too large")
        with tarfile.open(archive) as source:
            try:
                member = source.getmember("Dockerfile")
            except KeyError:
                return None
            if not member.isfile() or member.size > 1024**2:
                raise ImageError("invalid historical Dockerfile")
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
    ref = reference(namespace, "media-system", current)
    selected = select(ref, allow_build=force or previous != current)
    platform = "linux/" + architecture
    if selected is not None:
        return {"image": selected, "imageid": pull(selected, platform=platform),
                "built": "false", "key": current}
    if require_published and not publish:
        raise ImageError(
            "The changed media OS definition needs a published base before jobs can share it. "
            "Run the trusted base preparation workflow; this run cannot publish registry images."
        )
    run(["docker", "buildx", "build", "--builder", "default", "--load",
         "--platform", platform, "--target", "media-os", "--label",
         "org.opencontainers.image.source=https://github.com/" + namespace,
         "--file", str(repository / "Dockerfile"), "--tag", ref,
         str(repository)], timeout=3600)
    config = image_id(ref)
    # Qualify native executables and the worker's private default configuration
    # before publication, without granting the candidate any network access.
    run([
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
        if resolve(ref) is not None:
            raise ImageError("refusing to overwrite an existing media OS definition")
        run(["docker", "push", ref], timeout=1200)
        selected = resolve(ref)
        if selected is None:
            raise ImageError("published media OS base disappeared")
        if pull(selected, platform=platform) != config:
            raise ImageError("published media OS base differs from local candidate")
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
        emit(prepare(args.repository.resolve(strict=True), args.namespace,
                     args.architecture, args.compare, force=args.force,
                     publish=args.publish, require_published=args.require_published))
    except (ImageError, OSError, tarfile.TarError) as error:
        parser.exit(1, f"Media OS preparation failed: {error}\n")


if __name__ == "__main__":
    main()
