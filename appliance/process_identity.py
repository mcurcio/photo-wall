"""A process's kernel birth tick, the local fence the base's units compare a MainPID against.

It does not prove app control, trusted device identity, or visible output.
"""

from __future__ import annotations

from pathlib import Path


def read_proc_start_ticks(proc_root: Path, pid: int) -> int | None:
    """Read field 22 of one kernel stat record without trusting the process name."""
    if type(pid) is not int or not 0 < pid < 2**31:
        return None
    try:
        with (proc_root / str(pid) / "stat").open("rb") as stream:
            raw = stream.read(4097)
    except OSError:
        return None
    if len(raw) > 4096 or not raw.startswith(f"{pid} (".encode()):
        return None
    close = raw.rfind(b") ")
    if close < 0:
        return None
    fields = raw[close + 2:].split()
    # /proc/<pid>/stat field 22 is starttime; field 3 begins here.
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    ticks = int(fields[19])
    return ticks if 0 < ticks < 2**63 else None

