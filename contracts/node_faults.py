"""The node fault catalogue: every fault code a node can raise, and how it is judged and worded.

Stdlib only, pure data. On the node only the health judge reads it (Display never does: an
import-linter contract forbids it); Central will serve it to the console (M4). A fault code is
data here: adding one is one change to this module, deployed Central-first, and the judge
refuses a code that is not in it. The probe constants T, k, S and K belong to App lifecycle
(`appliance.node.probe`) and D to Display; a row owns only its own raise window and clear hold.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

MAX_HOUSEHOLD_LINE = 96  # one overlay card line (Display's bound)
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _duration(value: object) -> bool:
    return type(value) is int and value >= 0


@dataclass(frozen=True, slots=True)
class Fault:
    code: str
    display_affecting: bool  # raised -> the wall shows the tint and the household line
    household_line: str
    raise_window_ms: int  # continuous evidence before a pending condition is raised
    clear_hold_ms: int  # continuous recovery before a raised condition clears

    def __post_init__(self) -> None:
        if not (isinstance(self.code, str) and _CODE.fullmatch(self.code)
                and type(self.display_affecting) is bool
                and isinstance(self.household_line, str)
                and 0 < len(self.household_line) <= MAX_HOUSEHOLD_LINE
                and _duration(self.raise_window_ms) and _duration(self.clear_hold_ms)):
            raise ValueError("fault_catalogue_row")


def _catalogue(*rows: Fault) -> Mapping[str, Fault]:
    faults = {row.code: row for row in rows}
    if len(faults) != len(rows):
        raise ValueError("fault_catalogue_duplicate")
    return MappingProxyType(faults)


FAULTS: Mapping[str, Fault] = _catalogue(
    Fault("app_unresponsive", True, "Photos paused — the player stopped responding",
          raise_window_ms=5000, clear_hold_ms=10000),
    # Degraded, not a fault: content still shows (0015), so no tint. A reported fact, not a
    # window: raised when the run reports a software renderer, cleared when one reports the GPU.
    Fault("software_renderer", False, "Photos are drawn without the graphics processor",
          raise_window_ms=0, clear_hold_ms=0),
    # 1b P1b: no app process. Opened when the broker reports the run exited, withdrawn or cleared
    # when it reports a new run started (a paced relaunch inside the window is never shown);
    # whether that run answers is app_unresponsive's question, not this one's.
    Fault("app_absent", True, "Photos paused — the player stopped",
          raise_window_ms=5000, clear_hold_ms=0),
    # 1b P1b: degraded, content still shows. Raised at once when the run holds at least 80 % of
    # its open-file limit (appliance.health.judge.RESOURCE_PRESSURE_PERCENT); clears after a
    # minute below it, or when that run exits.
    Fault("app_resource_exhausted", False, "The player is running out of open files",
          raise_window_ms=0, clear_hold_ms=60000),
)


def catalogue_digest() -> str:
    """sha256 of the catalogue's canonical JSON (rows by code): names the catalogue a node judged
    with, so a reader can tell two releases' catalogues apart without comparing rows."""
    rows = [
        {"code": fault.code, "display_affecting": fault.display_affecting,
         "household_line": fault.household_line, "raise_window_ms": fault.raise_window_ms,
         "clear_hold_ms": fault.clear_hold_ms}
        for fault in sorted(FAULTS.values(), key=lambda fault: fault.code)
    ]
    canonical = json.dumps(rows, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = ["FAULTS", "MAX_HOUSEHOLD_LINE", "Fault", "catalogue_digest"]
