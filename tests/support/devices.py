"""The tests' one `devices` insert after the full migration (decision 0034, S2e): a device as
central/fleet/node_boot.py `claim_new_device` writes it, generation 1 and its audit fields by
"node". A test seeding a database before migration 121 keeps its own raw insert."""

from __future__ import annotations

from typing import Any


def seed_device(conn: Any, device_id: str, serial: str | None, *, at: float = 1.0,
                retired_at: float | None = None) -> None:
    """Insert `device_id` (serial `serial`, seen at `at`) at generation 1, retired at `retired_at`
    when given; a known device is left as it is."""
    conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen, retired_at, generation, "
                 "created_at, created_by, changed_at, changed_by) "
                 "VALUES (%s, %s, %s, %s, %s, 1, %s, 'node', %s, 'node') ON CONFLICT (device_id) DO NOTHING",
                 (device_id, serial, at, at, retired_at, at, at))
