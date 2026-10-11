"""The photo library's vocabulary (roadmap 4; run ledger .claude/runs/photo-4.md, F0).

Values only. Invariants that a Field cannot state are each type's docstring; slice C1 adds the
validators that enforce them (the page names each one). `canonical_uuid`, `TagRef`,
`visible_text`, `TagText`, `LibraryTag` and `asset_identity` moved here verbatim from
media/models.py (which re-exports them until the integrate slice), so the domain has one home
and imports nothing from `media`.

Words (design-draft "New terms"): a **check** is one complete read of a Source's queries from
Immich in short steps; a **photo outcome** is in the pool, waiting (Immich has not read it yet) or
left out with a reason; the **photo pool** is a query's matches after exclusions, one per stack.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BeforeValidator, Field, SecretStr

from contracts.models import Digest, Identifier, Instant, Model, Positive

Count = Annotated[int, Field(ge=0, strict=True)]
Kind = Literal["image", "video"]  # the Planner's and the Player's word; the console says "photo"


def canonical_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid UUID") from None


# An Immich id (asset, album, person, tag, user): a canonical UUID. Album, person and tag ids may
# reach the browser (R22, PR 37 Q4); an asset's id never does.
LibraryRef = Annotated[str, Field(max_length=36), AfterValidator(canonical_uuid)]
TagRef = Annotated[str, Field(max_length=36), AfterValidator(canonical_uuid)]
AlbumRef = LibraryRef
PersonRef = LibraryRef

ConnectionRef = Identifier   # names one Immich connection; jobs carry this, never the key
SourceRef = Identifier       # a saved Photo source revision, "<name>:<revision>" (central/source_names.py)
DraftRef = Annotated[str, Field(max_length=36), AfterValidator(canonical_uuid)]  # an unsaved Source
PoolKey = Digest             # sha256 of a query's canonical form: one pool per distinct query

# The photo as Immich shows it (design rule 3): the original's SHA-1 (hex), plus for an edited
# photo "-" and the first 16 hex of the sha256 of its canonical edit list. Keys pool rows,
# thumbnails, preparation and 5a's content; an unedited photo's revision is its checksum, so its
# asset id is the one it had before 4.
AssetRevision = Annotated[str, Field(pattern=r"^[a-f0-9]{40}(-[a-f0-9]{16})?$")]

# Control characters, bidi controls (LRM, RLM, ALM, embeddings, isolates) and zero-width
# characters never reach the console (PR 37 §9): a tag name may otherwise reorder the text
# around it, or read as empty while not being so. Stripped before any length check.
_UNSAFE_TEXT = re.compile(
    "[\x00-\x1f\x7f-\x9f؜​-‏‪-‮⁦-⁩﻿]")
MAX_LIBRARY_TAGS = 5000
MAX_TAG_TEXT = 1024


def visible_text(value: object) -> object:
    return _UNSAFE_TEXT.sub("", value) if isinstance(value, str) else value


# Empty when nothing visible is left once stripped: the tag still exists in the library, so
# the stored list keeps its id (a by-id lookup must never call it gone); only the search
# hides it (`central/media_repository.py` `matching_tags`).
TagText = Annotated[str, BeforeValidator(visible_text), Field(max_length=MAX_TAG_TEXT)]


class LibraryTag(Model):
    """A tag as the browser may receive it (R22): ids, path and name only.

    The listed list is the library's whole list: every listed id is kept, even one whose
    path or name has nothing visible (`""`), which the search never offers.
    """

    tag_ref: TagRef
    path: TagText  # the full nested path, e.g. "Family/Christmas"
    name: TagText
    parent_ref: TagRef | None = None


def asset_identity(connection_id: str, upstream_id: str, revision: str) -> str:
    """Photo Wall's asset id: Immich-free, one per connection, Immich asset and revision."""
    identity = json.dumps([connection_id, upstream_id, revision], separators=(",", ":"))
    return "asset-" + hashlib.sha256(identity.encode()).hexdigest()


AssetId = Annotated[str, Field(pattern=r"^asset-[a-f0-9]{64}$")]

# --- The library's lists (Photo source editor pickers; tags keep `LibraryTag`) --------------------

ListKind = Literal["albums", "people", "places", "tags"]


class LibraryAlbum(Model):
    album_ref: AlbumRef
    name: TagText
    photos: Count   # Immich's own count for the album (assetCount)


class LibraryPerson(Model):
    """A named person; Immich's unnamed face clusters are never offered (nothing to pick by)."""

    person_ref: PersonRef
    name: TagText


class Place(Model):
    """A place is a city, state and country together; Immich matches all three exactly (C3).

    Invariant (C1 validator): at least `country` is non-empty after `visible_text`.
    """

    city: TagText | None = None
    state: TagText | None = None
    country: TagText


# --- A Photo source's query (#13-#33) ----------------------------------------------------------

MediaType = Literal["photo", "video", "live_photo"]
MAX_CHOSEN = 32  # albums, people or tags chosen in one list of one query: bounds one request body


class TakenBetween(Model):
    """#19, fixed dates. `start` and `end` are UTC instants, either open.

    Invariant (C1 validator): `start < end` when both are set.
    """

    kind: Literal["between"] = "between"
    start: Instant | None = None
    end: Instant | None = None


class TakenLast(Model):
    """#20, relative dates ("last 3 years"), resolved in UTC at each check (until 2b's timezone).

    Invariant (C1 validator): `years + months > 0`.
    """

    kind: Literal["last"] = "last"
    years: int = Field(default=0, ge=0, le=200, strict=True)
    months: int = Field(default=0, ge=0, le=11, strict=True)


TakenRange = Annotated[TakenBetween | TakenLast, Field(discriminator="kind")]


class Exclusions(Model):
    """#26-#28, #31. Each is its own Immich query, read whole each check and subtracted (C4).
    Archived, hidden and locked photos are never included: a fixed line, not a switch (C5).

    Invariant (C1 validator): each list unique and sorted (its canonical form).
    """

    albums: tuple[AlbumRef, ...] = Field(default=(), max_length=MAX_CHOSEN)
    people: tuple[PersonRef, ...] = Field(default=(), max_length=MAX_CHOSEN)
    tags: tuple[TagRef, ...] = Field(default=(), max_length=MAX_CHOSEN)
    screenshots: bool = True


class SourceQuery(Model):
    """One include query plus exclusion queries (design "TypeScript sketches", #13-#33).

    Several albums, people or tags mean "all of them" (Immich's meaning, C3); "any of them" is a
    second Source on the Scene (#36). Partners' photos are left out unless `partners` (#32).

    Invariants (C1 validators): `albums`, `people`, `tags` unique and sorted; `media_types`
    non-empty, unique and sorted. `canonical()` and `pool_key()` depend on them.
    """

    connection_ref: ConnectionRef
    albums: tuple[AlbumRef, ...] = Field(default=(), max_length=MAX_CHOSEN)
    people: tuple[PersonRef, ...] = Field(default=(), max_length=MAX_CHOSEN)
    place: Place | None = None
    taken: TakenRange | None = None
    favourites_only: bool = False
    tags: tuple[TagRef, ...] = Field(default=(), max_length=MAX_CHOSEN)
    media_types: tuple[MediaType, ...] = Field(default=("live_photo", "photo", "video"),
                                               min_length=1, max_length=3)
    partners: bool = False
    exclude: Exclusions = Exclusions()

    def canonical(self) -> str:
        """The query's meaning as canonical JSON (sorted keys, no spaces); a relative range stays
        relative, so "last 3 years" is one pool however many days pass."""
        raise NotImplementedError("slice C1")

    def pool_key(self) -> PoolKey:
        """sha256 hex of `canonical()`: equal queries share one pool, its check and its rows."""
        raise NotImplementedError("slice C1")


# --- Connections and their health (#8-#12; design "Settings › Photo library") -------------------


class ImmichVersion(Model):
    major: int = Field(ge=0, strict=True)
    minor: int = Field(ge=0, strict=True)
    patch: int = Field(ge=0, strict=True)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


class ImmichUser(Model):
    """The Immich user a key belongs to; a connection keeps the user of its first passing check."""

    user_ref: LibraryRef
    name: TagText
    email: TagText


class LibraryCounts(Model):
    """Immich's own statistics, shown as is on Settings › Photo library (#10)."""

    photos: Count
    videos: Count
    albums: Count
    people: Count


class KeyRejected(Model):
    kind: Literal["key_rejected"] = "key_rejected"            # fix: Paste a new key


class MissingPermissions(Model):
    kind: Literal["missing_permissions"] = "missing_permissions"  # fix: Make a key with these
    missing: tuple[str, ...] = Field(min_length=1)


class ImmichTooOld(Model):
    kind: Literal["immich_too_old"] = "immich_too_old"        # fix: Update Immich
    found: str
    oldest: str


class Unreachable(Model):
    kind: Literal["unreachable"] = "unreachable"              # fix: Check again


class UnexpectedAnswer(Model):
    kind: Literal["unexpected_answer"] = "unexpected_answer"  # fix: Check again; Details ›
    detail: str = Field(pattern=r"^[a-z_]{1,64}$")


class OtherUser(Model):
    kind: Literal["other_user"] = "other_user"                # fix: Paste a key for <expected>
    expected: str
    found: str


LibraryProblem = Annotated[
    KeyRejected | MissingPermissions | ImmichTooOld | Unreachable | UnexpectedAnswer | OtherUser,
    Field(discriminator="kind")]


class UntestedVersion(Model):
    kind: Literal["untested_version"] = "untested_version"    # newer than tested; nothing stops
    found: str


class NoFullSizeCopies(Model):
    kind: Literal["no_full_size_copies"] = "no_full_size_copies"  # HEIC, RAW play at preview size


LibraryNote = Annotated[UntestedVersion | NoFullSizeCopies, Field(discriminator="kind")]


class NotChecked(Model):
    state: Literal["not_checked"] = "not_checked"


class HealthOk(Model):
    state: Literal["ok"] = "ok"
    checked_at: Instant
    notes: tuple[LibraryNote, ...] = ()


class NeedsALook(Model):
    """Photos keep playing (C7); `since` is when this problem kind first appeared."""

    state: Literal["needs_a_look"] = "needs_a_look"
    problem: LibraryProblem
    since: Instant
    checked_at: Instant


LibraryHealth = Annotated[NotChecked | HealthOk | NeedsALook, Field(discriminator="state")]


class ContactAnswer(Model):
    """What one connection check found when Immich answered (media worker -> store)."""

    version: ImmichVersion
    user: ImmichUser
    missing_permissions: tuple[str, ...] = ()
    counts: LibraryCounts | None = None       # None when a permission it needs is missing
    full_size_copies: bool | None = None      # None when it could not be told


class ContactRefused(Model):
    """Immich did not answer usefully: key_rejected, unreachable or unexpected_answer only."""

    problem: KeyRejected | Unreachable | UnexpectedAnswer = Field(discriminator="kind")


ContactResult = ContactAnswer | ContactRefused


class ConnectionSecret(Model):
    """The one read of a key (media worker only; never serialised into an answer, job or log)."""

    connection_ref: ConnectionRef
    base_url: str = Field(max_length=2048)    # normalised, ends in /api (policy.normalise_url)
    api_key: SecretStr = Field(repr=False, exclude=True)
    bound_user: LibraryRef | None = None      # the first passing check's Immich user


class ContactTicket(Model):
    """A contact taken from the store: its number orders results (latest-started wins)."""

    contact_number: int = Field(ge=1, strict=True)
    connection: ConnectionSecret


# --- Pools: the per-photo outcome of a check ----------------------------------------------------

LeftOutReason = Literal[
    "unreadable_answer",   # this photo's answer did not parse
    "over_item_limits",    # pixels, bytes or video length
]
SourceProblem = Literal[
    "connection",          # see the connection's health
    "chosen_item_gone",    # an album, person or tag deleted in Immich
]


class LibraryAsset(Model):
    """One photo or video as Immich shows it now (edited), as the worker fetches and prepares it.

    `width` and `height` are upright (EXIF applied) and edited: the largest copy Photo Wall can
    fetch (C10). Replaces media.models.OriginalAsset in the integrate slice.
    Invariant (C1 validator): `duration` is set exactly for videos.
    """

    connection_ref: ConnectionRef
    upstream_id: LibraryRef
    revision: AssetRevision
    kind: Kind
    taken_at: Instant
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    duration: Positive | None = None

    @property
    def asset_id(self) -> str:
        return asset_identity(self.connection_ref, self.upstream_id, self.revision)


class PoolMember(Model):
    """Immich-free: what the Planner and 5a read of one photo in a pool (design `PoolMember`)."""

    asset_id: AssetId
    revision: AssetRevision
    kind: Kind
    taken_at: Instant
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)


class InPool(Model):
    outcome: Literal["in_pool"] = "in_pool"
    asset: LibraryAsset


class Waiting(Model):
    """Immich has not read this photo yet (no size): counted, not playable."""

    outcome: Literal["waiting"] = "waiting"
    upstream_id: LibraryRef
    kind: Kind
    taken_at: Instant


class LeftOut(Model):
    outcome: Literal["left_out"] = "left_out"
    upstream_id: LibraryRef
    kind: Kind | None = None
    taken_at: Instant | None = None
    reason: LeftOutReason


PoolEntry = Annotated[InPool | Waiting | LeftOut, Field(discriminator="outcome")]


class CheckMark(Model):
    """An id a check subtracts: in an exclusion query's answer, or a stacked copy (C9)."""

    upstream_id: LibraryRef
    mark: Literal["excluded", "stacked_copy"]


class NeverChecked(Model):
    state: Literal["never_checked"] = "never_checked"


class Checking(Model):
    state: Literal["checking"] = "checking"
    seen_so_far: Count
    started_at: Instant
    last_complete_at: Instant | None = None


class Complete(Model):
    state: Literal["complete"] = "complete"   # zero here is truly empty
    at: Instant


class Incomplete(Model):
    state: Literal["incomplete"] = "incomplete"
    reason: Literal["over_1000_at_one_instant"]
    last_complete_at: Instant | None = None


class Failed(Model):
    state: Literal["failed"] = "failed"
    problem: SourceProblem
    last_complete_at: Instant | None = None


CheckState = Annotated[NeverChecked | Checking | Complete | Incomplete | Failed,
                       Field(discriminator="state")]


class PoolThumbnail(Model):
    """A pool row's library identity, for the worker's thumbnail fetch only (never served)."""

    connection_ref: ConnectionRef
    upstream_id: LibraryRef
    revision: AssetRevision


class PoolCursor(Model):
    """Keyset position in a pool page (newest first): valid while rows are added or swept."""

    taken_at: Instant
    asset_id: AssetId

    def encode(self) -> str:
        """Opaque URL-safe text the console hands back as `after`."""
        raise NotImplementedError("slice C2")

    @classmethod
    def decode(cls, text: str) -> PoolCursor:
        """The cursor `encode` wrote; ValueError on anything else (routes: 422 cursor_invalid)."""
        raise NotImplementedError("slice C2")
