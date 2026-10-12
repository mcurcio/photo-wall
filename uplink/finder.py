"""Finding Central (R1), shared by provisioning and the Player: the kernel command line's root
wins, then a root the stage saved. Then one locate. Nothing found here is persisted (0014)."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from uplink.causes import Cause, UplinkError
from uplink.locate import LocatedCentral, locate
from uplink.origin import Origin, Url
from uplink.resolver import Configured, Unconfigured
from uplink.transport import Transport

RootSource = Literal["cmdline", "saved"]


@dataclass(frozen=True, slots=True)
class Found:
    root: Origin
    source: RootSource
    central: LocatedCentral


def choose_root(resolution: Configured | Unconfigured, *,
                saved: Origin | None) -> tuple[Origin, RootSource] | None:
    """R1, in order:
      Configured          -> (resolution.root, "cmdline"); `saved` is never consulted
      Unconfigured, saved -> (saved, "saved")
      otherwise           -> None"""
    if isinstance(resolution, Configured):
        return resolution.root, "cmdline"
    if saved is not None:
        return saved, "saved"
    return None


async def find_central(resolution: Configured | Unconfigured, *, transport: Transport,
                       saved: Origin | None = None,
                       on_hop: Callable[[Url, int, str], None] | None = None) -> Found:
    """choose_root, then uplink.locate(root, transport=transport, on_hop=on_hop) on a worker
    thread (asyncio.to_thread). No root: UplinkError(CONFIGURATION, "absent",
    detail="not_discovered"). Every locate failure is an UplinkError. Cancelling the caller
    does not stop the thread: it ends within LOCATE_DEADLINE and its result is dropped.
    Nothing is persisted."""
    chosen = choose_root(resolution, saved=saved)
    if chosen is None:
        raise UplinkError(Cause.CONFIGURATION, "absent", detail="not_discovered")
    root, source = chosen
    central = await asyncio.to_thread(locate, root, transport=transport, on_hop=on_hop)
    return Found(root, source, central)
