"""What the media worker's checks need from Immich (roadmap 4; run ledger .claude/runs/photo-4.md,
slices M1 and M2). media/immich.py `ImmichClient` implements `LibraryClient` (M1); the checks in
media/library_checks.py and the preparation fetch depend only on this protocol (M2, M3), and the
worker's tests drive them with a fake.

Design rule 1: Immich answers every match question; this boundary never decides what to subtract.
It parses answers into per-photo facts, and a malformed photo is only that photo's problem
(`SearchItem.problem`). A connection-wide failure raises `LibraryUnavailable`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from central.photo_library.model import (
    AssetRevision,
    ContactResult,
    Count,
    Kind,
    LeftOutReason,
    LibraryAlbum,
    LibraryAsset,
    LibraryPerson,
    LibraryProblem,
    LibraryRef,
    LibraryTag,
    Place,
    SourceQuery,
)
from contracts.models import Digest, Instant, Model, Positive


class LibraryUnavailable(Exception):
    """The connection failed for this call (key rejected, unreachable, unexpected answer); a
    check that meets it fails with SourceProblem "connection" and removes nothing."""

    def __init__(self, problem: LibraryProblem) -> None:
        super().__init__(problem.kind)
        self.problem = problem


class ChosenItemGone(Exception):
    """An album, person or tag the query names no longer exists in Immich (Immich answers 400 for
    an unknown id); the check fails with SourceProblem "chosen_item_gone"."""


class TakenWindow(Model):
    """A query's capture-time bounds for one check, relative dates already resolved (UTC)."""

    after: Instant | None = None
    before: Instant | None = None


class KeysetPosition(Model):
    """Where the next page continues (design parked note "Keyset"): Immich ordered newest first by
    `fileCreatedAt`; the next page asks `takenBefore` = `edge` (inclusive) and drops `edge_ids`, the
    ids already seen at that instant. A full page at one instant switches `by` to "created" within
    that instant (`createdBefore`); past that the check is incomplete."""

    by: Literal["taken", "created"] = "taken"
    edge: Instant
    created_edge: Instant | None = None
    edge_ids: tuple[LibraryRef, ...] = ()


class SearchItem(Model):
    """One search answer row, parsed. `problem` set: the row could not be used (left out)."""

    upstream_id: LibraryRef
    owner_ref: LibraryRef | None = None
    kind: Kind | None = None
    live_photo: bool = False
    taken_at: Instant | None = None
    checksum: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    is_edited: bool = False
    updated_at: str | None = Field(default=None, max_length=64)
    width: int | None = Field(default=None, gt=0)     # upright; None: Immich has not read it yet
    height: int | None = Field(default=None, gt=0)
    duration: Positive | None = None
    problem: LeftOutReason | None = None


class SearchPage(Model):
    items: tuple[SearchItem, ...]
    next: KeysetPosition | None                       # None: the query is read to its end
    over_one_instant: bool = False                    # the createdBefore fallback ran out too


class IdsPage(Model):
    """One page of an exclusion query's answer: ids only."""

    ids: tuple[LibraryRef, ...]
    next: KeysetPosition | None


ExclusionKind = Literal["album", "person", "tag", "screenshot_name", "screenshot_png"]


class ExclusionQuery(Model):
    """One exclusion, its own Immich query (C4). A screenshot is a file named "screenshot"
    (screenshot_name) or a PNG with no camera maker (screenshot_png)."""

    kind: ExclusionKind
    ref: LibraryRef | None = None                     # set exactly for album, person, tag


class TimeBucket(Model):
    """A month of Immich's timeline (local time, "YYYY-MM-01")."""

    month: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-01$")


class FetchedRendition(Model):
    """What preparation reads: the edited photo Immich shows (C10), measured on download."""

    path: Path
    rendition: Literal["preview", "fullsize", "original"]
    media_type: Literal["image/jpeg", "image/png", "image/webp", "video/mp4", "video/quicktime",
                        "video/webm", "video/x-matroska"]
    size: int = Field(gt=0)
    sha256: Digest
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class LibraryClient(Protocol):
    """One connection's client; async, every call bounded by its own budget (no retry loops: the
    worker's tasks own retries)."""

    async def contact(self) -> ContactResult:
        """Version, the key's user, missing REQUIRED_PERMISSIONS, Immich's statistics (photos,
        videos, albums, people) and whether full-size copies are on. Never raises for a
        connection problem: ContactRefused carries it."""
        ...

    async def albums(self) -> tuple[LibraryAlbum, ...]: ...
    async def people(self) -> tuple[LibraryPerson, ...]: ...
    async def places(self) -> tuple[Place, ...]: ...
    async def tags(self) -> tuple[LibraryTag, ...]: ...

    async def up_to(self, query: SourceQuery, window: TakenWindow) -> Count:
        """Immich's statistics for the include query ("up to N": partners, stacked copies and
        exclusions are still in it)."""
        ...

    async def exclusion_page(self, exclusion: ExclusionQuery,
                             after: KeysetPosition | None) -> IdsPage:
        """One page of an exclusion query, every visibility and owner Immich returns."""
        ...

    async def stacked_buckets(self, *, partners: bool) -> tuple[TimeBucket, ...]:
        """Months whose timeline counts differ with stacks shown and hidden (where copies hide)."""
        ...

    async def stack_primaries(self, buckets: tuple[TimeBucket, ...]) -> frozenset[str]:
        """Ids Immich's timeline shows with stacks collapsed in these months: a search id taken in
        or beside such a month and absent here is a stacked copy (C9)."""
        ...

    async def search_page(self, query: SourceQuery, window: TakenWindow,
                          after: KeysetPosition | None) -> SearchPage:
        """One include page: every filter of the query in one `search/metadata` body, visibility
        timeline only (archived, hidden, locked never: C5), newest first."""
        ...

    async def edits_fingerprint(self, upstream_id: str) -> str:
        """16 hex of the sha256 of the asset's canonical edit list (assets/{id}/edits)."""
        ...

    async def revision_now(self, upstream_id: str) -> tuple[SearchItem, AssetRevision]:
        """Re-read one asset before a fetch: its row and its revision now."""
        ...

    async def thumbnail(self, upstream_id: str, revision: AssetRevision) -> bytes:
        """The edited thumbnail, re-encoded by Photo Wall (no metadata survives); raises
        MediaError("asset_changed") when the asset's revision moved."""
        ...

    async def fetch_rendition(self, asset: LibraryAsset, *, frame_long_edge: int | None,
                              destination: Path) -> FetchedRendition:
        """Photos: the edited preview when its long edge covers `frame_long_edge`, else Immich's
        edited full-size copy (following same-origin redirects; with full-size copies off, the
        preview); `None` takes the full-size copy. Videos: the original. Exclusively creates
        `destination`; removes only a file it created on failure."""
        ...

    async def close(self) -> None: ...
