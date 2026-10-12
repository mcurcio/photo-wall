"""The media worker's checks (roadmap 4, run ledger .claude/runs/photo-4.md slice M2).

A connection check (`contact`) asks Immich who and what the key reaches and lands it as the
connection's health; a passing one re-lists albums, people, places and tags. A pool check
(`check_pool`) is ONE bounded step of a check (one Immich page, or the "up to" read), recorded in
the store, which queues the next step: photos join page by page, and only a complete check
removes any (design rule 2). Which photo is what is `media.pool_check`'s pure fold; this module
only reads Immich and the store around it.

Only this package holds a key: it arrives in the lease or the ticket (`ConnectionSecret`) and
reaches nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from central.photo_library.model import ConnectionSecret, ContactAnswer, LibraryProblem
from central.photo_library.ports import (
    CheckComplete,
    CheckFailed,
    CheckFinish,
    CheckIncomplete,
    CheckLease,
    CheckStep,
    LibraryWorkerStore,
)
from contracts.time import Clock
from media import pool_check
from media.library_client import ChosenItemGone, LibraryClient, LibraryUnavailable
from media.library_clients import LibraryClients
from media.pool_check import CheckProgress

logger = logging.getLogger("photo_wall.library")


class LibraryCheckQueue(Protocol):
    """Queues one check task (media/task_queue.py implements it over Procrastinate)."""

    async def connection(self, connection_ref: str) -> None: ...
    async def pool(self, pool_key: str) -> None: ...


def _code(problem: LibraryProblem) -> str:
    """A failed list's error code: the problem's kind, which the store's list status reads."""
    return problem.kind


async def _blocking(function, *args):
    return await asyncio.to_thread(function, *args)


class LibraryChecks:
    def __init__(self, store: LibraryWorkerStore,
                 client_for: Callable[[ConnectionSecret], LibraryClient], clock: Clock, *,
                 queue: LibraryCheckQueue | None = None) -> None:
        self.store, self.client_for, self.clock, self.queue = store, client_for, clock, queue
        self._clients = LibraryClients(client_for, self._close)

    async def _client(self, secret: ConnectionSecret) -> LibraryClient:
        return await self._clients.for_secret(secret)

    # connection checks -----------------------------------------------------------------------

    async def contact(self, connection_ref: str) -> None:
        ticket = await _blocking(self.store.begin_contact, connection_ref)
        if ticket is None:
            return
        client = await self._client(ticket.connection)
        result = await client.contact()
        applied = await _blocking(self.store.record_contact, ticket, result)
        if not applied or not isinstance(result, ContactAnswer):
            return
        for kind, read in (("albums", client.albums), ("people", client.people),
                           ("places", client.places), ("tags", client.tags)):
            try:
                items, error = await read(), None
            except LibraryUnavailable as caught:
                items, error = None, _code(caught.problem)
            await _blocking(self.store.record_list, connection_ref, kind, items, error)

    # pool checks -----------------------------------------------------------------------------

    async def check_pool(self, pool_key: str) -> None:
        lease = await _blocking(self.store.begin_check, pool_key)
        if lease is None:
            return
        progress = (CheckProgress.model_validate(lease.progress) if lease.progress
                    else pool_check.start(lease.query, self.clock.utc()))
        if progress.phase == "done":
            finish: CheckFinish = (CheckIncomplete(reason="over_1000_at_one_instant")
                                   if progress.incomplete else CheckComplete())
            await _blocking(self.store.finish_check, lease, finish)
            return
        try:
            client = await self._client(lease.connection)
            if progress.phase == "recheck":
                await self._recheck(lease, client)
                return
            step = await self._step(lease, progress, client)
        except LibraryUnavailable:
            await _blocking(self.store.finish_check, lease, CheckFailed(problem="connection"))
            return
        except ChosenItemGone:
            await _blocking(self.store.finish_check, lease,
                            CheckFailed(problem="chosen_item_gone"))
            return
        await _blocking(self.store.record_step, lease, step)

    async def _recheck(self, lease: CheckLease, client: LibraryClient) -> None:
        """One step of the Re-check phase (follow-up S4): ask Immich about one row the walk did
        not see, or, with none left, complete the check. A failure raises before anything is
        recorded, so it removes nothing."""
        unseen = await _blocking(self.store.unseen_rows, lease, 1)
        if not unseen:
            await _blocking(self.store.finish_check, lease, CheckComplete())
            return
        matches = await client.still_matches(lease.query, unseen[0])
        await _blocking(self.store.record_recheck, lease, unseen[0], matches)

    async def _step(self, lease: CheckLease, progress: CheckProgress,
                    client: LibraryClient) -> CheckStep:
        query = lease.query
        if progress.phase == "up_to":
            up_to = await client.up_to(query, progress.window)
            months = await client.stacked_buckets(partners=query.partners)
            after = pool_check.advance(progress, query, next_position=None,
                                       stacked_months=[month.month for month in months])
            return CheckStep(progress=after.model_dump(mode="json"), seen_so_far=0,
                             immich_up_to=up_to)
        if progress.phase == "exclusions":
            exclusion = pool_check.exclusions_of(query)[progress.exclusion_index]
            page = await client.exclusion_page(exclusion, progress.position)
            after = pool_check.advance(progress, query, next_position=page.next)
            return CheckStep(progress=after.model_dump(mode="json"),
                             marks=pool_check.excluded(page.ids),
                             seen_so_far=progress.seen_so_far)
        page = await client.search_page(query, progress.window, progress.position)
        items = page.items
        copies: frozenset[str] = frozenset()
        months = pool_check.months_to_read(items, progress.stacked_months)
        if months:
            shown = await client.stack_primaries(months)
            copies = pool_check.stacked_copies(items, progress.stacked_months, shown)
        known = await _blocking(self.store.known_revisions, query.connection_ref,
                                [item.upstream_id for item in items])
        fingerprints = {ref: await client.edits_fingerprint(ref)
                        for ref in pool_check.needs_fingerprint(items, known)}
        entries, marks = pool_check.entries_of(
            items, query=query, bound_user=lease.connection.bound_user,
            connection_ref=query.connection_ref, known=known, fingerprints=fingerprints,
            copies=copies)
        after = pool_check.advance(progress, query, next_position=page.next, seen=len(entries),
                                   over_one_instant=page.over_one_instant)
        return CheckStep(progress=after.model_dump(mode="json"), marks=marks, entries=entries,
                         seen_so_far=after.seen_so_far)

    # the periodic tick -----------------------------------------------------------------------

    async def tick(self) -> None:
        """Queue every due connection check and every due pool check."""
        if self.queue is None:
            raise RuntimeError("library checks have no queue")
        for connection_ref in await _blocking(self.store.due_contacts):
            await self.queue.connection(connection_ref)
        for pool_key in await _blocking(self.store.due_pools):
            await self.queue.pool(pool_key)

    @staticmethod
    async def _close(client: LibraryClient) -> None:
        try:
            async with asyncio.timeout(5):
                await client.close()
        except Exception:
            logger.warning("library client close failed")

    async def close(self) -> None:
        await self._clients.close()
