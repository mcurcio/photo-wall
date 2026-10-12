"""An in-memory `LibraryWorkerStore` (central.photo_library.ports) for the media worker's tests.

It behaves as the store's protocol docstrings say, and I1's conformance suite
(tests/test_library_store_conformance.py) holds it equal to the PostgreSQL store: contacts are
fenced by their number and judged by `policy.contact_health`; a check's steps are fenced by the
check number; marks subtract (an id marked in a check never becomes a row of that check); a
complete finish removes the rows the check marked, and a row it did not see leaves only through
`record_recheck` (follow-up S4); any other finish removes nothing.
Photos are held as PostgreSQL holds them (slice A2): one `assets` record per Immich asset, its
`revisions` by asset id, and each revision's measured `copies`.

Time is `clock.utc()` (a ManualClock in tests), standing in for the database's clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import SecretStr

from central.installation_models import FrameOutput
from central.photo_library.model import (
    CheckMark,
    ConnectionSecret,
    ContactAnswer,
    ContactResult,
    ContactTicket,
    CopySize,
    HealthOk,
    ImmichUser,
    InPool,
    LibraryAsset,
    LibraryCounts,
    LibraryHealth,
    ListKind,
    MeasurableCopy,
    MeasuredCopy,
    NotChecked,
    OriginalFile,
    PoolEntry,
    PoolThumbnail,
    SourceQuery,
)
from central.photo_library.policy import (
    CHECK_EVERY_SECONDS,
    STALE_CHECK_SECONDS,
    contact_health,
)
from central.photo_library.ports import (
    CheckComplete,
    CheckFinish,
    CheckLease,
    CheckStep,
    KnownRevision,
)


@dataclass
class _Connection:
    url: str
    key: str | None
    user: ImmichUser | None = None
    health: LibraryHealth = field(default_factory=NotChecked)
    counts: LibraryCounts | None = None
    taken: int = 0          # the last contact number handed out
    applied: int = 0        # the last contact number applied
    contact_started: float | None = None
    lists: dict = field(default_factory=dict)
    list_errors: dict = field(default_factory=dict)


@dataclass
class LibraryAssetRecord:
    """A `library_assets` row: one per Immich asset (connection, Immich id)."""
    kind: str
    taken_at: float
    original: OriginalFile | None
    first_seen: float
    last_seen: float


@dataclass
class RevisionRecord:
    """An `asset_revisions` row: the revision's own facts, keyed by its asset id."""
    connection_ref: str
    upstream_id: str
    revision: str
    width: int
    height: int
    duration: float | None
    updated_at: str | None
    last_seen: float


@dataclass
class _Pool:
    query: SourceQuery
    wanted: bool = True
    check_number: int = 0
    checking: bool = False
    started_at: float | None = None
    step_at: float | None = None
    progress: dict | None = None
    finished_at: float | None = None
    last_complete_at: float | None = None
    outcome: CheckFinish | None = None
    requested: bool = True
    seen_so_far: int = 0
    up_to: int | None = None
    rows: dict[str, tuple[PoolEntry, int]] = field(default_factory=dict)
    marks: dict[str, tuple[CheckMark, int]] = field(default_factory=dict)


class FakeLibraryStore:
    def __init__(self, clock, *, zone: str = "UTC") -> None:
        """`zone`: the house zone a check's lease carries (PgPhotoLibrary reads Settings › House)."""
        self.clock = clock
        self.zone = zone
        self.connections: dict[str, _Connection] = {}
        self.pools: dict[str, _Pool] = {}
        self.assets: dict[tuple[str, str], LibraryAssetRecord] = {}
        self.revisions: dict[str, RevisionRecord] = {}
        self.copies: dict[str, dict[str, CopySize]] = {}
        self.queued: list[str] = []               # pool keys whose next step was queued
        self.outputs: tuple[FrameOutput, ...] = ()   # what `frame_outputs` answers

    # seeding and reading (tests only) --------------------------------------------------------

    def add_connection(self, ref: str, url: str, key: str | None) -> None:
        self.connections[ref] = _Connection(url=url, key=key)

    def add_pool(self, query: SourceQuery) -> str:
        key = query.pool_key()
        self.pools.setdefault(key, _Pool(query=query))
        return key

    def rows(self, pool_key: str) -> dict[str, PoolEntry]:
        return {ref: entry for ref, (entry, _) in self.pools[pool_key].rows.items()}

    def marks(self, pool_key: str) -> dict[str, str]:
        return {ref: mark.mark for ref, (mark, _) in self.pools[pool_key].marks.items()}

    def connection(self, ref: str) -> _Connection:
        return self.connections[ref]

    def _now(self) -> float:
        return self.clock.utc()

    def _secret(self, ref: str) -> ConnectionSecret | None:
        connection = self.connections.get(ref)
        if connection is None or connection.key is None:
            return None
        return ConnectionSecret(connection_ref=ref, base_url=connection.url,
                                api_key=SecretStr(connection.key),
                                bound_user=connection.user.user_ref if connection.user else None)

    # contacts and lists ----------------------------------------------------------------------

    def due_contacts(self) -> tuple[str, ...]:
        now = self._now()
        return tuple(sorted(ref for ref, c in self.connections.items() if c.key is not None and (
            c.contact_started is None or now - c.contact_started >= CHECK_EVERY_SECONDS)))

    def begin_contact(self, connection_ref: str) -> ContactTicket | None:
        secret = self._secret(connection_ref)
        if secret is None:
            return None
        connection = self.connections[connection_ref]
        connection.taken += 1
        connection.contact_started = self._now()
        return ContactTicket(contact_number=connection.taken, connection=secret)

    def connection_secret(self, connection_ref: str) -> ConnectionSecret | None:
        return self._secret(connection_ref)

    def record_contact(self, ticket: ContactTicket, result: ContactResult) -> bool:
        connection = self.connections.get(ticket.connection.connection_ref)
        if connection is None or ticket.contact_number <= connection.applied:
            return False
        connection.applied = ticket.contact_number
        connection.health = contact_health(result, bound_user=connection.user,
                                           previous=connection.health, checked_at=self._now())
        if isinstance(connection.health, HealthOk) and isinstance(result, ContactAnswer):
            connection.user = connection.user or result.user
            connection.counts = result.counts
        return True

    def record_list(self, connection_ref: str, kind: ListKind, items: Sequence | None,
                    error: str | None) -> None:
        if (items is None) == (error is None):
            raise ValueError("exactly one of items and error")
        connection = self.connections[connection_ref]
        if items is not None:
            connection.lists[kind] = tuple(items)
            connection.list_errors.pop(kind, None)
        else:
            connection.list_errors[kind] = error

    # checks ----------------------------------------------------------------------------------

    def due_pools(self) -> tuple[str, ...]:
        now = self._now()
        def due(pool: _Pool) -> bool:
            if pool.checking:
                return pool.step_at is None or now - pool.step_at >= STALE_CHECK_SECONDS
            return (pool.requested or pool.finished_at is None
                    or now - pool.finished_at >= CHECK_EVERY_SECONDS)

        return tuple(sorted(key for key, pool in self.pools.items() if pool.wanted
                            and self._secret(pool.query.connection_ref) is not None
                            and due(pool)))

    def begin_check(self, pool_key: str) -> CheckLease | None:
        pool = self.pools.get(pool_key)
        if pool is None or not pool.wanted:
            return None
        secret = self._secret(pool.query.connection_ref)
        if secret is None:
            return None
        if not pool.checking:
            pool.check_number += 1
            pool.checking, pool.started_at, pool.progress = True, self._now(), None
            pool.requested, pool.seen_so_far = False, 0
        pool.step_at = self._now()
        return CheckLease(pool_key=pool_key, query=pool.query, connection=secret,
                          check_number=pool.check_number, started_at=pool.started_at,
                          zone=self.zone, progress=pool.progress)

    def known_revisions(self, connection_ref: str,
                        upstream_ids: Sequence[str]) -> dict[str, KnownRevision]:
        known: dict[str, RevisionRecord] = {}
        for revision in self.revisions.values():   # the latest seen with updatedAt, per id
            if (revision.connection_ref == connection_ref and revision.updated_at is not None
                    and revision.upstream_id in upstream_ids):
                kept = known.get(revision.upstream_id)
                if kept is None or revision.last_seen >= kept.last_seen:
                    known[revision.upstream_id] = revision
        return {ref: KnownRevision(updated_at=r.updated_at, revision=r.revision)
                for ref, r in known.items()}

    def library_asset(self, asset_id: str) -> LibraryAsset | None:
        """The revision as a LibraryAsset (`library_assets_in`'s answer); None when unknown."""
        revision = self.revisions.get(asset_id)
        if revision is None:
            return None
        parent = self.assets[(revision.connection_ref, revision.upstream_id)]
        return LibraryAsset(connection_ref=revision.connection_ref,
                            upstream_id=revision.upstream_id, revision=revision.revision,
                            kind=parent.kind, taken_at=parent.taken_at, width=revision.width,
                            height=revision.height, duration=revision.duration,
                            original=parent.original)

    def _record_asset(self, connection_ref: str, upstream_id: str, kind: str, taken_at: float,
                      original: OriginalFile | None, *, reads_original: bool) -> None:
        now = self._now()
        parent = self.assets.get((connection_ref, upstream_id))
        if parent is None:
            self.assets[(connection_ref, upstream_id)] = LibraryAssetRecord(
                kind, taken_at, original if reads_original else None, now, now)
            return
        parent.kind, parent.taken_at, parent.last_seen = kind, taken_at, now
        if reads_original:
            parent.original = original

    def _fenced(self, lease: CheckLease) -> _Pool | None:
        pool = self.pools.get(lease.pool_key)
        if pool is None or not pool.checking or pool.check_number != lease.check_number:
            return None
        return pool

    def record_step(self, lease: CheckLease, step: CheckStep) -> bool:
        pool = self._fenced(lease)
        if pool is None:
            return False
        for mark in step.marks:
            pool.marks[mark.upstream_id] = (mark, lease.check_number)
        marked = {ref for ref, (_, number) in pool.marks.items() if number == lease.check_number}
        for entry in step.entries:
            ref = entry.asset.upstream_id if isinstance(entry, InPool) else entry.upstream_id
            if ref in marked:
                continue
            if isinstance(entry, InPool):
                asset = entry.asset
                self._record_asset(asset.connection_ref, ref, asset.kind, asset.taken_at,
                                   asset.original, reads_original=True)
                self.revisions[asset.asset_id] = RevisionRecord(
                    asset.connection_ref, ref, asset.revision, asset.width, asset.height,
                    asset.duration, entry.updated_at, self._now())
            elif entry.kind is not None and entry.taken_at is not None:
                self._record_asset(pool.query.connection_ref, ref, entry.kind, entry.taken_at,
                                   None, reads_original=False)
            pool.rows[ref] = (entry, lease.check_number)
        pool.progress, pool.seen_so_far = step.progress, step.seen_so_far
        if step.immich_up_to is not None:
            pool.up_to = step.immich_up_to
        self.queued.append(lease.pool_key)
        return True

    @staticmethod
    def _marked(pool: _Pool, lease: CheckLease) -> set[str]:
        return {ref for ref, (_, number) in pool.marks.items() if number == lease.check_number}

    def unseen_rows(self, lease: CheckLease, limit: int) -> tuple[str, ...]:
        pool = self._fenced(lease)
        if pool is None:
            return ()
        marked = self._marked(pool, lease)
        return tuple(sorted(ref for ref, (_, seen) in pool.rows.items()
                            if seen < lease.check_number and ref not in marked))[:limit]

    def record_recheck(self, lease: CheckLease, ref: str, matches: bool) -> bool:
        pool = self._fenced(lease)
        if pool is None:
            return False
        if ref in pool.rows:
            if matches:
                pool.rows[ref] = (pool.rows[ref][0], lease.check_number)
            else:
                del pool.rows[ref]
        self.queued.append(lease.pool_key)
        return True

    def finish_check(self, lease: CheckLease, finish: CheckFinish) -> bool:
        pool = self._fenced(lease)
        if pool is None:
            return False
        now = self._now()
        if isinstance(finish, CheckComplete):
            marked = self._marked(pool, lease)
            pool.rows = {ref: row for ref, row in pool.rows.items() if ref not in marked}
            pool.marks = {ref: mark for ref, mark in pool.marks.items()
                          if mark[1] >= lease.check_number}
            pool.last_complete_at = now
        pool.checking, pool.progress, pool.finished_at, pool.outcome = False, None, now, finish
        return True

    def frame_outputs(self) -> tuple[FrameOutput, ...]:
        return self.outputs

    def copy_sizes(self, asset_id: str) -> dict[MeasurableCopy, CopySize]:
        return dict(self.copies.get(asset_id, {}))

    def record_copies(self, asset_id: str, measured: Sequence[MeasuredCopy]) -> None:
        if asset_id not in self.revisions:
            return
        copies = self.copies.setdefault(asset_id, {})
        for copy in measured:
            copies[copy.name] = CopySize(name=copy.name, media_type=copy.media_type,
                                         width=copy.width, height=copy.height,
                                         measured_at=self._now())

    def delete_revision(self, asset_id: str) -> None:
        """Seeding: a revision row deleted (its copies cascade)."""
        self.revisions.pop(asset_id, None)
        self.copies.pop(asset_id, None)

    def pool_thumbnail(self, asset_id: str) -> PoolThumbnail | None:
        for pool in self.pools.values():
            if not pool.wanted:
                continue
            for entry, _ in pool.rows.values():
                if isinstance(entry, InPool) and entry.asset.asset_id == asset_id:
                    return PoolThumbnail(connection_ref=entry.asset.connection_ref,
                                         upstream_id=entry.asset.upstream_id,
                                         revision=entry.asset.revision)
        return None

    def retire_unwanted(self) -> frozenset[str]:
        self.pools = {key: pool for key, pool in self.pools.items() if pool.wanted}
        return frozenset(entry.asset.asset_id for pool in self.pools.values()
                         for entry, _ in pool.rows.values() if isinstance(entry, InPool))


async def check_to_end(checks, store: FakeLibraryStore, pool_key: str, *,
                       steps: int = 10_000) -> None:
    """Drive one pool check step by step, as the queue would, until it finishes."""
    for _ in range(steps):
        await checks.check_pool(pool_key)
        if not store.pools[pool_key].checking:
            return
    raise AssertionError("the check never finished")


def immich_checks(base_url: str, api_key: str, clock, *, page_size: int = 1000):
    """The real `ImmichClient` behind `LibraryChecks`, over this fake store holding one
    connection "main" (the named tests against the Immich fixture, roadmap 4 slice M2)."""
    from media.immich import ImmichClient, LibraryLimits
    from media.library_checks import LibraryChecks

    store = FakeLibraryStore(clock)
    store.add_connection("main", base_url, api_key)
    limits = LibraryLimits(page_size=page_size)
    checks = LibraryChecks(store, lambda secret: ImmichClient.for_connection(
        secret, limits=limits, clock=clock), clock)
    return store, checks
