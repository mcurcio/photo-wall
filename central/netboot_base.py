"""The legacy netboot seams that stay in the MVP: the serial header and base-health.

Selection, serving, fetch, cache and GC moved behind the content catalog and the asset read path
(`central.content_catalog`, `central.assets`; design §6). What remains:

* `SERIAL_HEADER`: the netboot serial on the wire. The safe-serial rule and the canonical
  `devices` id derivation live in `central.content_catalog.catalog` (`sanitize_serial`,
  `device_id_for_serial`).
* `record_base_health` (authenticated base-health): advances `known_good_tag` only for a
  genuinely healthy check-in whose `running_tag` equals the tag Central recorded as last-served --
  the sole writer of the frontier input. Its move into the catalog is an MVP cut.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a hard import cycle at module load; only for type hints
    from contracts.models import BaseHealth

# The request header the Pi's initrd sends its hardware serial in. Duplicated on
# the client side (appliance/netboot_init.py SERIAL_HEADER): the two sides are
# independent (central must not import appliance, and vice versa), so this wire
# constant is restated rather than shared, like the route strings.
SERIAL_HEADER = "X-PhotoWall-Serial"


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
