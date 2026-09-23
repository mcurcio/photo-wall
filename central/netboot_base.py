"""The legacy netboot seams that stay in the MVP: the serial wire helpers and base-health.

Selection, serving, fetch, cache and GC moved behind the content catalog and the asset read path
(`central.content_catalog`, `central.assets`; design §6). What remains:

* `SERIAL_HEADER`, `sanitize_serial`, `device_id_for_serial`: the netboot serial on the wire and
  its canonical `devices` id.
* `record_base_health` (authenticated base-health): advances `known_good_tag` only for a
  genuinely healthy check-in whose `running_tag` equals the tag Central recorded as last-served --
  the sole writer of the frontier input. Its move into the catalog is an MVP cut.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from contracts.equipment import equipment_device_id

if TYPE_CHECKING:  # avoid a hard import cycle at module load; only for type hints
    from contracts.models import BaseHealth

# The request header the Pi's initrd sends its hardware serial in. Duplicated on
# the client side (appliance/netboot_init.py SERIAL_HEADER): the two sides are
# independent (central must not import appliance, and vice versa), so this wire
# constant is restated rather than shared, like the route strings.
SERIAL_HEADER = "X-PhotoWall-Serial"
# The `device_id` derivation kind, shared with appliance/bootstrap.py and
# player/service.py via contracts.equipment so one Pi resolves to one device_id.
DEVICE_KIND = "pi"
# The only shape a client serial may take before it can select an image or be
# logged: a Pi serial is 16 hex digits, but keep a small safe superset so a
# future per-serial scheme has room, and reject everything else (control chars,
# path separators, whitespace) at the seam -- the serial is unauthenticated,
# client-controlled input.
_SAFE_SERIAL = re.compile(r"[A-Za-z0-9:_.-]{1,128}")


def sanitize_serial(serial: str | None) -> str | None:
    """The serial if it matches the safe charset, else None. Bounds what the
    route logs AND guards the selection seam, so the guarantee holds regardless
    of caller."""
    if serial is not None and _SAFE_SERIAL.fullmatch(serial):
        return serial
    return None


def device_id_for_serial(serial: str | None) -> str | None:
    """Map a (sanitized) serial to its canonical `device-<64hex>` id, or None.

    Uses the one shared derivation (`contracts.equipment.equipment_device_id`,
    `kind="pi"`) the appliance and the flashed-image fallback use, so a given Pi
    resolves to the SAME `device_id` at the netboot seam as it does at
    enrollment (which is how `players.device_id` later joins the row)."""
    if serial is None:
        return None
    return equipment_device_id(DEVICE_KIND, serial.encode())


# -- base-health seam --------------------------------------------------------


def record_base_health(conn, device_id: str, report: "BaseHealth", *, clock) -> bool:
    """Advance `known_good_tag` for a validated base-health check-in; return
    whether it was applied.

    Read-modify-write under `SELECT ... FOR UPDATE` on the device row, then a
    single conditional `UPDATE ... WHERE last_served_tag = running_tag` (E1): a
    concurrent recovery serve that moved `last_served_tag` between the read and
    the write invalidates this write, so a device is never recorded healthy on a
    tag it was just rolled off. Requires: the device row exists; the report is
    monotonic per authority epoch (known-good only advances); `healthy` is true;
    and `running_tag` equals the tag Central recorded as last-served (not a live
    re-resolve -- that would drop a genuine report after a frontier move, S1).
    On success also clears `failed_tag` if it equals `running_tag`."""
    device = conn.execute(
        "SELECT device_id FROM devices WHERE device_id=%s FOR UPDATE", (device_id,)
    ).fetchone()
    if device is None:
        return False

    prior = conn.execute(
        "SELECT sequence FROM device_base_health WHERE device_id=%s AND authority_epoch=%s",
        (device_id, report.authority_epoch),
    ).fetchone()
    if prior is not None and report.sequence <= prior["sequence"]:
        return False  # monotonicity: a reordered/older check-in never regresses known-good
    conn.execute(
        "INSERT INTO device_base_health(device_id, authority_epoch, sequence) VALUES(%s,%s,%s) "
        "ON CONFLICT(device_id, authority_epoch) DO UPDATE SET sequence=EXCLUDED.sequence",
        (device_id, report.authority_epoch, report.sequence),
    )
    if not report.healthy:
        return False  # a false report never advances known-good, but did advance the sequence

    updated = conn.execute(
        "UPDATE devices SET known_good_tag=%s, known_good_at=%s, boot_outcome='healthy', "
        "failed_tag=CASE WHEN failed_tag=%s THEN NULL ELSE failed_tag END "
        "WHERE device_id=%s AND last_served_tag=%s",
        (report.running_tag, clock.utc(), report.running_tag, device_id, report.running_tag),
    ).rowcount
    return updated == 1
