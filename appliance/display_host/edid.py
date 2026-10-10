"""A display's EDID to its identity and modes (roadmap 1b; run ledger .claude/runs/display-1b.md,
slice D1). Pure: bytes in, values out; the read of /sys/class/drm/<card>-<output>/edid is the
caller's (`read_edid`).

The Pi normalises what the EDID says and never decides "same display": that is Central's
(central/displays/). Spike, 2026-10-10: the test monitor's EDID is maker XYM, product 5475, name
MNN, serial 0 and blank serial text, so `serial` is None for it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

from contracts.node_output import DisplayIdentity, DisplayMode

DRM_ROOT: Final = Path("/sys/class/drm")
MAX_EDID_BYTES: Final = 32 * 1024     # a base block and up to 255 extensions


def parse_edid(raw: bytes) -> tuple[DisplayIdentity | None, tuple[DisplayMode, ...]]:
    """The identity and the modes `raw` states. Identity None when the base block is absent,
    shorter than 128 bytes, has a wrong header or checksum; modes come from the detailed timing
    descriptors and the CTA extension's short video descriptors that the base block's checksum
    covers, deduplicated, the preferred one first, at most MAX_DISPLAY_MODES. Never raises."""
    raise NotImplementedError


def read_edid(output_id: str, *, root: Path = DRM_ROOT) -> bytes | None:
    """The EDID bytes of the connector named `output_id` (`<root>/card*-<output_id>/edid`, at most
    MAX_EDID_BYTES), or None when no such connector exists or it holds no EDID. Never raises."""
    raise NotImplementedError
