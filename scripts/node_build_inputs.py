"""Reviewed node builder inputs, shared by native and Debian closure builds.

The Debian snapshot/package declaration remains scripts.debian_packages.PIN.
Images must provide CA trust before authenticated snapshot normalization.

`docker_build` is the one way a node builder runs `docker build`. Every build passes the pin's
SOURCE_DATE_EPOCH as a build argument, which BuildKit uses for the image's config and history
times, so the same layers always give the same image ID; a Dockerfile that wants it in a RUN
environment declares `ARG SOURCE_DATE_EPOCH` itself. The image is loaded into the local Docker,
built by Docker's default builder unless PHOTO_WALL_NODE_BUILDER names another. A persisted
BuildKit cache is opt-in and changes no byte (a hit reuses the layer it records): with the
CACHE_* variables set (the CI node components workflow sets them), the build reads and writes
the cache each names, `{role}` replaced by the build's role, so each role keeps its own scope.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from scripts.debian_packages import PIN

BUILDER_IMAGE = "python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
ARCHITECTURES = ("arm64", "amd64")
# A buildx builder with a cache-capable driver (docker-container), and the --cache-from and
# --cache-to values for it, each with a `{role}` placeholder. Unset: Docker's daemon-backed
# default builder, named, never whichever builder is current (scripts/container_build.py's policy).
BUILDER_VARIABLE = "PHOTO_WALL_NODE_BUILDER"
CACHE_FROM_VARIABLE = "PHOTO_WALL_NODE_BUILD_CACHE_FROM"
CACHE_TO_VARIABLE = "PHOTO_WALL_NODE_BUILD_CACHE_TO"


def validate_builder(image: str, architecture: str, *, purpose: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError(purpose + "_builder_digest_required")
    if architecture not in ARCHITECTURES:
        raise ValueError(purpose + "_architecture")


def docker_build_argv(context: Path, *, architecture: str, role: str,
                      environ: Mapping[str, str] = os.environ) -> list[str]:
    """PURE. The build command for `context`'s Dockerfile; its image ID goes to context/image-id."""
    builder = environ.get(BUILDER_VARIABLE)
    caches = [(flag, environ[name].format(role=role)) for flag, name in
              (("--cache-from", CACHE_FROM_VARIABLE), ("--cache-to", CACHE_TO_VARIABLE))
              if environ.get(name)]
    if caches and not builder:
        raise ValueError(f"node_build_cache_needs_{BUILDER_VARIABLE}")
    argv = ["docker", "buildx", "build", "--builder", builder or "default", "--load"]
    for flag, value in caches:
        argv += [flag, value]
    return [*argv, "--platform", "linux/" + architecture,
            "--build-arg", f"SOURCE_DATE_EPOCH={PIN.epoch}",
            "--iidfile", str(context / "image-id"), str(context)]


def docker_build(context: Path, *, architecture: str, role: str) -> str:
    """Build `context`'s Dockerfile for linux/`architecture` and return the image ID, loaded
    into the local Docker so `docker create`/`run` and a later FROM can use it."""
    subprocess.run(docker_build_argv(context, architecture=architecture, role=role), check=True)
    return (context / "image-id").read_text().strip()
