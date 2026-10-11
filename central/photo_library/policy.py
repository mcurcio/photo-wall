"""The photo library's rules, each with one home (roadmap 4; run ledger .claude/runs/photo-4.md).

Constants are real in F0. Functions are slice C1's (their docstrings are the contract); every
consumer calls these, never a copy: the store judges contacts, the routes normalise URLs and
hint keys, the media worker asks Immich for exactly `REQUIRED_PERMISSIONS`, and the Immich
fixture's key is pinned equal to it by a test.
"""

from __future__ import annotations

from typing import Final

from central.photo_library.model import (
    ContactResult,
    ImmichTooOld,
    ImmichUser,
    ImmichVersion,
    LibraryHealth,
    UntestedVersion,
)

# The key's permissions (design parked note "Permissions, one home"); 5a needs face.read.
REQUIRED_PERMISSIONS: Final[tuple[str, ...]] = (
    "album.read", "album.statistics", "asset.download", "asset.edit.get", "asset.read",
    "asset.statistics", "asset.view", "face.read", "person.read", "tag.read", "user.read",
)
# C2: 2.5.x is tested; newer works with a note; older than 2.5 is Needs a look ("Update Immich").
OLDEST_VERSION: Final = ImmichVersion(major=2, minor=5, patch=0)
TESTED_RELEASE: Final[tuple[int, int]] = (2, 5)
# #12 "Look for new photos every": each connection check and each Source's check (C2).
CHECK_EVERY_SECONDS: Final = 900
# C8: an unsaved draft and its pool, when nothing else uses that pool, go after a day.
DRAFT_KEEP_SECONDS: Final = 86400
# One page of a pool in the console (newest first).
POOL_PAGE_SIZE: Final = 60


def normalise_url(text: str) -> str:
    """The connection URL as stored: http or https, a host, no userinfo, query or fragment;
    trailing slashes dropped and "/api" appended when the path does not already end in it
    ("http://immich.local:2283" -> "http://immich.local:2283/api"). ValueError otherwise
    (routes: 422 invalid_url). Plain http is accepted (UX over security, owner 2026-10-09)."""
    raise NotImplementedError("slice C1")


def key_hint(key: str) -> str:
    """What the console shows of a stored key: "…" and its last four characters."""
    raise NotImplementedError("slice C1")


def version_verdict(version: ImmichVersion) -> ImmichTooOld | UntestedVersion | None:
    """Older than OLDEST_VERSION: ImmichTooOld(found, oldest). A release newer than
    TESTED_RELEASE: UntestedVersion(found). The tested release: None."""
    raise NotImplementedError("slice C1")


def contact_health(result: ContactResult, *, bound_user: ImmichUser | None,
                   previous: LibraryHealth, checked_at: float) -> LibraryHealth:
    """A connection's health after one applied contact (the store calls it; the latest-started
    contact wins, ordered by the store's contact number, never by clocks).

    Precedence: a ContactRefused's problem; then OtherUser when `bound_user` is set and the
    answer's user differs; then ImmichTooOld; then MissingPermissions. With none of these, ok
    with notes: UntestedVersion, and NoFullSizeCopies when `full_size_copies` is False.
    `since` stays `previous.since` while the problem's kind is unchanged, else `checked_at`.
    """
    raise NotImplementedError("slice C1")
