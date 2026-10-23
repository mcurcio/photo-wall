"""The health judge's verdict on its Node's bus: is each Output dark, and why (issue #126; the
debug overlay design, revision 2, with the owner's answers of 2026-10-20). Stdlib and contracts
only: the Pi's health judge runs it, and its unit stays small (no pydantic).

The owner, 2026-10-20: Central records that a Frame went dark, with no alert and no notification
(the debug overlay is the alert); earlier, 2026-10-03: "the debug overlay enables and shows the
crash timestamp and reason", and boot and app failures "reach Central and the console".

One key rides the node bus's `health` store line (contracts/node_link.py STORE_LINES, fleet pipe),
in its state bucket: HEALTH_REPORT_KEY, a `HealthReport`. The health judge (appliance/health)
writes it whenever its verdict's sequence moves, and again at every attach (a bus restart starts
empty). Central records every value it reads (node_link_records) and reads the newest one for the
Frame page's status; Home Assistant may read it through the Pi's read-only door.

Rules this module owns (one home each):
- the wire shape and its canonical JSON codec (contracts/wire_codec.py rules);
- the key, its largest size and the line's schema major.

Rules it does NOT own: when an Output is dark and which code it shows (the judge,
appliance/health/judge.py, over contracts/node_faults.py's catalogue); how a code is worded (the
catalogue's `household_line`); how Central words a dark Frame (central/fleet/frame_dark.py).

`since` is the Pi's own wall clock, shown beside the card's text and on the console, never
compared with another clock (the owner's rule: clocks compare only to themselves).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

HEALTH_LINE: Final = "health"                # contracts.node_link.STORE_LINES["health"]
HEALTH_COMPONENT: Final = "health"           # the component writing the line, and its birth's name
HEALTH_SCHEMA_MAJOR: Final = 1               # the major of the line's keys below (its birth names it)
HEALTH_REPORT_KEY: Final = "verdict"         # the state key of a HealthReport
HEALTH_REPORT_BYTES: Final = 4096            # its largest encoding
MAX_HEALTH_OUTPUTS: Final = 16               # the most Outputs one report names (Display ships 16)


@dataclass(frozen=True)
class OutputHealth:
    """One connected Output as the judge judged it. `dark`: a display-affecting code is raised on it,
    so its debug overlay's tint is on. `code`: the catalogue code its card shows (the first of the
    judge's card precedence). `since`: the Pi's wall clock, as a set time
    (contracts/node_output.py SET_TIME_PATTERN), when that code's evidence began (the condition
    opened). `code` and `since` are both None exactly when `dark` is False."""
    output_id: str
    dark: bool
    code: str | None
    since: str | None

    def __post_init__(self) -> None:
        """ValueError("output_health") unless output_id is a node token of at most 96 characters
        (contracts.node_protocol `token`), dark is a bool, and either dark is False with code and
        since None, or dark is True with code a catalogue-shaped code ([a-z][a-z0-9_]{0,63}) and
        since a set time that decodes (contracts.node_output `decode_set_time`)."""
        ...


@dataclass(frozen=True)
class HealthReport:
    """The judge's verdict for every connected Output of Display's latest snapshot. `sequence`: the
    judge's verdict sequence (per judge process: it restarts at 0 when the judge
    restarts, so a reader orders reports by the bus record, never by this number). `catalogue`:
    contracts.node_faults `catalogue_digest()` of the catalogue the codes come from."""
    sequence: int
    catalogue: str
    outputs: tuple[OutputHealth, ...]

    def __post_init__(self) -> None:
        """ValueError("health_report") unless sequence is an int >= 0 (never a bool), catalogue is 64
        lowercase hex digits, outputs is a tuple of at most MAX_HEALTH_OUTPUTS OutputHealth with
        unique output_ids, in output_id order."""
        ...


def encode_health_report(report: HealthReport) -> bytes:
    """Canonical JSON (contracts.wire_codec `encode`) of at most HEALTH_REPORT_BYTES;
    ValueError("health_report_too_large") past it."""
    ...


def decode_health_report(raw: bytes) -> HealthReport:
    """The report `raw` encodes (contracts.wire_codec `decode` rules: strict JSON, exactly the
    dataclasses' field names); ValueError("health_report") for anything else."""
    ...


__all__ = [
    "HEALTH_COMPONENT", "HEALTH_LINE", "HEALTH_REPORT_BYTES", "HEALTH_REPORT_KEY", "HEALTH_SCHEMA_MAJOR",
    "MAX_HEALTH_OUTPUTS", "HealthReport", "OutputHealth", "decode_health_report", "encode_health_report",
]
