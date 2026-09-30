"""Shared fleet serialization boundary for artifact roots and device retirement.

Callers that also mutate Runtime equipment acquire Coordination then Runtime
before this lock. Within the fleet boundary, lock device, lifecycle, session,
attempt and artifact rows in that order where applicable.
"""

FLEET_ASSET_LOCK = 734118328


def lock_fleet_assets_in(conn) -> None:
    """Serialize offer/reservation publication, retirement and conditional eviction."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (FLEET_ASSET_LOCK,))
