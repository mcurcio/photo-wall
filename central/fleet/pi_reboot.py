"""A Pi's reboot, asked of its host over the Pi's fleet-pipe link (decision 0034, S4a).

Only the worker holds the Pis' links (central/node_bus_wiring.py), and Install is written by the API
process too, so a reboot is a job on Central's queue: the write that wants it publishes `RebootPi` in
its own transaction (an Install that rolls back queues none; one pending copy per Pi), and the
worker's `RebootPiHandler` asks the Pi's host (contracts/node_state.py HOST_REBOOT_METHOD) through
`PiReboot`, then records the ask as one `events` row (`link_action_in`, kind 'reboot', body
{"request_id", "reason", "by", "delivered"}), kept `EVENTS_KEPT_SECONDS` like every event. A Pi with
no link now, or that does not answer, is not asked again: its next boot takes its version anyway.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Final, Protocol

from central.infra.events import link_action_in
from central.infra.node_links import NodeLinks
from central.infra.transactions import PgTransactions, pg_connection
from central.kernel.job_types import RebootPi
from contracts.node_link import Pipe
from contracts.node_state import (
    HOST_LINE,
    HOST_REBOOT_METHOD,
    HostReboot,
    decode_host_reboot_answer,
    encode_host_reboot,
)

log = logging.getLogger(__name__)

REBOOT_CALL_SECONDS: Final = 5.0   # how long the host has to answer `reboot`
REBOOT_ACTION: Final = "reboot"    # the `events` link action's kind


class PiReboot(Protocol):
    async def reboot(self, device_id: str, reason: str) -> bool:
        """Ask the Pi's host to reboot over its fleet-pipe link; False when it has no link now or
        does not answer within the call's timeout."""
        ...


class BusPiReboot:
    """PiReboot over the worker's fleet-pipe NodeLinks (built by NodeBus.pi_reboot())."""

    def __init__(self, links: NodeLinks) -> None:
        self._links = links

    async def reboot(self, device_id: str, reason: str) -> bool:
        reply = await self._links.call(device_id, HOST_LINE, HOST_REBOOT_METHOD,
                                       encode_host_reboot(HostReboot(reason=reason)), timeout=REBOOT_CALL_SECONDS)
        if reply is None:
            return False
        try:
            return decode_host_reboot_answer(reply).accepted
        except ValueError:
            log.warning("reboot on %s: the host's answer does not decode", device_id)
            return False


class NoHubReboot:
    """PiReboot with no hub deployed: no Pi is asked (its next boot takes its version)."""

    async def reboot(self, device_id: str, reason: str) -> bool:
        return False


class RebootPiHandler:
    """Handler[RebootPi]: ask the Pi through `PiReboot`, then record the ask in `events`."""

    def __init__(self, reboot: PiReboot, transactions: PgTransactions) -> None:
        self._reboot = reboot
        self._transactions = transactions

    async def handle(self, job: RebootPi) -> None:
        delivered = await self._reboot.reboot(job.device_id, job.reason)
        log.info("reboot %s on %s (%s, by %s): %s", job.request_id, job.device_id, job.reason, job.by,
                 "asked" if delivered else "not delivered")
        await asyncio.to_thread(self._record, job, delivered)

    def _record(self, job: RebootPi, delivered: bool) -> None:
        with self._transactions.begin() as tx:
            link_action_in(pg_connection(tx), job.device_id, Pipe.FLEET, REBOOT_ACTION,
                           {"request_id": job.request_id, "reason": job.reason, "by": job.by,
                            "delivered": delivered})


__all__ = ["REBOOT_ACTION", "BusPiReboot", "NoHubReboot", "PiReboot", "RebootPiHandler"]
