"""The device fence: the one home of who may act on a device's generation.

A per-device reader/writer lock, PostgreSQL's transaction-scoped advisory locks in the two-int key
space (class `DEVICE_FENCE_CLASS`, the device's key): `act` is shared, so any number of requests act
on one device at once and none waits on another's slow COMMIT; `change` is exclusive (a boot offer,
retirement), and PostgreSQL queues new `act`s behind a waiting `change`, so a
change is never starved. The lock is memory only: no row write, no transaction id, no WAL.

Rules (design r2, rule 1): a transaction holds at most one device fence, at the strength it took;
`fence_in` takes the held one again for free and refuses an upgrade or a second device
(`device_fence_held`); a fence not granted within the transaction's `lock_timeout` answers 503
`node_device_busy` with Retry-After: a `FleetError`, never a `NodeControlError`, so no caller
mistakes a busy device for an unavailable one. No fence is taken inside a SAVEPOINT: rolling one
back would forget the held fence while PostgreSQL keeps its lock. Reads take no fence: they read
`standing_in` from one READ ONLY snapshot (`Database.read_snapshot`).

Lock order: Coordination -> Runtime -> device fence -> devices insert. The release writer's lock is taken alone.

Every writer of `devices.retired_at` and `devices.generation` holds `change` (`retire_in` here;
the device row an offer inserts); `act` holders therefore read both without row locks.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, Literal

from psycopg.errors import LockNotAvailable

from central.actor import Actor
from central.fleet.models import FleetError, NodeControlError

DEVICE_FENCE_CLASS: Final = 734118329
# The transaction-local record of the fence it holds, `<strength>:<device_id>` (`set_config`'s
# is_local): cheaper than reading pg_locks on every node request.
_HELD_SETTING: Final = "photo_wall.device_fence"
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
    row = conn.execute("SELECT retired_at,generation FROM devices WHERE device_id=%s",
                       (device_id,)).fetchone()
    if row is None:
        return Standing("unrecorded")
    if row["retired_at"] is not None:
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
    """Hold `device_id`'s fence at `strength` on `conn` until its transaction ends. Fencing the
    held device again at the held strength, or `act` under a held `change`, is free; an upgrade or
    a second device could deadlock, so either raises `device_fence_held`. Which fence the
    transaction holds is a transaction-local setting, reset at COMMIT or ROLLBACK as the lock is."""
    held = conn.execute("SELECT current_setting(%s,true) AS held",
                        (_HELD_SETTING,)).fetchone()["held"]
    if held:
        held_strength, _, held_device = held.partition(":")
        if held_device != device_id or (held_strength, strength) == ("act", "change"):
            raise RuntimeError("device_fence_held")
        return FencedTransaction(conn, device_id, strength)
    function = "pg_advisory_xact_lock_shared" if strength == "act" else "pg_advisory_xact_lock"
    try:
        conn.execute(f"SELECT {function}(%s,%s),set_config(%s,%s,true)",
                     (DEVICE_FENCE_CLASS, device_key(device_id), _HELD_SETTING,
                      f"{strength}:{device_id}"))
    except LockNotAvailable as busy:
        raise FleetError("node_device_busy", 503, retry_after=1) from busy
    return FencedTransaction(conn, device_id, strength)


def retire_in(fenced: FencedTransaction, now: float, *, by: Actor) -> None:
    """The one generation bump: one UPDATE sets generation+1 and retired_at where the device is not
    retired. A row-less Player's tombstone is inserted at generation 1 first (then bumped); retiring
    a retired device changes nothing."""
    if fenced.strength != "change":
        raise RuntimeError("device_fence_change_required")
    conn, device_id = fenced.conn, fenced.device_id
    conn.execute("INSERT INTO devices(device_id,first_seen,last_seen,generation,created_at,created_by,"
                 "changed_at,changed_by) VALUES(%s,%s,%s,1,%s,%s,%s,%s) ON CONFLICT(device_id) DO NOTHING",
                 (device_id, now, now, now, by, now, by))
    conn.execute("UPDATE devices SET generation=generation+1,retired_at=%s,changed_at=%s,changed_by=%s "
                 "WHERE device_id=%s AND retired_at IS NULL", (now, now, by, device_id))
