"""How long Central may hold a device's asset request before its status line. A request that
misses Central's cache waits for the fetch it publishes, up to this bound, and only then is
answered (the asset, or Central's own error). Central's read path waits exactly this long; a
device waits at least this long for the status line, so a miss reaches it as Central's error
(R9), not as a connect timeout. Stdlib only, so it runs in the initramfs."""

from typing import Final

READ_THROUGH_WAIT_SECONDS: Final = 30.0
