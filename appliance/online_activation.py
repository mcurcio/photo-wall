"""Unprivileged local facts for preparing an online app attempt.

These values carry correlation and exact artifact refs. Constructing one does
not authenticate a command or grant permission to stop the running Player.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OnlineArtifactRef:
    sha256: str
    size: int
    base_abi: str


@dataclass(frozen=True, slots=True)
class OnlineAttempt:
    attempt_id: UUID
    command_sha256: str
    target: OnlineArtifactRef
    fallback: OnlineArtifactRef
