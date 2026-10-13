"""The device fence: the one home of who may act on a device's generation.

A per-device reader/writer lock, PostgreSQL's transaction-scoped advisory locks in the two-int key
space (class `DEVICE_FENCE_CLASS`, the device's key): `act` is shared, so any number of requests act
on one device at once and none waits on another's slow COMMIT; `change` is exclusive (a boot offer,
a node enrolment, retirement), and PostgreSQL queues new `act`s behind a waiting `change`, so a
change is never starved. The lock is memory only: no row write, no transaction id, no WAL.

Rules (design r2, rule 1): a transaction holds at most one device fence, at the strength it took;
`fence_in` refuses a second (`device_fence_held`), and a fence not granted within the transaction's
`lock_timeout` answers 503 `node_device_busy` with Retry-After: a `FleetError`, never a
`NodeControlError`, so no caller mistakes a busy device for an unavailable one. Reads take no fence: they read
`standing_in` from one READ ONLY snapshot (`Database.read_snapshot`).

Lock order: Coordination -> Runtime -> device fence -> offer quota row -> devices insert ->
session -> producer -> evidence/commands. The release writer's lock is taken alone.

Every writer of `devices.retired_at` and of `fleet_device_lifecycle` holds `change`
(`retire_in` here; the device row an offer inserts); `act` holders therefore read both without
row locks.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, Literal

from psycopg.errors import LockNotAvailable

from central.fleet.models import FleetError, NodeControlError

DEVICE_FENCE_CLASS: Final = 734118329
Strength = Literal["act", "change"]


def device_key(device_id: str) -> int:
    """The device's key in the fence's class: a signed 32-bit hash of its id. Two devices may
    share a key (about 1e-6 for 128 devices); they then only share a fence."""
    return int.from_bytes(sha256(device_id.encode()).digest()[:4], "big", signed=True)


@dataclass(frozen=True, slots=True)
class Standing:
    """A device's standing: `active` at a generation, `unrecorded` (no devices row: a Compose or
    demo Player never offered), or `unavailable` (retired or revoked)."""

    kind: Literal["active", "unrecorded", "unavailable"]
    generation: int | None = None

    def active_generation(self) -> int:
        if self.kind != "active" or self.generation is None:
            raise NodeControlError("node_device_unavailable", 403)
        return self.generation


def standing_in(conn: Any, device_id: str) -> Standing:
    """The device's standing as `conn`'s snapshot sees it; consistent only under a fence or in
    one READ ONLY snapshot."""
    row = conn.execute("SELECT d.retired_at,l.generation,l.revoked_at FROM devices d "
                       "LEFT JOIN fleet_device_lifecycle l USING(device_id) "
                       "WHERE d.device_id=%s", (device_id,)).fetchone()
    if row is None:
        return Standing("unrecorded")
    if row["generation"] is None or row["retired_at"] is not None or row["revoked_at"] is not None:
        return Standing("unavailable")
    return Standing("active", row["generation"])


@dataclass(frozen=True, slots=True)
class FencedTransaction:
    """`conn`'s transaction holding `device_id`'s fence at `strength` until it ends."""

    conn: Any
    device_id: str
    strength: Strength

    def standing(self) -> Standing:
        return standing_in(self.conn, self.device_id)

    def generation(self) -> int:
        """The device's current generation; 403 `node_device_unavailable` unless active."""
        return self.standing().active_generation()


def fence_in(conn: Any, device_id: str, strength: Strength) -> FencedTransaction:
    """Hold `device_id`'s fence at `strength` on `conn` until its transaction ends."""
    held = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                        "AND pid=pg_backend_pid() AND classid=%s AND objsubid=2) AS held",
                        (DEVICE_FENCE_CLASS,)).fetchone()["held"]
    if held:
        raise RuntimeError("device_fence_held")
    function = "pg_advisory_xact_lock_shared" if strength == "act" else "pg_advisory_xact_lock"
    try:
        conn.execute(f"SELECT {function}(%s,%s)", (DEVICE_FENCE_CLASS, device_key(device_id)))
    except LockNotAvailable as busy:
        raise FleetError("node_device_busy", 503, retry_after=1) from busy
    return FencedTransaction(conn, device_id, strength)


def retire_in(fenced: FencedTransaction, now: float) -> None:
    """The one generation bump. Writes the devices tombstone a row-less Player lacks; retiring a
    retired device changes nothing."""
    if fenced.strength != "change":
        raise RuntimeError("device_fence_change_required")
    conn, device_id = fenced.conn, fenced.device_id
    conn.execute("INSERT INTO devices(device_id,first_seen,last_seen) VALUES(%s,%s,%s) "
                 "ON CONFLICT(device_id) DO NOTHING", (device_id, now, now))
    conn.execute("UPDATE devices SET retired_at=%s WHERE device_id=%s AND retired_at IS NULL",
                 (now, device_id))
    conn.execute("UPDATE fleet_device_lifecycle SET generation=generation+1,revoked_at=%s "
                 "WHERE device_id=%s AND revoked_at IS NULL", (now, device_id))
