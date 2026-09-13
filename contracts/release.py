"""Shared rootfs size bound for the netboot image stream (0009).

The signed release manifest / boot ticket format (Release, BootRequest,
BootTicket, require_compatible) has been retired along with the signed-rootfs
boot path. Only the streamed-image size cap survives, consumed by
appliance/netboot_init.py to bound the verified download.
"""

from __future__ import annotations

MAX_ROOTFS_BYTES = 1024**3
