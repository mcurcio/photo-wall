"""Which Central (R1): the kernel command line's `photowall.central`, validated by the one
Central-root validator. A present value is Configured or an error, never a fallback; only an
absent one is Unconfigured, the proof a stage's own discovery (Project 2) must be handed."""

import errno
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from uplink.causes import Cause, UplinkError
from uplink.origin import Origin

CENTRAL_KEY: Final = "photowall.central"
KERNEL_COMMAND_LINE: Final = Path("/proc/cmdline")
MAX_COMMAND_LINE_BYTES: Final = 64 * 1024


def parse_kernel_command_line(text: str) -> dict[str, str]:
    """`key=value` tokens; first occurrence wins, as the kernel's own left-to-right
    precedence; bare flags ignored. Moved unchanged from stage 1."""
    result: dict[str, str] = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if sep and key not in result:
            result[key] = value
    return result


def read_kernel_command_line(path: Path = KERNEL_COMMAND_LINE) -> dict[str, str] | None:
    """Read at most 64 KiB and parse it. None ONLY when the file does not exist (no Linux
    cmdline: tests, the demo wall). Any other read error raises
    UplinkError(CONFIGURATION, "unreadable"): an unreadable cmdline proves nothing about
    absence, so it must never license discovery (R1). Procfs reports st_size 0, so the read is
    bounded explicitly. Undecodable bytes are replaced, so they can only make a value invalid."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_COMMAND_LINE_BYTES)
    except FileNotFoundError:
        return None
    except OSError as error:        # EACCES, EISDIR, ENOTDIR, EIO, ...
        raise UplinkError(Cause.CONFIGURATION, "unreadable",
                          detail=errno.errorcode.get(error.errno, type(error).__name__)
                          ) from error
    return parse_kernel_command_line(data.decode("utf-8", "replace"))


@dataclass(frozen=True, slots=True)
class Configured:
    root: Origin


@dataclass(frozen=True, slots=True)
class Unconfigured:
    """Proof that the cmdline did not name Central. A stage's own discovery (mDNS; Project 2)
    takes this value as an argument, so it cannot run without it."""
    reason: Literal["absent", "no_cmdline"]


def resolve_central(cmdline: Mapping[str, str] | None) -> Configured | Unconfigured:
    """R1. Key present (even empty): validate and return Configured, or raise
    UplinkError(CONFIGURATION, "invalid"). An invalid value NEVER falls back to discovery.
    Key absent: Unconfigured("absent"). cmdline None (no /proc/cmdline): Unconfigured("no_cmdline")."""
    if cmdline is None:
        return Unconfigured("no_cmdline")
    if CENTRAL_KEY not in cmdline:
        return Unconfigured("absent")
    return Configured(Origin.parse_root(cmdline[CENTRAL_KEY]))
