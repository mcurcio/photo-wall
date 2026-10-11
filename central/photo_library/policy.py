"""The photo library's rules, each with one home (roadmap 4; run ledger .claude/runs/photo-4.md).

Constants are real in F0; the functions are slice T0's (their docstrings are the contract). Every
consumer calls these, never a copy: the store judges contacts, the routes normalise URLs and
hint keys, the media worker asks Immich for exactly `REQUIRED_PERMISSIONS`, and the Immich
fixture's key is pinned equal to it by a test.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

from central.photo_library.model import (
    ContactRefused,
    ContactResult,
    HealthOk,
    ImmichCopy,
    ImmichTooOld,
    ImmichUser,
    ImmichVersion,
    Kind,
    LibraryHealth,
    LibraryNote,
    MissingPermissions,
    NeedsALook,
    NoFullSizeCopies,
    OtherUser,
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
# A running check whose latest step began this long ago (database time) is due again, so a step
# lost with its worker never leaves a pool checking. Varies independently of CHECK_EVERY_SECONDS.
STALE_CHECK_SECONDS: Final = 900
# C8: an unsaved draft and its pool, when nothing else uses that pool, go after a day.
DRAFT_KEEP_SECONDS: Final = 86400
# The original types Immich v2.5.6 calls web-supported (server/src/utils/mime-types.ts): for these
# Immich makes no full-size copy of an unedited photo and redirects `size=fullsize&edited=false`
# to the original (follow-ups 2026-10-11, slice A3; the one home, media/immich.py reads it).
IMMICH_WEB_TYPES: Final[frozenset[str]] = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/avif",
    "image/svg+xml", "image/bmp"})
# One page of a pool in the console (newest first).
POOL_PAGE_SIZE: Final = 60
# A stored connection URL's longest form (ConnectionSecret.base_url).
_MAX_URL_LENGTH: Final = 2048
_UNSAFE_URL_TEXT = re.compile(r"[\s\x00-\x1f\x7f]")


def normalise_url(text: str) -> str:
    """The connection URL as stored: http or https, a host, no userinfo, query or fragment;
    trailing slashes dropped and "/api" appended when the path does not already end in it
    ("http://immich.local:2283" -> "http://immich.local:2283/api"). ValueError otherwise
    (routes: 422 invalid_url). Plain http is accepted (UX over security, owner 2026-10-09)."""
    text = text.strip()
    if _UNSAFE_URL_TEXT.search(text) or "?" in text or "#" in text:
        raise ValueError("a connection URL has no spaces, query or fragment")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("a connection URL is http or https with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("a connection URL carries no user or password")
    _ = parts.port   # ValueError on a port that is not a number in range
    path = parts.path.rstrip("/")
    if not path.endswith("/api"):
        path += "/api"
    url = f"{parts.scheme}://{parts.netloc}{path}"
    if len(url) > _MAX_URL_LENGTH:
        raise ValueError("a connection URL is too long")
    return url


def key_hint(key: str) -> str:
    """What the console shows of a stored key: "…" and its last four characters."""
    return "…" + key[-4:]


def version_verdict(version: ImmichVersion) -> ImmichTooOld | UntestedVersion | None:
    """Older than OLDEST_VERSION: ImmichTooOld(found, oldest). A release newer than
    TESTED_RELEASE: UntestedVersion(found). The tested release: None."""
    found = (version.major, version.minor, version.patch)
    if found < (OLDEST_VERSION.major, OLDEST_VERSION.minor, OLDEST_VERSION.patch):
        return ImmichTooOld(found=str(version), oldest=str(OLDEST_VERSION))
    if found[:2] > TESTED_RELEASE:
        return UntestedVersion(found=str(version))
    return None


def contact_health(result: ContactResult, *, bound_user: ImmichUser | None,
                   previous: LibraryHealth, checked_at: float) -> LibraryHealth:
    """A connection's health after one applied contact (the store calls it; the latest-started
    contact wins, ordered by the store's contact number, never by clocks).

    Precedence: a ContactRefused's problem; then OtherUser when `bound_user` is set and the
    answer's user differs; then ImmichTooOld; then MissingPermissions. With none of these, ok
    with notes: UntestedVersion, and NoFullSizeCopies when `full_size_copies` is False.
    `since` stays `previous.since` while the problem's kind is unchanged, else `checked_at`.
    An OtherUser names each user by name, or by email when the name is empty.
    """
    if isinstance(result, ContactRefused):
        problem = result.problem
    else:
        verdict = version_verdict(result.version)
        if bound_user is not None and result.user.user_ref != bound_user.user_ref:
            problem = OtherUser(expected=_named(bound_user), found=_named(result.user))
        elif isinstance(verdict, ImmichTooOld):
            problem = verdict
        elif result.missing_permissions:
            problem = MissingPermissions(missing=tuple(sorted(set(result.missing_permissions))))
        else:
            notes: list[LibraryNote] = []
            if isinstance(verdict, UntestedVersion):
                notes.append(verdict)
            if result.full_size_copies is False:
                notes.append(NoFullSizeCopies())
            return HealthOk(checked_at=checked_at, notes=tuple(notes))
    since = checked_at
    if isinstance(previous, NeedsALook) and previous.problem.kind == problem.kind:
        since = previous.since
    return NeedsALook(problem=problem, since=since, checked_at=checked_at)


def _named(user: ImmichUser) -> str:
    return user.name or user.email


def copies_in_immich(kind: Kind, edited: bool, original_type: str | None,
                     full_size_copies: bool | None) -> dict[ImmichCopy, bool | None]:
    """Which copies Immich v2.5.6 keeps for one asset (follow-ups 2026-10-11, slice A3; facts:
    Immich media.service.ts): thumbnail, preview and original always (a video: original only;
    its stills are not Photo Wall's); a full-size copy when the photo is edited, else when its
    original's type is not in IMMICH_WEB_TYPES and the connection's full-size copies are on
    (`full_size_copies`; None, or an unknown `original_type`: None, cannot tell). Keys in the
    order thumbnail, preview, fullsize, original. Pure."""
    if kind == "video":
        return {"thumbnail": False, "preview": False, "fullsize": False, "original": True}
    fullsize: bool | None
    if edited:
        fullsize = True
    elif original_type is None:
        fullsize = None
    elif original_type.lower() in IMMICH_WEB_TYPES:
        fullsize = False
    else:
        fullsize = full_size_copies
    return {"thumbnail": True, "preview": True, "fullsize": fullsize, "original": True}
