"""A dark Frame as Central reads it from its Pi's health verdict (issue #126). Pure: no database,
no bus, no clock of its own.

The owner, 2026-10-20: Central records that a Frame went dark, with no alert and no notification
(the debug overlay is the alert); the Frame page's existing Player status reads it, for example
"Dark since 08:10 · out of open files".

Rules this module owns (one home each):
- **Which verdict entry is a Frame's**: the entry of the Output the Frame is bound to.
- **How a dark Frame is worded for the console**: the card's code, its catalogue household line
  (contracts/node_faults.py, the one wording of a fault) and the Pi's `since`, passed through as
  the Pi sent it (shown, never compared with Central's clock).

Rules it does NOT own: when an Output is dark (the Pi's judge); where the verdict is read from
(central/infra/health_report_store.py); the console's precedence among a Frame's states
(central/console/src/health.js `frameHealth`).
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from contracts.models import Model
from contracts.node_health import HealthReport
from contracts.node_output import SET_TIME_PATTERN


class FrameDark(Model):
    """A Frame whose Output's debug overlay is tinted: the catalogue code its card shows, that
    code's household line (contracts.node_faults FAULTS; the code itself when this Central's
    catalogue lacks it) and when its evidence began on the Pi's clock (a set time)."""
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    line: Annotated[str, Field(min_length=1, max_length=96)]
    since: Annotated[str, Field(pattern=rf"^{SET_TIME_PATTERN}$")]


def frame_dark(report: HealthReport | None, output_id: str | None) -> FrameDark | None:
    """The bound Output's darkness: None when `report` or `output_id` is None, when the report names
    no entry for `output_id`, or when that entry is not dark; else FrameDark(entry.code, the
    catalogue's household line for it, entry.since). Pure."""
    ...


__all__ = ["FrameDark", "frame_dark"]
