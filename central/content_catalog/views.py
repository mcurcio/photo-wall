"""The console's Releases page, by version per Pi (Q2 A, 2026-10-16): its read and its bodies.

| Method | Path | Body | Answer | Refusals ({"error": code}) |
|---|---|---|---|---|
| GET | /v1/operator/releases | | ReleasesView | |
| PUT | /v1/operator/releases/wall | VersionChoice | ReleasesView | 404 unknown_version, 409 not_deployable |
| POST | /v1/operator/players/{serial}/install | VersionChoice | contracts.central_api InstallStarted | 404 unknown_player, 404 unknown_version, 409 not_deployable |

Update the wall (PUT .../wall) sets every Pi's version and restarts none: the console's rollout
reboots each Player in turn. Install (POST .../install) is Central's `installPlayer`, the same service
Home Assistant's door calls: it sets that one Pi's version, downloads it, then restarts that Pi.
Each route takes the operator `admin` dependency; the routes are central/fleet/node_routes.py's.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from contracts.central_api import InstallProgress, Name, ReadinessState, ReleaseTag, Serial
from contracts.models import Digest, Identifier, Instant, Model


class ReleaseView(Model):
    """One release Central observed, newest first: `problem` says why it cannot be deployed (None:
    deployable); `base_tag` and `app_environment_sha256` are its deployment's (None while rejected,
    or a release with no app); `in_window` when it is among the newest stable releases Central keeps
    downloaded; its readiness now (None: not deployable)."""
    version: ReleaseTag
    stable: bool
    problem: str | None
    base_tag: Annotated[str, Field(max_length=128)] | None
    app_environment_sha256: Digest | None
    in_window: bool
    readiness: ReadinessState | None
    readiness_reason: str | None
    missing_bytes: int = Field(ge=0)
    discovered_at: Instant | None


class PlayerVersionView(Model):
    """One enrolled Pi's version: what it boots next (`version`, None: no release is ready to boot),
    whether Central keeps one for it (`chosen`; False: the newest ready stable release), its version
    before the last change, an install's progress, and the base tag its host last reported (`running`,
    Central's record; None: not reported this boot)."""
    serial: Serial
    player_id: Identifier | None
    name: Name
    version: ReleaseTag | None
    chosen: bool
    previous: ReleaseTag | None
    install: InstallProgress | None
    running: Annotated[str, Field(max_length=128)] | None


class ReleasesView(Model):
    """The Releases page: every observed release, every enrolled Pi's version, and `default`, the
    release a Pi with no version boots (the newest ready stable release; None: none is ready)."""
    read_at: Instant
    releases: tuple[ReleaseView, ...]
    players: tuple[PlayerVersionView, ...]
    default: ReleaseTag | None


class VersionChoice(Model):
    """The body of Update the wall and of Install: the release to put on."""
    version: ReleaseTag


__all__ = ["PlayerVersionView", "ReleaseView", "ReleasesView", "VersionChoice"]
