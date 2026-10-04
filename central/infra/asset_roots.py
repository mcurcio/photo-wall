"""The fleet asset lock: serializes offer and reservation publication with device retirement.

Moved from `central/fleet/locks.py` so infra's deployment writer (`central/infra/node_releases.py`)
can take it without importing fleet. Its holders: boot offers and their artifact reads, fleet
reservations and retirement (`central/fleet/`, `central/registry.py`) and the deployment writer.
The cache cleaner does NOT take it, and no other writer of a desired-set input needs to: the
cleaner re-checks the desired set immediately before each unlink (`central/assets/maintenance.py`),
and a file removed in the remaining window is restored by read-through.

Callers that also mutate Runtime equipment acquire Coordination then Runtime before this lock.
Within the fleet boundary, lock device, lifecycle, session, attempt and artifact rows in that
order where applicable.
"""

from __future__ import annotations

from typing import Any, Final

FLEET_ASSET_LOCK: Final = 734118328


def lock_fleet_assets_in(conn: Any) -> None:
    """Hold the fleet asset lock on `conn` until its transaction ends."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (FLEET_ASSET_LOCK,))
