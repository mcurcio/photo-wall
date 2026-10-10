"""`PgReleaseRecords` round-trips every method against a migrated schema.

The DB tests use the `registry` fixture, which skips without PHOTO_WALL_TEST_DATABASE_URL (CI runs
them). The fake-transaction guard runs without a database. `fleet_desired_assets` is read through
the catalog in `test_node_release_catalog.py` and `test_node_release_ingest.py`.
"""

from __future__ import annotations

import pytest
from fakes.transactions import FakeTransaction

from central.content_catalog.ports import StoredEtag
from central.infra.catalog_records import PgReleaseRecords
from central.infra.transactions import PgTransactions


@pytest.fixture
def pg(registry):
    return PgTransactions(registry.db)


# -- no database: a fake transaction handed to a real repository --------------------------------


FAKE_TRANSACTION_CALLS = {
    "PgReleaseRecords.fleet_desired_assets": lambda tx: PgReleaseRecords().fleet_desired_assets(tx),
    "PgReleaseRecords.load_etag": lambda tx: PgReleaseRecords().load_etag(tx),
    "PgReleaseRecords.store_etag": lambda tx: PgReleaseRecords().store_etag(tx, "e", now=1.0),
}


def test_a_fake_transaction_is_a_type_error():
    # Every call is tried, so one run names every offender: one that accepts the fake, and one
    # that refuses it with anything but a TypeError.
    offenders = []
    for name, call in FAKE_TRANSACTION_CALLS.items():
        try:
            call(FakeTransaction())
        except TypeError:
            continue
        except Exception as error:
            offenders.append(f"{name}: {type(error).__name__}")
        else:
            offenders.append(f"{name}: accepted")
    assert offenders == []


# -- the listing's ETag ------------------------------------------------------------------------


def test_etag_round_trip_records_when_it_was_stored(registry, pg):
    releases = PgReleaseRecords()
    with pg.begin() as tx:
        assert releases.load_etag(tx) is None
        releases.store_etag(tx, 'W/"abc"', now=1000.0)
    with pg.begin() as tx:
        assert releases.load_etag(tx) == StoredEtag('W/"abc"', 1000.0)
        releases.store_etag(tx, None, now=2000.0)
        assert releases.load_etag(tx) == StoredEtag(None, 2000.0)
    with registry.db.transaction() as conn:  # an ETag no code of this version stored
        conn.execute("UPDATE app_release_poll SET etag='W/\"old\"', etag_stored_at=NULL")
    with pg.begin() as tx:
        assert releases.load_etag(tx) is None
