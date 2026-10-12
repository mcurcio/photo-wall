"""What the media worker's checks need from Immich (roadmap 4; run ledger .claude/runs/photo-4.md,
slices M1 and M2). media/immich.py `ImmichClient` implements `LibraryClient` (M1); the checks in
media/library_checks.py and the preparation fetch depend only on this protocol (M2, M3), and the
worker's tests drive them with a fake.

Design rule 1: Immich answers every match question; this boundary never decides what to subtract.
It parses answers into per-photo facts, and a malformed photo is only that photo's problem
(`SearchItem.problem`). A connection-wide failure raises `LibraryUnavailable`.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from central.photo_library.model import (
    AssetRevision,
    ContactResult,
    CopySize,
    Count,
    Kind,
    LeftOutReason,
    LibraryAlbum,
    LibraryAsset,
    LibraryPerson,
    LibraryProblem,
    LibraryRef,
    LibraryTag,
    MeasurableCopy,
    MeasuredCopy,
    MediaTypeName,
    OriginalFile,
    Place,
    SourceQuery,
)
from central.photo_library.rendition import (
    FetchStep,
    FrameNeed,
    copy_ladder,
    copy_to_measure,
    covers,
)
from contracts.models import Digest, Instant, Model, Positive

# --- The rendition choice (follow-ups 2026-10-11, slice A4; owner, 2026-10-09: "use Immich
# downsampled copy only if it is at least the visible Frame size, else the original") --------


def fetch_plan(asset: LibraryAsset, needs: Sequence[FrameNeed],
               known: Mapping[MeasurableCopy, CopySize], full_size_copies: bool | None
               ) -> tuple[FetchStep, ...]:
    """The copies to try, smallest first: the copy ladder (central/photo_library/rendition.py
    `copy_ladder`, the one home of which copies Photo Wall can read), narrowed by what is known.
    The fetch keeps the first that `covers` the needs and always keeps the last. Pure.

    - No need: the ladder's largest copy alone.
    - A known preview size (`known["preview"]`) that does not cover: the ladder's largest copy
      alone (the preview is not downloaded again), unless the preview is all the ladder holds.
    - Otherwise the whole ladder."""
    ladder = copy_ladder(asset, full_size_copies)
    preview = known.get("preview")
    if len(ladder) > 1 and (not needs or (preview is not None and not covers(
            preview.width, preview.height, needs))):
        return (ladder[-1],)
    return ladder


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
    stack_primary: LibraryRef | None = None           # `stack.primaryAssetId` when Immich sends it
    # The original file (`originalMimeType` and `exifInfo`, sent because every search asks
    # `withExif`; follow-ups 2026-10-11, slice A3). None: the row carried no `exifInfo`.
    original: OriginalFile | None = None
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
    (screenshot_name: `originalFileName`) or a PNG with no camera (screenshot_png: in Immich
    v2.5.6's MetadataSearchDto `make` is not nullable but `model` is, so "no camera" is
    `model: null`)."""

    kind: ExclusionKind
    ref: LibraryRef | None = None                     # set exactly for album, person, tag


class TimeBucket(Model):
    """A month of Immich's timeline (local time, "YYYY-MM-01")."""

    month: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-01$")


class FetchedRendition(Model):
    """What preparation reads: the edited photo Immich shows (C10), measured on download.
    `rendition` is the copy Immich served for the kept step: a "fullsize" step Immich redirected
    to the preview is "preview", and one it redirected to the original is "original"."""

    path: Path
    rendition: FetchStep
    # One of the types the client downloads: `media.prepare.STILL_MEDIA_TYPES` or a video's
    # (`media.immich` refuses any other before writing a byte).
    media_type: MediaTypeName
    size: int = Field(gt=0)
    sha256: Digest
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    # Every still copy this fetch downloaded and measured, the kept one included; never the
    # original, whose size is Immich's (`OriginalFile`). The worker records them
    # (`LibraryWorkerStore.record_copies`; slice A4).
    measured: tuple[MeasuredCopy, ...]


class FaceBox(Model):
    """One face Immich found (`GET /faces?id=<asset id>`, v2.5.6 `AssetFaceResponseDto`): its
    pixel box on the `image_width` x `image_height` image Immich measured it on (its preview,
    upright and edited; not the original's pixels). media/key_content.py `faces_of` turns it into
    fractions (roadmap 5a, slice M1)."""

    x1: int = Field(ge=0)
    y1: int = Field(ge=0)
    x2: int = Field(ge=0)
    y2: int = Field(ge=0)
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)


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
        """Months whose timeline counts differ with stacks shown and hidden (where copies hide).
        The fallback for C9 (design parked note): used only if a `withStacked` search answer does
        not carry `stack.primaryAssetId` on a stacked copy; M1 settles which on the fixture's stack
        and deletes this pair when the search answer carries it (erratum E-P4-M1-n records it)."""
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

    async def faces(self, upstream_id: str) -> tuple[FaceBox, ...]:
        """Every face Immich holds for the asset (`GET /faces?id=`, permission `face.read`), in
        Immich's order. Owner, 2026-10-11: "If immich face detection is unavailable, then smart
        crop doesn't take into account faces": a 403 (the key lacks face.read) answers () like an
        asset with no faces (the connection's health already names the missing permission); a
        malformed box is left out. Other failures raise LibraryUnavailable."""
        ...

    async def fetch_rendition(self, asset: LibraryAsset, *, plan: Sequence[FetchStep],
                              needs: Sequence[FrameNeed], destination: Path) -> FetchedRendition:
        """Download each step of `plan` (`fetch_plan`) in order, measure it, and keep the first
        that `covers(needs)`, else the last: "preview" is `size=preview&edited=true`, "fullsize"
        is `size=fullsize&edited=<asset.edited>` (following same-origin redirects; a redirect to
        the preview after the preview was downloaded keeps that preview), "original" is
        `/assets/{id}/original?edited=false`. Re-checks the asset's revision first. Exclusively
        creates `destination`; removes only what it created on failure."""
        ...

    async def close(self) -> None: ...


async def measure_readable_copy(client: LibraryClient, asset: LibraryAsset,
                                known: Mapping[MeasurableCopy, CopySize]
                                ) -> tuple[MeasuredCopy, ...]:
    """Follow-up S1: download and measure (from its header, with Pillow: `fetch_rendition`) the
    copy the photo's readable size needs and Photo Wall has not measured
    (central/photo_library/rendition.py `copy_to_measure`: a RAW photo's preview), in a private
    temporary folder it removes; () when none is needed. The key content measurement runs it
    first (media/worker.py `measure`), so a photo the Planner could not size is sized by its
    next measurement."""
    if copy_to_measure(asset, None, known) is None:
        return ()
    with tempfile.TemporaryDirectory(prefix="photo-wall-measure-") as folder:
        fetched = await client.fetch_rendition(asset, plan=("preview",), needs=(),
                                               destination=Path(folder) / "copy")
    return fetched.measured
