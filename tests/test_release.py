"""Surviving contract after the signed-release retirement (0009): only the
streamed-rootfs size bound remains, consumed by appliance/netboot_init.py to
cap the verified download. The Release/BootRequest/BootTicket manifest format
and its tests were deleted with the signed boot path."""

from contracts.release import MAX_ROOTFS_BYTES


def test_max_rootfs_bytes_is_a_positive_one_gib_bound():
    assert isinstance(MAX_ROOTFS_BYTES, int)
    assert MAX_ROOTFS_BYTES == 1024**3
