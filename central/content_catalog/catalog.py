"""`ReleaseCatalog`: the release catalog behind the kernel's `ContentCatalog` port.

It names the desired set (the wanted node deployments' files) and holds the operator's refresh
of the release listing. Every public async method runs its blocking work in `asyncio.to_thread`
and opens its own `transactions.begin()`, so nothing blocks the event loop.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import TypeVar

from central.content_catalog.ports import ReleaseRecords
from central.kernel.job_types import (
    AssetJob,
    FetchOsImage,
    FetchSealedEnvironment,
    Prefetch,
    SyncReleases,
)
from central.kernel.ports import DesiredTiers
from central.kernel.publishing import Publisher
from central.kernel.transactions import Transaction, Transactions
from contracts.equipment import equipment_device_id
from contracts.time import Clock

# The `equipment_device_id` kind, shared with player/service.py via contracts.equipment, so one
# Pi resolves to one device_id at boot and at enrollment.
DEVICE_KIND = "pi"
# The only shape a client serial may take before it can name a device or be logged: a Pi serial
# is 16 hex digits; a small safe superset leaves room for another scheme. The serial is
# unauthenticated, so control characters, separators and whitespace are refused here.
_SAFE_SERIAL = re.compile(r"[A-Za-z0-9:_.-]{1,128}")

T = TypeVar("T")


async def in_transaction(transactions: Transactions, body: Callable[[Transaction], T]) -> T:
    """Run `body` in one `transactions.begin()` on a worker thread, off the event loop."""
    def run() -> T:
        with transactions.begin() as tx:
            return body(tx)

    return await asyncio.to_thread(run)


def sanitize_serial(serial: str | None) -> str | None:
    """The serial if it matches the safe charset, else None (a route logs only this value)."""
    if serial is not None and _SAFE_SERIAL.fullmatch(serial):
        return serial
    return None


def device_id_for_serial(serial: str | None) -> str | None:
    """The canonical `device-<64hex>` id of a safe serial, else None."""
    serial = sanitize_serial(serial)
    return None if serial is None else equipment_device_id(DEVICE_KIND, serial.encode())


class ReleaseCatalog:
    """Implements the kernel's `ContentCatalog` for the node deployments."""

    def __init__(self, *, releases: ReleaseRecords, transactions: Transactions,
                 publisher: Publisher, clock: Clock) -> None:
        self._releases = releases
        self._transactions = transactions
        self._publisher = publisher
        self._clock = clock

    # -- ContentCatalog ---------------------------------------------------------------------------

    async def desired_assets(self) -> frozenset[AssetJob]:
        return await self._in_tx(self.desired_in)

    async def desired_tiers(self) -> DesiredTiers:
        return await self._in_tx(self.desired_tiers_in)

    def desired_in(self, tx: Transaction) -> frozenset[AssetJob]:
        """The desired set (`desired_tiers_in`, both tiers), read inside the caller's
        transaction: the release read and the cache cleaner keep exactly this."""
        tiers = self.desired_tiers_in(tx)
        return tiers.wanted | frozenset(tiers.background)

    def desired_tiers_in(self, tx: Transaction) -> DesiredTiers:
        """The desired set split by urgency, read inside the caller's transaction.

        The exact node roots (`ReleaseRecords.fleet_desired_assets`: the selected and previous
        node deployments' bases and sealed environments) are `wanted`; the files only the window
        of newest stable node releases names are `background`, newest release first.
        """
        fleet = self._releases.fleet_desired_assets(tx)
        jobs: set[AssetJob] = {FetchOsImage(tarball_sha256=digest)
                               for digest in fleet.base_tarballs}
        jobs.update(FetchSealedEnvironment(sha256=digest) for digest in fleet.sealed_environments)
        background = tuple(job for job in fleet.window if job not in jobs)
        return DesiredTiers(frozenset(jobs), background)

    # -- operator actions -------------------------------------------------------------------------

    async def refresh(self) -> None:
        """Force a full listing: clear the stored ETag and publish the sync in one transaction
        (the operator's repair of a stale equal-version observation, design §6.4)."""
        def write(tx: Transaction) -> None:
            self._releases.store_etag(tx, None, now=self._clock.utc())
            self._publisher.publish(SyncReleases(), within=tx, retry_terminal=True)

        await self._in_tx(write)
        await self._publisher.publish_now(Prefetch())

    # -- internals --------------------------------------------------------------------------------

    async def _in_tx(self, body: Callable[[Transaction], T]) -> T:
        return await in_transaction(self._transactions, body)
