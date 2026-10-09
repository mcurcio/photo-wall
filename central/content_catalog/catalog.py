"""`ReleaseCatalog`: the release catalog behind the kernel's `ContentCatalog` port.

It holds the Player `.deb` promotion over `ReleaseRecords` and names the desired set: the
promoted and last-good `.deb`s and the wanted node deployments' files. Every public async method
runs its blocking work in `asyncio.to_thread` and opens its own `transactions.begin()`, so
nothing blocks the event loop.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from central.content_catalog.ports import Promoter, ReleaseRecords, ReleaseRow, StoredAssets
from central.kernel.job_types import (
    AssetJob,
    FetchOsImage,
    FetchPackage,
    FetchSealedEnvironment,
    Prefetch,
    SyncReleases,
)
from central.kernel.ports import Candidates, DesiredTiers, PackageRequest, Resolution, Unknown
from central.kernel.publishing import Publisher
from central.kernel.transactions import Transaction, Transactions
from central.kernel.types import release_version
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


@dataclass(frozen=True, slots=True)
class DevicePackage:
    tag: str
    version: str  # == tag (was the app_packages label)
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ManifestRefusal:
    code: Literal["app_unconfigured"]


class CatalogError(Exception):
    """An operator action refused before any write; `kind` maps to the HTTP status class."""

    def __init__(self, code: str, kind: Literal["not_found", "conflict", "invalid"]) -> None:
        super().__init__(code)
        self.code = code
        self.kind = kind


@dataclass(frozen=True, slots=True)
class ReleaseView:
    tag: str
    is_prerelease: bool
    deployable: bool
    promoted_by: Promoter | None  # who promoted this release; None when it is not promoted
    has_os_image: bool
    promoted: bool = field(init=False)  # derived from promoted_by, so the two cannot disagree

    def __post_init__(self) -> None:
        object.__setattr__(self, "promoted", self.promoted_by is not None)


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


def _package(row: ReleaseRow | None) -> DevicePackage | None:
    if row is None or row.package is None or row.package.sha256 is None or row.package.size is None:
        return None
    return DevicePackage(row.tag, row.tag, row.package.sha256, row.package.size)


def _require_tag(tag: str) -> None:
    try:
        release_version(tag)
    except ValueError:
        raise CatalogError("invalid_tag", "invalid") from None


class ReleaseCatalog:
    """Implements the kernel's `ContentCatalog` for Player `.deb`s and the node deployments."""

    def __init__(self, *, releases: ReleaseRecords, stored: StoredAssets,
                 transactions: Transactions, publisher: Publisher, clock: Clock) -> None:
        self._releases = releases
        self._stored = stored
        self._transactions = transactions
        self._publisher = publisher
        self._clock = clock

    # -- ContentCatalog ---------------------------------------------------------------------------

    async def resolve(self, request: PackageRequest) -> Resolution:
        if not isinstance(request, PackageRequest):
            raise TypeError(f"not a content request: {request!r}")
        return await self._in_tx(lambda tx: self._resolve_package(tx, request))

    async def desired_assets(self) -> frozenset[AssetJob]:
        return await self._in_tx(self.desired_in)

    async def desired_tiers(self) -> DesiredTiers:
        return await self._in_tx(self.desired_tiers_in)

    def desired_in(self, tx: Transaction) -> frozenset[AssetJob]:
        """The desired set (`desired_tiers_in`, both tiers), read inside the caller's
        transaction: the sync tail, the release read and the cache cleaner keep exactly this."""
        tiers = self.desired_tiers_in(tx)
        return tiers.wanted | frozenset(tiers.background)

    def desired_tiers_in(self, tx: Transaction) -> DesiredTiers:
        """The desired set split by urgency, read inside the caller's transaction.

        The `.deb` of the promoted and the last-good tag, plus the exact node roots
        (`ReleaseRecords.fleet_desired_assets`: the selected and previous node deployments'
        bases and sealed environments). Everything is `wanted` except the files only the window
        of newest stable node releases names: those are `background`, newest release first.
        """
        jobs: set[AssetJob] = set()
        for tag in self._policy_tags(tx):
            if (package := _package(self._releases.get(tx, tag))) is not None:
                jobs.add(FetchPackage(sha256=package.sha256))
        fleet = self._releases.fleet_desired_assets(tx)
        jobs.update(FetchOsImage(tarball_sha256=digest) for digest in fleet.base_tarballs)
        jobs.update(FetchSealedEnvironment(sha256=digest) for digest in fleet.sealed_environments)
        background = tuple(job for job in fleet.window if job not in jobs)
        return DesiredTiers(frozenset(jobs), background)

    def _policy_tags(self, tx: Transaction) -> set[str]:
        """The promoted and the last-good tag: the `.deb`s `/v1/app/manifest` may name."""
        tags = {self._releases.promoted_tag(tx), self._releases.last_good_tag(tx)}
        return {tag for tag in tags if tag is not None}

    # -- serving ----------------------------------------------------------------------------------

    async def promoted_package(self) -> DevicePackage | ManifestRefusal:
        """The promoted `.deb`; while its bytes are not on disk, the last-good one if they are.

        Main's two-pointer rule (`app_package_policy.current_sha256` stayed on the previous
        package until the promoted one was mirrored): a failed or in-flight fetch of a new
        promotion never takes the served `.deb` away. With neither on disk, the promoted one
        (the package route fetches it on demand, since it is desired).
        """
        def read(tx: Transaction) -> DevicePackage | ManifestRefusal:
            promoted = self._releases.promoted_tag(tx)
            package = _package(self._releases.get(tx, promoted)) if promoted else None
            if package is None:
                return ManifestRefusal("app_unconfigured")
            if self._on_disk(tx, package):
                return package
            last_good = self._releases.last_good_tag(tx)
            fallback = _package(self._releases.get(tx, last_good)) if last_good else None
            if fallback is not None and self._on_disk(tx, fallback):
                return fallback
            return package

        return await self._in_tx(read)

    # -- operator actions -------------------------------------------------------------------------

    async def promote(self, tag: str) -> None:
        """Move the promoted pointer and publish its `.deb` fetch. The outgoing promoted tag
        becomes the last-good fallback if its `.deb` is on disk (otherwise the older last-good
        stays); re-promoting the promoted tag still writes, making the promotion the
        operator's."""
        _require_tag(tag)

        def write(tx: Transaction) -> None:
            release = self._releases.get(tx, tag)
            if release is None:
                raise CatalogError("release_not_found", "not_found")
            package = _package(release)
            if package is None:
                raise CatalogError("release_undeployable", "conflict")
            outgoing = self._releases.set_promoted(tx, tag)
            if outgoing is not None and outgoing.tag != tag:
                previous = _package(self._releases.get(tx, outgoing.tag))
                if previous is not None and self._on_disk(tx, previous):
                    self._releases.set_last_good(tx, outgoing.tag)
            self._publisher.publish(FetchPackage(sha256=package.sha256), within=tx,
                                    retry_terminal=True)
            self._publisher.publish(Prefetch(), within=tx)

        await self._in_tx(write)

    async def refresh(self) -> None:
        """Force a full listing: clear the stored ETag and publish the sync in one transaction
        (the operator's repair of a stale equal-version observation, design §6.4)."""
        def write(tx: Transaction) -> None:
            self._releases.store_etag(tx, None, now=self._clock.utc())
            self._publisher.publish(SyncReleases(), within=tx, retry_terminal=True)

        await self._in_tx(write)
        await self._publisher.publish_now(Prefetch())

    # -- operator views ---------------------------------------------------------------------------

    async def releases_view(self) -> tuple[ReleaseView, ...]:
        """Every release, semver DESC (non-semver legacy tags last)."""
        def read(tx: Transaction) -> tuple[ReleaseView, ...]:
            promotion = self._releases.promotion(tx)
            return tuple(
                ReleaseView(row.tag, row.is_prerelease, _package(row) is not None,
                            promotion.by if promotion and row.tag == promotion.tag else None,
                            row.os_image is not None)
                for row in _semver_desc(self._releases.all(tx))
            )

        return await self._in_tx(read)

    # -- internals --------------------------------------------------------------------------------

    def _resolve_package(self, tx: Transaction, request: PackageRequest) -> Resolution:
        """A desired `.deb` resolves (a miss fetches it); any other known one only while it is
        on disk, so an unauthenticated client can never make Central download an arbitrary
        historical `.deb`. An unknown sha costs one indexed lookup.

        The on-disk answer is a snapshot: a file removed between here and the reader's open is
        still fetched once. The cache cleaner (`MaintainCache`) removes only files `desired_in`
        does not name, past a grace, so a desired `.deb` it resolves here is never removed.
        """
        rows = [row for row in self._releases.shipping(tx, request.sha256)
                if _package(row) is not None]
        if not rows:
            return Unknown("unknown_package")
        job = FetchPackage(sha256=request.sha256)
        if ({row.tag for row in rows} & self._policy_tags(tx)
                or self._stored.present(tx, job)):
            return Candidates((job,), pinned=True)
        return Unknown("unknown_package")

    def _on_disk(self, tx: Transaction, package: DevicePackage) -> bool:
        return self._stored.present(tx, FetchPackage(sha256=package.sha256))

    async def _in_tx(self, body: Callable[[Transaction], T]) -> T:
        return await in_transaction(self._transactions, body)


def _semver_desc(rows: Iterable[ReleaseRow]) -> list[ReleaseRow]:
    """Rows by `release_version` order DESC; legacy non-semver tags last, by tag."""
    ranked: list[tuple[tuple[int, int, int, bool, str], ReleaseRow]] = []
    invalid: list[ReleaseRow] = []
    for row in rows:
        try:
            ranked.append((release_version(row.tag).order_key(), row))
        except ValueError:
            invalid.append(row)
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in ranked] + sorted(invalid, key=lambda row: row.tag)
