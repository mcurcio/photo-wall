#!/usr/bin/env python3
"""Build a sealed environment's squashfs image, deterministically, from its release archive.

The tree squashed is the one `scripts/sealed_archive.stage_archive` produces from the archive, verified
with `verify_root` (the per-file proof), so the image holds exactly the sealed release. mksquashfs runs
in a pinned tool image (`tools_image`): the Debian snapshot pin, not the runner's apt, decides
its version, and SOURCE_DATE_EPOCH (the pin's epoch) fixes the filesystem's and every inode's
time, so two builds of one archive give one image digest. `-all-root` makes every member owned
by root whoever staged the tree, so no step here needs root.

Since E2c the image is the shipped form: scripts/build_node_components.py builds each root's image
twice (two builds must give one digest), ships it, and its digest and size are the release ref's.
The tar is a build intermediate only.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from appliance.apps.environment import IMAGE_SUFFIX, file_sha256
from contracts.app_environment import AppEnvironmentRefV2
from scripts.debian_packages import PIN
from scripts.node_build_inputs import BUILDER_IMAGE, docker_build, validate_builder
from scripts.sealed_archive import stage_archive

__all__ = ["IMAGE_SUFFIX", "SQUASHFS_OPTIONS", "EnvironmentImage", "image_from_archive", "tools_image"]
SQUASHFS_OPTIONS: Final[tuple[str, ...]] = ("-noappend", "-no-progress", "-all-root", "-no-xattrs",
                                            "-comp", "zstd", "-b", "128K")
TREE_MODE: Final = 0o755
# The tool image: the builder image on the pin's snapshot (the same sources and preference as the
# component builders), plus mksquashfs/unsquashfs at the snapshot's version.
TOOLS_DOCKERFILE: Final = """FROM {builder_image}
COPY snapshot.list /etc/apt/sources.list
RUN rm -f /etc/apt/sources.list.d/* && printf 'Package: *\\nPin: origin snapshot.debian.org\\nPin-Priority: 1001\\n' > /etc/apt/preferences.d/snapshot && apt-get update && apt-get -y --allow-downgrades dist-upgrade && apt-get install -y --no-install-recommends squashfs-tools
"""


@dataclass(frozen=True, slots=True)
class EnvironmentImage:
    sha256: str
    size_bytes: int
    path: Path                       # <output>/<sha256>.squashfs


def tools_image(*, architecture: str) -> str:
    """The pinned image tool: BUILDER_IMAGE + PIN's snapshot sources + squashfs-tools, built by
    node_build_inputs.docker_build(role="image-tools"); returns its image ID. A snapshot pin, not
    the runner's apt, decides the mksquashfs version, so the digest cannot drift with the runner."""
    validate_builder(BUILDER_IMAGE, architecture, purpose="image_tools")
    with tempfile.TemporaryDirectory(prefix="photo-wall-image-tools-") as temporary:
        work = Path(temporary)
        (work / "snapshot.list").write_text("\n".join(source.line() for source in PIN.sources()) + "\n")
        (work / "Dockerfile").write_text(TOOLS_DOCKERFILE.format(builder_image=BUILDER_IMAGE))
        return docker_build(work, architecture=architecture, role="image-tools")


def image_from_archive(archive: Path, reference: AppEnvironmentRefV2, output: Path, *,
                       base_abi: str, graphics_abi: str, plugin_abi: str,
                       tools: str) -> EnvironmentImage:
    """stage_archive(archive, <private roots>, reference, ..., owner_uid=os.geteuid()) builds the
    verified tree. Its top directory is set to 0755. Then
    `docker run --rm --network none -e SOURCE_DATE_EPOCH -v <tree>:/in:ro -v <output>:/out <tools>
    mksquashfs /in /out/<tmp> *SQUASHFS_OPTIONS`, hashed, and renamed to <sha256>.squashfs.
    Raises stage_archive's ValueErrors unchanged. No root is needed: -all-root sets ownership."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="photo-wall-image-") as temporary:
        roots = Path(temporary) / "roots"
        roots.mkdir(mode=TREE_MODE)
        tree = stage_archive(archive, roots, reference, base_abi=base_abi,
                             graphics_abi=graphics_abi, plugin_abi=plugin_abi,
                             owner_uid=os.geteuid())
        tree.chmod(TREE_MODE)  # the mount's root: stage_archive's private 0700 work directory
        partial = output / f".{reference.environment_sha256}{IMAGE_SUFFIX}.partial"
        try:
            # The caller's ids, so the image file is the caller's to hash, rename and delete.
            subprocess.run(["docker", "run", "--rm", "--platform", "linux/" + reference.architecture,
                            "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                            "-e", f"SOURCE_DATE_EPOCH={PIN.epoch}",
                            "-v", f"{tree}:/in:ro", "-v", f"{output}:/out", tools,
                            "mksquashfs", "/in", "/out/" + partial.name, *SQUASHFS_OPTIONS],
                           check=True, stdout=subprocess.DEVNULL)
            sha256 = file_sha256(partial)
            path = output / (sha256 + IMAGE_SUFFIX)
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)
    return EnvironmentImage(sha256, path.stat().st_size, path)
