"""The photo library store over PostgreSQL (roadmap 4; run ledger .claude/runs/photo-4.md).

`PgPhotoLibrary` is the one home of migration 080's tables (library_connections, library_lists,
library_list_items, photo_pools, photo_pool_rows, photo_check_marks, photo_drafts) and the only
code that reads `library_connections.api_key` (for the media worker: `begin_contact`,
`connection_secret`, `begin_check`). It implements central.photo_library.ports'
PhotoLibraryQueries and PhotoLibraryCommands (slice C1), PhotoSourceQueries and the store half of
PhotoSourceCommands (slice C2), and LibraryWorkerStore (C1 contacts and lists, C2 checks).

F0 freezes the constructor only, so the media worker's composition (media/worker.py `_entry`) can
name it before C1 and C2 land; every method arrives with its slice, exactly as the protocols say.
"""

from __future__ import annotations

from central.db import Database, TransactionClock
from central.media_queue import MediaTaskQueue


class PgPhotoLibrary:
    def __init__(self, db: Database, *, times: TransactionClock,
                 queue: MediaTaskQueue | None = None) -> None:
        """`times` stamps and compares every time on the transaction's own connection (G11);
        `queue` queues connection checks and pool check steps in the writing transaction (None:
        a read-only composition, whose queuing methods refuse with media_queue_unconfigured)."""
        raise NotImplementedError("slice C1")
