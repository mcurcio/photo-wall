"""Every writer of a desired-set input waits on the asset-roots lock (`central/infra/asset_roots.py`).

The cache cleaner holds that lock from its desired read through its unlinks
(`CacheRetention.hold_desired`); a writer that skipped it could commit a root the cleaner then
deletes. Each case holds the lock in a blocker transaction and runs the writer with a short
`lock_timeout`: it must time out (it queued on the lock), and run through once the lock is free.
The classification test makes a new records method name its side, so none is added unlocked.
"""

from __future__ import annotations

import inspect

import psycopg
import pytest

from central.infra.asset_roots import FLEET_ASSET_LOCK
from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
from central.infra.node_releases import PgNodeReleaseRecords
from central.infra.transactions import PgTransaction
from central.kernel.assets import OriginLocator
from central.kernel.ports import PublishedRelease, UpstreamVersion
from central.netboot_base import record_base_health
from contracts.models import BaseHealth
from contracts.time import ManualClock

TAG = "v2.0.0"
DEB = OriginLocator("https://example.test/v2.0.0.deb", "d" * 64, 10)


def _release(at: float = 1.0) -> PublishedRelease:
    return PublishedRelease(TAG, False, DEB, None, None, UpstreamVersion(at, 1))


def _node_problem() -> PublishedRelease:
    return PublishedRelease(TAG, False, None, "no_player_asset", None, None,
                            node_problem="node_release_invalid",
                            node_version=UpstreamVersion(1.0, 1), legacy=False)


def _seed(conn) -> None:
    """The release and its promotion, for the writers that update existing rows."""
    PgReleaseRecords().claim(PgTransaction(conn), _release(), now=1.0)
    PgReleaseRecords().set_promoted(PgTransaction(conn), TAG, by="auto")


# name -> (needs the seed, the write over a raw connection)
WRITERS = {
    "PgNodeReleaseRecords.record_problem": (False, lambda conn: PgNodeReleaseRecords(
        ManualClock(1.0)).record_problem(PgTransaction(conn), _node_problem(),
                                         "node_release_invalid", now=1.0)),
    "PgReleaseRecords.claim": (False, lambda conn: PgReleaseRecords().claim(
        PgTransaction(conn), _release(), now=1.0)),
    "PgReleaseRecords.apply": (True, lambda conn: PgReleaseRecords().apply(
        PgTransaction(conn), _release(2.0), divergent=False, now=2.0)),
    "PgReleaseRecords.set_promoted": (True, lambda conn: PgReleaseRecords().set_promoted(
        PgTransaction(conn), TAG, by="operator")),
    "PgReleaseRecords.set_last_good": (True, lambda conn: PgReleaseRecords().set_last_good(
        PgTransaction(conn), TAG)),
    "PgDeviceRecords.set_pin": (True, lambda conn: PgDeviceRecords().set_pin(
        PgTransaction(conn), "d-1", TAG)),
    "PgDeviceRecords.record_served": (True, lambda conn: PgDeviceRecords().record_served(
        PgTransaction(conn), "d-1", TAG, now=1.0)),
    "netboot_base.record_base_health": (True, lambda conn: record_base_health(
        conn, "d-1", BaseHealth(authority_epoch=1, sequence=1, running_tag=TAG, healthy=True),
        clock=ManualClock(1.0))),
}

# Methods that write no desired-set input, or take the lock themselves before any write
# (`ingest` and `select_first` are covered by test_node_release_ingest.py's race test).
UNLOCKED_OR_SELF_LOCKING = {
    "PgReleaseRecords": {"get", "all", "fleet_desired_assets", "payload_abi_for", "shipping",
                         "promoted_tag", "promotion", "last_good_tag", "load_etag",
                         "store_etag", "lock_auto_promotion", "bound_player_count"},
    # `lock`, `apply` and `sweep_failed_boots` write serial, last_seen, failed_tag and
    # boot_outcome: no desired-set input.
    "PgDeviceRecords": {"lock", "active", "known_good_tags", "named_tags", "names_any", "get",
                        "apply", "sweep_failed_boots"},
    "PgNodeReleaseRecords": {"ingest", "selection_exists", "stable_deployments",
                             "select_first"},
}


def test_every_records_method_is_classified():
    """A new method must be listed as a locked writer (WRITERS) or as not writing a root."""
    for cls in (PgReleaseRecords, PgDeviceRecords, PgNodeReleaseRecords):
        public = {name for name, _ in inspect.getmembers(cls, inspect.isfunction)
                  if not name.startswith("_")}
        locked = {key.split(".", 1)[1] for key in WRITERS if key.startswith(cls.__name__ + ".")}
        assert public == locked | UNLOCKED_OR_SELF_LOCKING[cls.__name__], cls.__name__


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_desired_set_writer_waits_for_the_cleaner(registry, name):
    seeded, write = WRITERS[name]
    if seeded:
        with registry.db.transaction() as conn:
            _seed(conn)
            conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                         "VALUES('d-1','s-1',1,1)")
    with registry.db.transaction() as blocker:  # the cleaner, between its read and its unlinks
        blocker.execute("SELECT pg_advisory_xact_lock(%s)", (FLEET_ASSET_LOCK,))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with registry.db.transaction() as conn:
                conn.execute("SET LOCAL lock_timeout='200ms'")
                write(conn)
    with registry.db.transaction() as conn:  # the lock released: the same write goes through
        conn.execute("SET LOCAL lock_timeout='200ms'")
        write(conn)
