"""What a failure line carries beside its cause (R9): the boot's clock record next to a
certificate failure, and which anchors and floor the stage trusted. One text for stage 1's
FAILED line, provisioning's log and the Player's fault detail."""

import time
from typing import Final

from contracts.clock_record import ClockRecord
from uplink.causes import Cause, UplinkError
from uplink.trust import Trust

MAX_FAILURE_TEXT: Final = 1024


def trust_provenance(trust: Trust, floor: int) -> str:
    """Which CA list the stage carries and how old the build is:
    'bundle=sha256:<12 hex> anchors=<n> floor=<date>'."""
    date = time.strftime("%Y-%m-%d", time.gmtime(floor))
    return f"bundle=sha256:{trust.sha256[:12]} anchors={trust.anchors} floor={date}"


def failure_text(error: UplinkError, *, clock: ClockRecord | None, provenance: str = "") -> str:
    """error.console(); for cause TIME or TLS/untrusted, plus ' ' + clock.summary() (or
    'clock=unknown' when there is no record) and the provenance when given. One console line,
    at most 1024 characters."""
    text = error.console()
    if error.cause is Cause.TIME or (error.cause, error.reason) == (Cause.TLS, "untrusted"):
        text += " " + (clock.summary() if clock is not None else "clock=unknown")
        if provenance:
            text += f" {provenance}"
    return text[:MAX_FAILURE_TEXT]
