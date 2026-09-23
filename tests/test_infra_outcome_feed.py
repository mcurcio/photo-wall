"""The process-wide outcome feed.

The "recheck" cases run the feed over the real `job_outcomes` (PostgreSQL) with an unreachable
LISTEN DSN, so only the recheck can resolve a waiter; the others cover the real LISTEN connection.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import psycopg
import pytest
from fakes.transactions import FakeTransactions

from central.infra.outcome_feed import APPLICATION_NAME, OutcomeFeed
from central.infra.outcomes import JobOutcomes
from central.infra.transactions import PgTransactions
from central.kernel.job_types import FetchPackage, SyncReleases
from central.kernel.jobs import job_keys

UNREACHABLE = "postgresql://127.0.0.1:1/none?connect_timeout=1"
LOCK = job_keys(SyncReleases()).lock
SOON = timedelta(seconds=5)


def record(outcomes, transactions, job=None, status="ok", reason=None, now=1.0):
    with transactions.begin() as tx:
        return outcomes.record(tx, job or SyncReleases(), status=status, reason=reason,
                               retry_not_before=None, now=now)


class Local:
    def __init__(self, registry, recheck=timedelta(milliseconds=50)):
        self.transactions = PgTransactions(registry.db)
        self.outcomes = JobOutcomes()
        self.feed = OutcomeFeed(UNREACHABLE, transactions=self.transactions,
                                outcomes=self.outcomes, recheck=recheck)

    def run(self, scenario):
        async def main():
            await self.feed.start()
            try:
                return await scenario()
            finally:
                await self.feed.stop()

        logging.getLogger("central.infra.outcome_feed").setLevel(logging.ERROR)
        return asyncio.run(main())


def test_wait_requires_a_started_feed():
    feed = OutcomeFeed(UNREACHABLE, transactions=FakeTransactions(), outcomes=JobOutcomes())
    with pytest.raises(RuntimeError, match="not_started"):
        asyncio.run(feed.wait_for(LOCK, since=0, timeout=SOON))


def test_recheck_must_be_positive():
    with pytest.raises(ValueError):
        OutcomeFeed(UNREACHABLE, transactions=FakeTransactions(), outcomes=JobOutcomes(),
                    recheck=timedelta(0))


def test_a_stored_newer_outcome_returns_at_once(registry):
    local = Local(registry, recheck=timedelta(hours=1))
    row = record(local.outcomes, local.transactions)
    assert local.run(lambda: local.feed.wait_for(LOCK, since=0, timeout=timedelta(0))) == row
    assert local.feed._entries == {}


def test_an_older_outcome_times_out_to_none(registry):
    local = Local(registry)
    row = record(local.outcomes, local.transactions)
    assert local.run(lambda: local.feed.wait_for(
        LOCK, since=row.seq, timeout=timedelta(milliseconds=120))) is None
    assert local.feed._entries == {}


def test_the_recheck_resolves_waiters_without_any_notify(registry):
    local = Local(registry)

    async def scenario():
        waits = [asyncio.create_task(local.feed.wait_for(LOCK, since=0, timeout=SOON))
                 for _ in range(50)]
        await asyncio.sleep(0.02)
        assert list(local.feed._entries) == [LOCK]  # 50 waiters, one entry
        assert local.feed._entries[LOCK].waiters == 50
        row = record(local.outcomes, local.transactions)
        return row, await asyncio.gather(*waits)

    row, rows = local.run(scenario)
    assert rows == [row] * 50
    assert local.feed._entries == {}


def test_cancelling_a_waiter_only_unregisters_it(registry):
    local = Local(registry)

    async def scenario():
        cancelled = asyncio.create_task(local.feed.wait_for(LOCK, since=0, timeout=SOON))
        kept = asyncio.create_task(local.feed.wait_for(LOCK, since=0, timeout=SOON))
        await asyncio.sleep(0.02)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert local.feed._entries[LOCK].waiters == 1
        row = record(local.outcomes, local.transactions)
        assert await kept == row

    local.run(scenario)
    assert local.feed._entries == {}


def test_waiters_with_different_since_share_the_entry(registry):
    local = Local(registry)
    first = record(local.outcomes, local.transactions)

    async def scenario():
        old = asyncio.create_task(local.feed.wait_for(LOCK, since=0, timeout=SOON))
        new = asyncio.create_task(local.feed.wait_for(LOCK, since=first.seq, timeout=SOON))
        assert await old == first
        await asyncio.sleep(0.1)
        assert not new.done()
        second = record(local.outcomes, local.transactions, now=2.0)
        assert await new == second

    local.run(scenario)


# -- PostgreSQL (CI) --------------------------------------------------------------------------------


def listeners(dsn):
    """Pids of EVERY feed LISTEN connection on this database, owned by this test or not.

    CI runs the suite beside the Compose `central` service, whose own feed holds one; compare
    against `before` (the pids present before this test's feed started), never a raw count.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        return {row[0] for row in conn.execute(
            "SELECT pid FROM pg_stat_activity WHERE application_name = %s "
            "AND datname = current_database()", (APPLICATION_NAME,)).fetchall()}


def own_listeners(dsn, before):
    return listeners(dsn) - before


def pg_feed(registry, recheck):
    transactions, outcomes = PgTransactions(registry.db), JobOutcomes()
    feed = OutcomeFeed(registry.db.dsn, transactions=transactions, outcomes=outcomes,
                       recheck=recheck)
    return feed, transactions, outcomes


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.02)


def test_fifty_waiters_share_one_listen_connection_and_one_entry(registry):
    # A long recheck: only the NOTIFY can resolve these waiters in time.
    feed, transactions, outcomes = pg_feed(registry, timedelta(minutes=5))
    job = FetchPackage(sha256="ab" * 32)
    lock = job_keys(job).lock
    dsn = registry.db.dsn
    # Another process's feed on the same database (CI: the Compose `central`); not ours to count.
    foreign = psycopg.connect(dsn, autocommit=True, application_name=APPLICATION_NAME)
    before = listeners(dsn)

    async def scenario():
        await feed.start()
        try:
            await until(lambda: len(own_listeners(dsn, before)) == 1)
            waits = [asyncio.create_task(feed.wait_for(lock, since=0, timeout=SOON))
                     for _ in range(50)]
            await until(lambda: lock in feed._entries and feed._entries[lock].waiters == 50)
            assert len(feed._entries) == 1
            await asyncio.sleep(0.2)  # every waiter has made its initial read
            row = await asyncio.to_thread(record, outcomes, transactions, job)
            rows = await asyncio.wait_for(asyncio.gather(*waits), 3)
            assert len(own_listeners(dsn, before)) == 1
            return row, rows
        finally:
            await feed.stop()

    try:
        row, rows = asyncio.run(scenario())
    finally:
        foreign.close()
    assert rows == [row] * 50
    assert feed._entries == {}


def test_a_notify_before_the_waiter_registered_is_seen_through_the_stored_row(registry):
    feed, transactions, outcomes = pg_feed(registry, timedelta(minutes=5))

    async def scenario():
        await feed.start()
        try:
            row = await asyncio.to_thread(record, outcomes, transactions)  # nobody listening
            await asyncio.sleep(0.2)
            return row, await feed.wait_for(LOCK, since=0, timeout=timedelta(0))
        finally:
            await feed.stop()

    row, seen = asyncio.run(scenario())
    assert seen == row


def test_the_recheck_resolves_a_waiter_after_the_listen_connection_drops(registry):
    feed, transactions, outcomes = pg_feed(registry, timedelta(milliseconds=500))
    dsn = registry.db.dsn
    before = listeners(dsn)

    async def scenario():
        await feed.start()
        try:
            await until(lambda: len(own_listeners(dsn, before)) == 1)
            (pid,) = own_listeners(dsn, before)
            wait = asyncio.create_task(feed.wait_for(LOCK, since=0, timeout=SOON))
            await until(lambda: LOCK in feed._entries)
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,))  # only OUR feed's
                # Written WITHOUT a NOTIFY: only a re-read can find it.
                admin.execute(
                    "INSERT INTO job_outcomes (lock_key, job_name, status, seq, updated_at) "
                    "VALUES (%s, 'releases.sync', 'ok', nextval('job_outcome_seq'), 1.0)",
                    (LOCK,))
            return await asyncio.wait_for(wait, 3)
        finally:
            await feed.stop()

    row = asyncio.run(scenario())
    assert (row.lock_key, row.status) == (LOCK, "ok")
