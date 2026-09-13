"""Equipment-serial identity derivation, shared by the netboot bootstrap
(appliance/bootstrap.py LinuxOps.device_id) and the flashed-image fallback
(player/service.py hardware_boot_context) so a given Pi resolves to the SAME
`device_id` regardless of boot tier (0008: device_id is the immutable serial).
"""

import hashlib
import re

MAX_RAW_BYTES = 256
# Both raw-serial read sites (appliance/bootstrap.py LinuxOps.device_id and
# player/service.py read_pi_serial) read exactly READ_CAP bytes so an
# oversized file is detected (raw longer than MAX_RAW_BYTES survives
# normalization) rather than silently truncated to something that validates.
# Defined once, here, so a future one-sided edit to either read site cannot
# desynchronize the netboot/flash device_id derivations from each other.
READ_CAP = MAX_RAW_BYTES + 1
_NORMALIZED_PATTERN = re.compile(rb"[a-z0-9-]+")


def equipment_device_id(kind: str, raw: bytes) -> str | None:
    """Normalize a raw hardware identifier and hash it into the
    `device-<64hex>` form used for `device_id` everywhere.

    Returns None when `raw` fails validation (empty after normalization,
    oversized, or outside `[a-z0-9-]`) so callers can try the next equipment
    source in their own precedence list instead of trusting untrusted bytes.
    """
    normalized = raw.strip(b"\x00\r\n ").lower()
    if not normalized or len(normalized) > MAX_RAW_BYTES or not _NORMALIZED_PATTERN.fullmatch(normalized):
        return None
    return "device-" + hashlib.sha256(kind.encode() + b":" + normalized).hexdigest()
