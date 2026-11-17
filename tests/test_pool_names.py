"""Every pool and connection Central and its worker open names itself in
`pg_stat_activity.application_name` (decision 0034), through `central.db.with_application_name`,
the one way any of them is named.

Mutation probe (P1): make `Database` ignore `application_name` -> the transaction reads another
name."""
from __future__ import annotations

import asyncio

import pytest
from fakes.transactions import FakeTransactions
from psycopg.conninfo import conninfo_to_dict
from runtime_fakes import catalog_stubs

from central.db import Database, with_application_name
from central.infra.asset_records import PgAssetRecords
from central.infra.outcomes import JobOutcomes
from central.infra.publisher import ProcrastinatePublisher
from central.infra.queue_ops import QueueAdmin
from central.infra.runtime import JobRuntime
from central.kernel.jobs import QueueName
from contracts.time import ManualClock

NAME_SQL = "SELECT current_setting('application_name') AS name, pg_backend_pid() AS pid"


def test_the_name_replaces_a_given_one_and_keeps_every_other_parameter():
    named = with_application_name(
        "postgresql://wall:secret@db.example:6543/photos?sslmode=require&application_name=old"
        "&connect_timeout=3", "photo-wall-web")
    assert conninfo_to_dict(named) == {
        "user": "wall", "password": "secret", "host": "db.example", "port": "6543",
        "dbname": "photos", "sslmode": "require", "connect_timeout": "3",
        "application_name": "photo-wall-web"}


@pytest.mark.db
def test_a_database_pool_connects_under_its_name(registry):
    db = Database(registry.db.dsn, application_name="photo-wall-web")
    try:
        with db.transaction() as conn:
            row = conn.execute(NAME_SQL).fetchone()
            seen = conn.execute("SELECT application_name FROM pg_stat_activity WHERE pid=%s",
                                (row["pid"],)).fetchone()["application_name"]
        assert (row["name"], seen) == ("photo-wall-web", "photo-wall-web")
    finally:
        db.close()


def _job_runtime(dsn: str) -> JobRuntime:
    return JobRuntime(dsn, catalog_stubs(), {QueueName.FETCH: 1, QueueName.UPKEEP: 1,
                                             QueueName.TRANSCODE: 1},
                      transactions=FakeTransactions(), assets=PgAssetRecords(ManualClock(0.0)),
                      clock=ManualClock(0.0))


async def _async_name(connector) -> str:
    await connector.open_async()
    try:
        row = await connector.execute_query_one_async(
            "SELECT application_name AS name FROM pg_stat_activity WHERE pid=pg_backend_pid()")
        return row["name"]
    finally:
        await connector.close_async()


@pytest.mark.db
def test_the_job_runtime_and_queue_admin_connect_under_their_names(registry):
    dsn = registry.db.dsn
    runtime = _job_runtime(dsn)
    assert asyncio.run(_async_name(runtime._app.connector)) == "photo-wall-jobs"
    admin = QueueAdmin(dsn)
    assert asyncio.run(_async_name(admin._app.connector)) == "photo-wall-queue-ops"


@pytest.mark.db
def test_the_publisher_connects_under_its_name(registry):
    publisher = ProcrastinatePublisher(registry.db.dsn, transactions=FakeTransactions(),
                                       outcomes=JobOutcomes(),
                                       assets=PgAssetRecords(ManualClock(0.0)),
                                       clock=ManualClock(0.0), feed=None)
    connector = publisher._app.connector
    connector.open()
    try:
        row = connector.execute_query_one(
            "SELECT application_name AS name FROM pg_stat_activity WHERE pid=pg_backend_pid()")
    finally:
        connector.close()
    assert row["name"] == "photo-wall-publisher"
