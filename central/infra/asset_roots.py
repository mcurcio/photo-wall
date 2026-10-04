"""The one serialization boundary for asset roots: who may add or drop what the cache must keep.

Every writer of a desired-set input and the cache cleaner take this transaction-scoped advisory
lock, so the cleaner's read of the desired set and its deletes see no root committed in between.
The writers: a node boot selection, a boot offer, a release ingest and every node release
observation (`central/infra/node_releases.py`); V1 release rows, the promoted and last-good tags,
device pins and served tags (`central/infra/catalog_records.py`, through `_root_write`);
known-good tags (`central/netboot_base.py`); fleet reservations and device retirement.

Callers that also mutate Runtime equipment acquire Coordination then Runtime before this lock.
Within the fleet boundary, lock device, lifecycle, session, attempt and artifact rows in that
order where applicable.
"""

from __future__ import annotations

from typing import Any, Final

from central.infra.transactions import pg_connection
from central.kernel.transactions import Transaction

FLEET_ASSET_LOCK: Final = 734118328


def lock_fleet_assets_in(conn: Any) -> None:
    """Hold the asset-roots lock on `conn` until its transaction ends."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (FLEET_ASSET_LOCK,))


def lock_asset_roots(tx: Transaction) -> None:
    """`lock_fleet_assets_in` for a kernel `Transaction`."""
    lock_fleet_assets_in(pg_connection(tx))
