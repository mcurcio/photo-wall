"""One policy for builds that consume images loaded in the local Docker daemon."""

from __future__ import annotations

from pathlib import Path


def daemon_image_build(tag: str, context: Path, *, dockerfile: Path | None = None,
                       network: str | None = None,
                       labels: tuple[tuple[str, str], ...] = ()) -> list[str]:
    """Build with Docker's daemon-backed default builder and load the result."""

    command = ["docker", "buildx", "build", "--builder", "default", "--load", "--tag", tag]
    if dockerfile is not None:
        command.extend(("--file", str(dockerfile)))
    if network is not None:
        command.extend(("--network", network))
    for name, value in labels:
        command.extend(("--label", f"{name}={value}"))
    command.append(str(context))
    return command


def daemon_compose_build(service: str) -> tuple[str, ...]:
    """Select the daemon-backed builder for a Compose build."""

    return ("build", "--builder", "default", service)
