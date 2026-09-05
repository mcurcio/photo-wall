"""Shared integration database fixture; each test owns only its random schema."""

import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from central.db import Database
from central.registry import Registry
from contracts.time import ManualClock


@pytest.fixture
def registry():
    dsn = os.environ.get("PHOTO_WALL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set PHOTO_WALL_TEST_DATABASE_URL for real PostgreSQL integration")
    schema = "pw_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    try:
        db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
        db.migrate()
        yield Registry(db, ManualClock(1000))
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
