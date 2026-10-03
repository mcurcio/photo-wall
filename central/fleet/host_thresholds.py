"""Central's host-health thresholds (console DDD §63): numbers only, served by G12.

The console's one classifier (hostHealth.js) judges and words every band and the host-silence
limit from these numbers; it holds no default of its own. The values are current choices,
edited here. No prose is served.
"""
from __future__ import annotations

from typing import Final

from contracts.node_observation import HOST_OBSERVATION_INTERVAL_SECONDS

# Four intervals without a stored host sample (§63): one late post and one coalesced post
# never read as silence.
HOST_SILENT_AFTER_SECONDS: Final[int] = 4 * HOST_OBSERVATION_INTERVAL_SECONDS

_FLAGS: Final = ("under_voltage", "frequency_capped", "throttled", "soft_temperature_limit")

# Temperature: placeholders, to confirm against the bench Pi's firmware limit (§63). A firmware
# flag set now is an alarm; a sticky "occurred" flag is a notice. A band a metric does not have
# is null. CPU is never banded, so it has no row.
_METRICS: Final = (
    {"name": "soc_temperature", "unit": "celsius", "notice_at": 75, "alarm_at": 80},
    *({"name": f"{flag}_now", "unit": "boolean", "notice_at": None, "alarm_at": 1} for flag in _FLAGS),
    *({"name": f"{flag}_occurred", "unit": "boolean", "notice_at": 1, "alarm_at": None} for flag in _FLAGS),
)


def thresholds_document() -> dict:
    """The served thresholds table, a fresh copy on every read."""
    return {"host_silent_after_seconds": HOST_SILENT_AFTER_SECONDS,
            "metrics": [dict(metric) for metric in _METRICS]}
