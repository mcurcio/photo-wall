"""Central's host-health thresholds (console DDD §63): numbers only, served by G12.

The console's one classifier (hostHealth.js) judges and words every band and the host-silence
limit from these numbers; it holds no default of its own. The bands are contracts/node_observation.py
`HOST_METRIC_BANDS`, the one table HostCore also puts its metrics by (decision 0034, S4a). No prose
is served.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Final

from contracts.node_observation import HOST_METRIC_BANDS
from contracts.node_state import HOST_METRICS_EVERY

# A Pi off Central's hub reads silent once its newest host record is older than this: four of the
# host's metrics intervals, so a leaf's blip just after a put never reads as silence (hostHealth.js).
HOST_SILENT_AFTER_SECONDS: Final[int] = 4 * HOST_METRICS_EVERY


def thresholds_document() -> dict:
    """The served thresholds table, a fresh copy on every read."""
    return {"host_silent_after_seconds": HOST_SILENT_AFTER_SECONDS,
            "metrics": [asdict(band) for band in HOST_METRIC_BANDS]}
