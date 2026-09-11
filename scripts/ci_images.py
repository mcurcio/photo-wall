"""Select and retain definition-scoped CI images; caches are never authoritative.

Registry tags are lookup keys only. Every reuse resolves a tag once and pulls its
immutable digest. A missing artifact permits preparation only after definition
changes or an explicit request, and transport/authentication errors fail closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path

from appliance import build as appliance

BUILDER_FILES = ("appliance/Dockerfile.builder", "appliance/build-tools.txt")
DIGEST = r"sha256:[a-f0-9]{64}"


class ImageError(ValueError):
    pass


class CommandError(ImageError):
    pass


def run(command: list[str], *, timeout: int = 600) -> bytes:
    """Reuse the image tooling's finite time/output limits for registry operations."""
    with tempfile.TemporaryDirectory(prefix="pw-registry-") as temporary:
        log = Path(temporary) / "tool.log"
        try:
            return appliance.run(command, timeout=timeout, log=log)
        except appliance.BuildError as error:
            detail = log.read_text(errors="replace") if log.exists() else ""
            raise CommandError(f"{error}: {detail[-4000:]}") from error


def builder_id(repository: Path) -> str:
    digest = hashlib.sha256(b"photo-wall-builder-v1\0")
    for name in BUILDER_FILES:
        content = (repository / name).read_bytes()
        digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def identities(repository: Path) -> dict[str, str]:
    from scripts import os_base
    return {"builder": builder_id(repository), "base": os_base.definition_id(repository)}


def previous_identities(repository: Path, revision: str) -> dict[str, str] | None:
    """Read old definitions as data; never execute code from the comparison revision."""
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ImageError("comparison must be a full Git commit")
    if revision == "0" * 40:
        return None
    # git archive writes directly to a bounded temporary tree, not command output.
    with tempfile.TemporaryDirectory(prefix="pw-definition-") as temporary:
        root = Path(temporary)
        archive = root / "source.tar"
        run(["git", "-C", str(repository), "archive", "--format=tar",
             "--output", str(archive), revision], timeout=60)
        if archive.stat().st_size > 128 * 1024**2:
            raise ImageError("comparison source archive too large")
        checkout = root / "source"
        checkout.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(checkout, filter="data")
        if not (checkout / "scripts/os_base.py").is_file():
            return None  # First introduction of the explicit OS definition.
        # The definition implementation is itself an input. If its schema or
        # symbol inventory changed, do not ask the new parser to interpret an
        # old tree that cannot satisfy its new contract.
        if ((checkout / "scripts/os_base.py").read_bytes()
                != (repository / "scripts/os_base.py").read_bytes()):
            try:
                previous_builder = builder_id(checkout)
            except FileNotFoundError:
                previous_builder = ""
            return {"builder": previous_builder, "base": ""}
        return identities(checkout)


def qualification_required(repository: Path, compare: str) -> bool:
    """Skip expensive builds only for an explicit, committed documentation diff."""
    if not re.fullmatch(r"[a-f0-9]{40}", compare):
        raise ImageError("qualification comparison must be a full Git commit")
    if compare == "0" * 40:
        return True
    changed = run(["git", "-C", str(repository), "diff", "--name-only", "-z",
                   compare, "HEAD", "--"], timeout=60).split(b"\0")
    names = [name.decode("utf-8", errors="replace") for name in changed if name]
    root_docs = {"README.md", "AGENTS.md", "CONTRIBUTING.md"}
    return not names or not all(name in root_docs or (name.startswith("docs/")
                                                     and name.endswith(".md")) for name in names)


def plan(repository: Path, compare: str, force: bool = False,
         qualification_compare: str | None = None) -> dict[str, str]:
    current = identities(repository)
    previous = previous_identities(repository, compare)
    return {
        "builder_key": current["builder"], "base_key": current["base"],
        "qualify": str(force or qualification_required(
            repository, qualification_compare or compare)).lower(),
        "prepare_builder": str(force or previous is None
                               or previous["builder"] != current["builder"]).lower(),
        "prepare_base": str(force or previous is None
                            or previous["base"] != current["base"]).lower(),
    }


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


def prepare_builder(repository: Path, ref: str, *, allow_build: bool,
                    publish: bool) -> dict[str, str]:
    selected = select(ref, allow_build=allow_build)
    if selected is not None:
        return {"imageid": pull(selected), "image": selected, "built": "false"}
    run(["docker", "buildx", "build", "--builder", "default", "--load",
         "--platform", "linux/arm64", "--label",
         "org.opencontainers.image.source=https://github.com/" + "/".join(ref.split("/")[1:3]),
         "--file",
         str(repository / "appliance/Dockerfile.builder"), "--tag", ref,
         str(repository)], timeout=3600)
    config = image_id(ref)
    if publish:
        run(["docker", "push", ref], timeout=1200)
        selected = resolve(ref)
        if selected is None:
            raise ImageError("published builder disappeared")
        if pull(selected) != config:
            raise ImageError("published builder differs from local candidate")
    return {"imageid": config, "image": selected or config, "built": "true"}


def restore_base(ref: str, output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise ImageError("base output must be new")
    pull(ref)
    container = run(["docker", "create", ref, "/unused"], timeout=60).decode().strip()
    if re.fullmatch(r"[a-f0-9]{64}", container) is None:
        raise ImageError("unexpected carrier container identity")
    try:
        run(["docker", "cp", f"{container}:/os-base", str(output)], timeout=1200)
    finally:
        run(["docker", "rm", container], timeout=60)
        # The validated inner archive is the input from here on. Release its
        # transport layer before allocating the appliance's working filesystem.
        run(["docker", "image", "rm", ref], timeout=120)


def publish_candidate(repository: Path, ref: str, bundle: Path) -> None:
    """Retain a freshly built OS base under an explicitly UNQUALIFIED tag.

    Unlike publish_base, this is never gated on a green boot and it may
    overwrite an existing candidate: base_key is the definition's content
    identity (scripts/os_base.py:definition_id), so any build tagged with a
    given key is safe and reproducible to reuse in place of another. The
    authoritative definition-<key> tag remains published only by
    publish_base, after boot qualification, exactly as before.
    """
    run(["docker", "buildx", "build", "--builder", "default", "--load",
         "--platform", "linux/arm64", "--label",
         "org.opencontainers.image.source=https://github.com/" + "/".join(ref.split("/")[1:3]),
         "--file",
         str(repository / "appliance/Dockerfile.os-base"), "--tag", ref,
         str(bundle)], timeout=1200)
    run(["docker", "push", ref], timeout=1200)


def prepare_base(repository: Path, ref: str, candidate_ref: str, output: Path, builder: str,
                 builder_config: str, *, allow_build: bool, publish: bool) -> dict[str, str]:
    from scripts import os_base
    selected = select(ref, allow_build=allow_build)
    if selected is not None:
        restore_base(selected, output)
    else:
        # base_key already permitted a build (select only returns None when
        # allow_build is set); check for a prior UNQUALIFIED build of this
        # exact key before repeating the ~17-minute fetch/extract/install.
        candidate = resolve(candidate_ref)
        if candidate is not None:
            restore_base(candidate, output)
        else:
            # Only this container can access APT; ordinary assembly uses --network none.
            run(["docker", "run", "--rm", "--platform", "linux/arm64",
                 "--volume", f"{repository}:{repository}:ro",
                 "--volume", f"{output.parent}:{output.parent}",
                 "--workdir", str(repository), "--env", "PYTHONDONTWRITEBYTECODE=1",
                 builder_config, "python3.12", "-m", "scripts.os_base", "build",
                 "--repository", str(repository), "--output", str(output),
                 "--builder-image", builder], timeout=5400)
            if publish:
                publish_candidate(repository, candidate_ref, output)
    # Final assembly independently verifies the bundle before extracting it.
    manifest = os_base.verify(output, os_base.definition(repository))
    if manifest["builder_image"] != builder:
        raise ImageError("OS base was prepared with a different immutable builder")
    return {"image": selected or "", "built": str(selected is None).lower()}


def publish_base(repository: Path, ref: str, bundle: Path) -> dict[str, str]:
    # Called only after the candidate's full assembly and boot qualification pass.
    # Definition-scoped job concurrency prevents cooperating writers replacing it.
    if resolve(ref) is not None:
        raise ImageError("refusing to overwrite an existing definition artifact")
    run(["docker", "buildx", "build", "--builder", "default", "--load",
         "--platform", "linux/arm64", "--label",
         "org.opencontainers.image.source=https://github.com/" + "/".join(ref.split("/")[1:3]),
         "--file",
         str(repository / "appliance/Dockerfile.os-base"), "--tag", ref,
         str(bundle)], timeout=1200)
    run(["docker", "push", ref], timeout=1200)
    selected = resolve(ref)
    if selected is None:
        raise ImageError("published OS base disappeared")
    return {"image": selected}


def emit(values: dict[str, str]) -> None:
    print(json.dumps(values, sort_keys=True))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as stream:
            for key, value in values.items():
                if "\n" in key + value:
                    raise ImageError("invalid workflow output")
                stream.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "builder", "base", "publish-base"))
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--compare")
    parser.add_argument("--qualification-compare")
    parser.add_argument("--namespace")
    parser.add_argument("--key")
    parser.add_argument("--allow-build", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--builder-image")
    parser.add_argument("--builder-config")
    args = parser.parse_args()
    try:
        repository = args.repository.resolve(strict=True)
        if args.operation == "plan":
            if not args.compare:
                raise ImageError("comparison commit required")
            result = plan(repository, args.compare, args.force, args.qualification_compare)
        else:
            kind = "builder" if args.operation == "builder" else "base"
            ref = reference(args.namespace or "", kind, args.key or "")
            if args.operation == "builder":
                result = prepare_builder(repository, ref, allow_build=args.allow_build,
                                         publish=args.publish)
            else:
                if args.output is None or not args.output.is_absolute():
                    raise ImageError("absolute external bundle output required")
                appliance.outside_git(args.output)
                if args.operation == "publish-base":
                    result = publish_base(repository, ref, args.output)
                else:
                    if not args.builder_image or not args.builder_config:
                        raise ImageError("immutable builder identities required")
                    candidate_ref = reference(args.namespace or "", kind, args.key or "",
                                              stage="candidate")
                    result = prepare_base(repository, ref, candidate_ref, args.output,
                                          args.builder_image, args.builder_config,
                                          allow_build=args.allow_build, publish=args.publish)
        emit(result)
    except (ValueError, OSError) as error:
        parser.exit(1, f"CI image selection failed: {error}\n")


if __name__ == "__main__":
    main()
