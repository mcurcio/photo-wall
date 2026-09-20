"""One hardened file-open primitive for central's byte-serving routes.

Every artifact central streams to an unauthenticated client -- the `.deb`
(`/v1/app/package`), and the netboot base squashfs plus its `SHA256SUMS`
(`/v1/netboot/base`) -- opens through `open_regular` so the
`O_RDONLY|O_CLOEXEC|O_NOFOLLOW` + `fstat` + regular-file discipline is enforced
by shared code, not re-copied per route. Three hand-copies drift; a drifted
copy is how a symlink-follow or a fifo/device serve slips into an unauthed
route.
"""

from __future__ import annotations

import os
import stat


class HardenedOpenError(Exception):
    """`reason` is `"unavailable"` (open failed / not found / symlink refused)
    or `"invalid"` (not a regular file, or outside the required size). Callers
    map it to their route-specific 503 code (`app_artifact_*`/`base_artifact_*`).
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def open_regular(path, *, expected_size: int | None = None, max_size: int | None = None):
    """Open `path` `O_RDONLY|O_CLOEXEC|O_NOFOLLOW`, `fstat` it, and require a
    regular file.

    `expected_size` demands an exact size match (the `.deb` route, whose size is
    registry-recorded); `max_size` demands `0 < size <= max_size` (the netboot
    base squashfs and its `SHA256SUMS`, which have no recorded size). Returns
    `(fd, stat_result)`; the caller owns closing the fd. Raises
    `HardenedOpenError` on any failure, closing the fd first.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise HardenedOpenError("unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise HardenedOpenError("invalid")
        if expected_size is not None and metadata.st_size != expected_size:
            raise HardenedOpenError("invalid")
        if max_size is not None and not 0 < metadata.st_size <= max_size:
            raise HardenedOpenError("invalid")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, metadata
