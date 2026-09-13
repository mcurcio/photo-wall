"""Queue identity and the tag-keyed dispatch port for GitHub release mirroring.

Mirrors `central/media_queue.py`: the constants below name the worker's second
queue and its two tasks, and `ProcrastinateAppReleaseQueue` is the transactional
enqueue port the API's promote route (bead 4) uses to defer a mirror onto that
queue through its own psycopg transaction.

The mirror's `queueing_lock` is the **tag**, never the asset sha256 (0010 P2
fix): two tags built from the same commit share a `.deb` sha256, so a sha-keyed
lock would drop the second tag's mirror as `AlreadyEnqueued` and strand it. A
tag-keyed lock keeps each promoted tag's mirror distinct while still coalescing
repeat promotes of the *same* tag into one in-flight job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import procrastinate

APP_RELEASE_QUEUE = "photo-wall-app-release"
POLL_RELEASES_TASK = "photo_wall.app_release.poll"
MIRROR_RELEASE_TASK = "photo_wall.app_release.mirror"

# 0010 gate #3: poll cadence is a placeholder (~900s) behind a named constant.
# Overridable per deployment via PHOTO_WALL_RELEASE_POLL_SECONDS (read where the
# periodic task is registered). Nothing structural depends on the exact value.
POLL_SECONDS = 900

# The poller self-coalesces: a still-running poll blocks the next tick's defer.
POLL_QUEUEING_LOCK = "app-release-poll"


def poll_cron(seconds: int = POLL_SECONDS) -> str:
    """A six-field (seconds-last) cron firing every `seconds`, minute-granular.

    croniter places seconds last in a six-field expression (see
    media/task_queue.py); the trailing `0` pins the fire to second 0 so a
    minute-granular cadence does not fan out across a whole minute. Sub-minute
    cadences are clamped to one minute -- the release poll is deliberately slow.
    """
    minutes = max(1, int(seconds) // 60)
    return f"*/{minutes} * * * * 0"


@dataclass(frozen=True, slots=True)
class QueueReceipt:
    coalesced: bool


class AppReleaseTaskQueue(Protocol):
    """The producer-side enqueue port the API's operator routes (bead 4) depend on.

    The promote route defers a tag-keyed mirror; the refresh route defers a
    tag-less, coalesced poll. Depending on this abstraction (not the concrete
    Procrastinate class) lets `create_app` inject a fake in tests with no worker
    or network, mirroring `MediaTaskQueue`.
    """

    def enqueue_mirror_in(self, conn: Any, tag: str) -> "QueueReceipt": ...
    def enqueue_poll_in(self, conn: Any) -> "QueueReceipt": ...


class ProcrastinateAppReleaseQueue:
    """Defer a tag-keyed mirror (or an on-demand poll) through the caller's txn."""

    def __init__(self, dsn: str):
        connector = procrastinate.SyncPsycopgConnector(conninfo=dsn)
        self.app = procrastinate.App(connector=connector)

    def enqueue_mirror_in(self, conn: Any, tag: str) -> QueueReceipt:
        """Enqueue `mirror_release(tag)`; coalesce a duplicate onto the in-flight job.

        The `queueing_lock` is the tag: a repeat promote of the same tag folds
        into the existing job (`coalesced=True`), while a sibling tag sharing the
        same sha256 gets its own distinct job.
        """
        try:
            self.app.configure_task(
                MIRROR_RELEASE_TASK,
                queue=APP_RELEASE_QUEUE,
                queueing_lock=tag,
                connection=conn,
            ).defer(tag=tag)
            return QueueReceipt(coalesced=False)
        except procrastinate.exceptions.AlreadyEnqueued:
            return QueueReceipt(coalesced=True)

    def enqueue_poll_in(self, conn: Any) -> QueueReceipt:
        """Enqueue an on-demand `poll_releases`; coalesce onto the pending poll.

        Shares the periodic poll's `queueing_lock` (`POLL_QUEUEING_LOCK`), so an
        operator refresh folds into an already-queued tick instead of stacking a
        second poll -- the same coalescing the periodic task relies on. The task
        ignores its `timestamp` argument, so `0` is a harmless on-demand marker.
        """
        try:
            self.app.configure_task(
                POLL_RELEASES_TASK,
                queue=APP_RELEASE_QUEUE,
                queueing_lock=POLL_QUEUEING_LOCK,
                connection=conn,
            ).defer(timestamp=0)
            return QueueReceipt(coalesced=False)
        except procrastinate.exceptions.AlreadyEnqueued:
            return QueueReceipt(coalesced=True)
