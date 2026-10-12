"""What depends on the photo library store (roadmap 4; run ledger .claude/runs/photo-4.md): one
plain method per question, never a generic apply. central/infra/photo_library_store.py implements
the operator and worker sides over PostgreSQL (slices C1, C2); the routes depend only on the
operator protocols, the media worker only on `LibraryWorkerStore`, coordination only on
`ScenePoolReader` (through central.media_ports.CoordinationMedia).

Every time compared here is the database's (G11). Fences are numbers the database hands out
(contact numbers, check numbers), never clock comparisons across processes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import Field

from central.catalog import SourcePool
from central.installation_models import FrameOutput
from central.photo_library.model import (
    AlbumRef,
    AssetRevision,
    CheckMark,
    ConnectionRef,
    ConnectionSecret,
    ContactResult,
    ContactTicket,
    CopySize,
    Count,
    DraftRef,
    LibraryAlbum,
    LibraryPerson,
    LibraryRef,
    LibraryTag,
    ListKind,
    MeasurableCopy,
    MeasuredCopy,
    PersonRef,
    Place,
    PoolCursor,
    PoolEntry,
    PoolKey,
    PoolThumbnail,
    SourceProblem,
    SourceQuery,
    SourceRef,
    TagRef,
)
from central.photo_library.views import (
    ConnectionWrite,
    LibraryConnectionView,
    ListAnswer,
    PhotoFacts,
    SourcePoolView,
)
from contracts.models import Instant, Model


class LibraryRefused(Exception):
    """A refusal the routes answer as {"error": code} with `status`:
    connection_unknown 404, sources_use_connection 409, invalid_url 422, key_required 422,
    connection_name_exists 409, draft_unknown 404, source_unknown 404, cursor_invalid 422,
    source_name_exists 409, source_revision_conflict 409, invalid_source_name 422."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


# --- Operator side (routes) --------------------------------------------------------------------


class PhotoLibraryQueries(Protocol):
    def connections(self) -> tuple[LibraryConnectionView, ...]:
        """Every connection, by name; cut-over rows (C12) included."""
        ...

    def albums(self, connection_ref: ConnectionRef, q: str, limit: int) -> ListAnswer[LibraryAlbum]:
        """Albums whose name starts with `q` first, then those containing it (casefolded, NFKC).
        LibraryRefused("connection_unknown", 404)."""
        ...

    def albums_by_id(self, connection_ref: ConnectionRef,
                     ids: tuple[AlbumRef, ...]) -> ListAnswer[LibraryAlbum]:
        """Exactly these albums; `absent` names ids a current, complete list lacks."""
        ...

    def people(self, connection_ref: ConnectionRef, q: str, limit: int) -> ListAnswer[LibraryPerson]:
        """Named people, matched as `albums`."""
        ...

    def people_by_id(self, connection_ref: ConnectionRef,
                     ids: tuple[PersonRef, ...]) -> ListAnswer[LibraryPerson]:
        ...

    def places(self, connection_ref: ConnectionRef, q: str, limit: int) -> ListAnswer[Place]:
        """Places whose city, state or country matches `q`, as `albums`."""
        ...

    def tags(self, connection_ref: ConnectionRef, q: str, limit: int) -> ListAnswer[LibraryTag]:
        """GET /v1/operator/library/tags, now from the generalised list store."""
        ...

    def tags_by_id(self, connection_ref: ConnectionRef,
                   ids: tuple[TagRef, ...]) -> ListAnswer[LibraryTag]:
        ...

    def photo_facts(self, asset_id: str) -> PhotoFacts:
        """GET /v1/operator/library/photos/{asset_id} (follow-ups 2026-10-11, slice A3): one
        photo of a wanted pool, its original file and Immich's copies. LibraryRefused
        ("photo_not_found", 404) for an id no wanted pool row selects (the thumbnail route's
        rule: only such an id may be referenced)."""
        ...


class PhotoLibraryCommands(Protocol):
    def add_connection(self, write: ConnectionWrite) -> ConnectionRef:
        """Store a new connection (key write-only) and queue its check in the same transaction.
        LibraryRefused("invalid_url", 422), ("key_required", 422)."""
        ...

    def change_connection(self, connection_ref: ConnectionRef, write: ConnectionWrite) -> None:
        """Rename, re-point or re-key; `write.key` None keeps the key. Queues a check.
        The bound Immich user stays (a key for another user reads Needs a look, other_user).
        LibraryRefused("connection_unknown", 404), ("invalid_url", 422)."""
        ...

    def remove_connection(self, connection_ref: ConnectionRef) -> None:
        """Delete the connection, its key, health and lists. Refused while a saved Source or a
        draft uses it: LibraryRefused("sources_use_connection", 409); ("connection_unknown", 404)."""
        ...

    def check_connection(self, connection_ref: ConnectionRef) -> None:
        """Check now: queue a contact; the answer lands on the row's health."""
        ...


class PhotoSourceCommands(Protocol):
    def create_draft(self, query: SourceQuery, editing: SourceRef | None) -> DraftRef:
        """An unsaved Source whose check starts at once (C8). LibraryRefused
        ("connection_unknown", 404), ("source_unknown", 404) for an unknown `editing`."""
        ...

    def set_draft_query(self, draft: DraftRef, query: SourceQuery) -> None:
        """Point the draft at the new query's pool (cleared from the console's view: a different
        pool), queueing its check. ("draft_unknown", 404), ("connection_unknown", 404)."""
        ...

    def save_draft(self, draft: DraftRef, name: str) -> SourceRef:
        """Save at any time: the next revision of `editing`'s name (renamed to `name` when it
        differs), or revision 1 of a new name; the pool carries over (same pool key) and Scenes
        using the edited Source move to the new revision (Runtime, as Source edits do today).
        ("draft_unknown", 404), ("source_name_exists", 409), ("source_revision_conflict", 409),
        ("invalid_source_name", 422)."""
        ...

    def check_source(self, source_ref: SourceRef) -> None:
        """Check now (#12's button): queue the Source's pool check. ("source_unknown", 404)."""
        ...


class PhotoSourceQueries(Protocol):
    def draft_pool(self, draft: DraftRef, after: PoolCursor | None) -> SourcePoolView:
        """("draft_unknown", 404)."""
        ...

    def source_pool(self, source_ref: SourceRef, after: PoolCursor | None) -> SourcePoolView:
        """("source_unknown", 404)."""
        ...


# --- Media worker side -------------------------------------------------------------------------

JsonObject = dict[str, Any]


class KnownRevision(Model):
    """The revision last recorded for an Immich asset, and the `updatedAt` it was taken at: an
    edited photo's edit list is re-read only when `updatedAt` moved."""

    updated_at: str = Field(max_length=64)
    revision: AssetRevision


class CheckLease(Model):
    """One running check of one pool, handed to the step that continues it."""

    pool_key: PoolKey
    query: SourceQuery
    connection: ConnectionSecret
    check_number: int = Field(ge=1, strict=True)
    started_at: Instant
    progress: JsonObject | None = None   # the worker's own resume state; None: a fresh check


class CheckStep(Model):
    """One bounded step's result. Rows are added (never removed) as they are confirmed."""

    progress: JsonObject
    marks: tuple[CheckMark, ...] = ()
    entries: tuple[PoolEntry, ...] = ()
    seen_so_far: Count
    immich_up_to: Count | None = None


class CheckComplete(Model):
    outcome: Literal["complete"] = "complete"


class CheckIncomplete(Model):
    outcome: Literal["incomplete"] = "incomplete"
    reason: Literal["over_1000_at_one_instant"]


class CheckFailed(Model):
    outcome: Literal["failed"] = "failed"
    problem: SourceProblem


CheckFinish = CheckComplete | CheckIncomplete | CheckFailed


class LibraryWorkerStore(Protocol):
    """The media worker's whole view of the store (slices C1 for contacts and lists, C2 for
    checks). Implemented by central.infra.photo_library_store.PgPhotoLibrary; media tests use
    tests/support/library_store_fake.py, held to the same behaviour by the conformance suite."""

    def due_contacts(self) -> tuple[ConnectionRef, ...]:
        """Connections with a key whose last contact started CHECK_EVERY_SECONDS ago or more."""
        ...

    def begin_contact(self, connection_ref: ConnectionRef) -> ContactTicket | None:
        """Take the next contact number and the secret; None for an unknown or keyless one."""
        ...

    def connection_secret(self, connection_ref: ConnectionRef) -> ConnectionSecret | None:
        """The key and URL for a fetch or a thumbnail (preparation, M3); None: unknown or keyless."""
        ...

    def record_contact(self, ticket: ContactTicket, result: ContactResult) -> bool:
        """Apply iff `ticket.contact_number` is newer than the last applied (False: stale, nothing
        written). Health through policy.contact_health; the first passing contact binds the
        Immich user; counts kept from a passing contact."""
        ...

    def record_list(self, connection_ref: ConnectionRef, kind: ListKind,
                    items: Sequence[LibraryAlbum | LibraryPerson | Place | LibraryTag] | None,
                    error: str | None) -> None:
        """A listing replaces that kind's stored list; a failure (error code) keeps it and its
        age. Exactly one of `items` and `error`. No count cap (owner 2026-10-09)."""
        ...

    def due_pools(self) -> tuple[PoolKey, ...]:
        """Wanted pools (a live draft, or a Source revision Runtime or a name needs) whose
        connection has a key, and either: no check running, and the last check finished
        CHECK_EVERY_SECONDS ago or more, or never ran, or a Check now asked for one; or a check
        running whose latest step began STALE_CHECK_SECONDS ago or more (database time), so a
        step lost with its worker never leaves a pool checking."""
        ...

    def begin_check(self, pool_key: PoolKey) -> CheckLease | None:
        """Resume the pool's running check from its stored progress, or start the next check
        number; either way the latest step's start (`step_at`) becomes now. None when the pool
        is gone, unwanted, or its connection has no key."""
        ...

    def known_revisions(self, connection_ref: ConnectionRef,
                        upstream_ids: Sequence[LibraryRef]) -> dict[str, KnownRevision]:
        """The recorded revisions of these Immich assets, by upstream id."""
        ...

    def record_step(self, lease: CheckLease, step: CheckStep) -> bool:
        """In one transaction, fenced by `lease.check_number` (False: superseded, nothing
        written): store the marks, then upsert each entry's row as seen by this check (an InPool's
        asset also into asset_revisions), except an entry whose upstream id carries a mark of this
        check (stored now or by an earlier step): the subtraction's one home. Store the progress
        and queue the next step."""
        ...

    def unseen_rows(self, lease: CheckLease, limit: int) -> tuple[LibraryRef, ...]:
        """Up to `limit` of the pool's rows this check has neither seen nor marked, by Immich id
        (follow-up S4): the rows its Re-check phase must ask Immich about, one at a time. A row
        marked by this check (excluded, a stacked copy) is not among them: it counts as gone.
        Empty for a superseded lease."""
        ...

    def record_recheck(self, lease: CheckLease, ref: LibraryRef, matches: bool) -> bool:
        """One re-checked row, fenced as `record_step` (False: superseded, nothing written):
        `matches` keeps the row as seen by this check; otherwise the row is removed, the only
        removal of a row the check did not see. Queues the next step."""
        ...

    def finish_check(self, lease: CheckLease, finish: CheckFinish) -> bool:
        """Complete: remove the rows this check marked (the subtraction) and record the time; a
        row it did not see stays, since only `record_recheck` removes one (follow-up S4).
        Incomplete or failed: remove nothing (design rule 2). Fenced as `record_step`."""
        ...

    def frame_outputs(self) -> tuple[FrameOutput, ...]:
        """Every Frame's Frame output (`central.infra.display_store.frame_outputs_in`, its own
        transaction): what the rendition choice must cover (slice A1; read by A4)."""
        ...

    def copy_sizes(self, asset_id: str) -> dict[MeasurableCopy, CopySize]:
        """Every copy of this revision Photo Wall has measured (its `asset_copies` rows; slice
        A2). Empty for an unknown asset id."""
        ...

    def record_copies(self, asset_id: str, measured: Sequence[MeasuredCopy]) -> None:
        """After a fetch: upsert each measured copy's type and size at the database's time, one
        `asset_copies` row per (asset_id, copy). Idempotent; a cache of Immich, never the source
        of truth (a fetch always measures what it downloads). An asset id with no
        `asset_revisions` row writes nothing (slice A2)."""
        ...

    def pool_thumbnail(self, asset_id: str) -> PoolThumbnail | None:
        """The library identity of a row of a wanted pool with this asset id, or None: only such
        an id may be referenced, fetched or served."""
        ...

    def retire_unwanted(self) -> frozenset[str]:
        """Maintenance: drafts older than DRAFT_KEEP_SECONDS go; pools nothing wants go with
        their rows and marks; library-thumbnail records no wanted pool row selects go. Returns
        the thumbnail ids still recorded, so the worker discards every other file."""
        ...


# --- Coordination side -------------------------------------------------------------------------


class ScenePoolReader(Protocol):
    def source_pools_in(self, conn: Any, source_refs: set[str]) -> dict[str, SourcePool]:
        """Each saved Source revision's whole pool (in-pool rows only, no window, no limit, C11),
        hydrated with variants, read in the caller's transaction. A Scene's pool is the union the
        Planner takes over its contributions' refs, one row per asset id (planner `_pool`)."""
        ...
