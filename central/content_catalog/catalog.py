"""`ReleaseCatalog`: the release/device catalog behind the kernel's `ContentCatalog` port.

It ports the release policy of `central/netboot_base.py` (selection, pin, served-tag carry) and
`central/app_releases.py` (promotion) behind `ReleaseRecords`/`DeviceRecords`. Every public async
method runs its blocking work in `asyncio.to_thread` and opens its own `transactions.begin()`, so
nothing blocks the event loop.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, TypeVar

from pydantic import ValidationError

from central.content_catalog.boot_policy import choose_base, newest
from central.content_catalog.ports import DeviceRecords, DeviceRow, ReleaseRecords, ReleaseRow
from central.kernel.job_types import AssetJob, FetchOsImage, FetchPackage, Prefetch, SyncReleases
from central.kernel.ports import (
    Candidates,
    ContentRequest,
    NetbootBaseRequest,
    PackageRequest,
    Resolution,
    Unknown,
)
from central.kernel.publishing import Publisher
from central.kernel.transactions import Transaction, Transactions
from central.kernel.types import release_version
from contracts.equipment import equipment_device_id
from contracts.time import Clock

DEVICE_KIND = "pi"  # the equipment_device_id kind a netbooting Pi derives its id with
_SAFE_SERIAL = re.compile(r"[A-Za-z0-9:_.-]{1,128}")  # ported from netboot_base

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DevicePackage:
    tag: str
    version: str  # == tag (was the app_packages label)
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ManifestRefusal:
    code: Literal["app_manifest_unresolved", "app_unconfigured", "app_manifest_undeployable"]


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
    promoted: bool
    has_os_image: bool


@dataclass(frozen=True, slots=True)
class NetbootView:
    frontier: str | None
    devices: tuple[DeviceRow, ...]


async def in_transaction(transactions: Transactions, body: Callable[[Transaction], T]) -> T:
    """Run `body` in one `transactions.begin()` on a worker thread, off the event loop."""
    def run() -> T:
        with transactions.begin() as tx:
            return body(tx)

    return await asyncio.to_thread(run)


def _os_image_job(tag: str) -> FetchOsImage | None:
    try:
        return FetchOsImage(tag=tag)
    except ValidationError:  # a legacy non-semver tag cannot name an asset job
        return None


def _frontier(active: Iterable[DeviceRow]) -> str | None:
    """max(semver) over non-retired devices' known-good tags (the live frontier)."""
    return newest(row.known_good_tag for row in active if row.known_good_tag is not None)


def _bootstrap(releases: Iterable[ReleaseRow]) -> str | None:
    """The newest full release that ships an OS image: the empty-frontier target only."""
    return newest(r.tag for r in releases if not r.is_prerelease and r.os_image is not None)


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
    """Implements the kernel's `ContentCatalog` for OS images and Player `.deb`s."""

    def __init__(self, *, releases: ReleaseRecords, devices: DeviceRecords,
                 transactions: Transactions, publisher: Publisher, clock: Clock) -> None:
        self._releases = releases
        self._devices = devices
        self._transactions = transactions
        self._publisher = publisher
        self._clock = clock

    # -- ContentCatalog ---------------------------------------------------------------------------

    async def resolve(self, request: ContentRequest) -> Resolution:
        if isinstance(request, NetbootBaseRequest):
            return await self._in_tx(lambda tx: self._resolve_base(tx, request))
        if isinstance(request, PackageRequest):
            return await self._in_tx(lambda tx: self._resolve_package(tx, request))
        raise TypeError(f"not a content request: {request!r}")

    async def desired_assets(self) -> frozenset[AssetJob]:
        return await self._in_tx(self.desired_in)

    def desired_in(self, tx: Transaction) -> frozenset[AssetJob]:
        """The desired set, read inside the caller's transaction (the sync tail uses it).

        OS images for active pins, active known-goods and `frontier or bootstrap`; the `.deb`
        of each of those tags plus the promoted tag's. Tags without the locator are skipped.
        """
        active = self._devices.active(tx)
        by_tag = {row.tag: row for row in self._releases.all(tx)}
        tags = {row.attached_tag for row in active if row.attached_tag is not None}
        tags |= {row.known_good_tag for row in active if row.known_good_tag is not None}
        target = _frontier(active) or _bootstrap(by_tag.values())
        if target is not None:
            tags.add(target)
        jobs: set[AssetJob] = set()
        for tag in tags:
            row = by_tag.get(tag)
            if row is None:
                continue
            if row.os_image is not None and (job := _os_image_job(tag)) is not None:
                jobs.add(job)
            if (package := _package(row)) is not None:
                jobs.add(FetchPackage(sha256=package.sha256))
        promoted = self._releases.promoted_tag(tx)
        if promoted is not None and (package := _package(by_tag.get(promoted))) is not None:
            jobs.add(FetchPackage(sha256=package.sha256))
        return frozenset(jobs)

    # -- serving ----------------------------------------------------------------------------------

    async def record_served(self, request: NetbootBaseRequest, job: FetchOsImage) -> None:
        """After a 200 only: record the tag whose bytes were served (never on a 503 miss)."""
        device_id = self._device_id(request.serial)
        if device_id is None:
            return

        def write(tx: Transaction) -> None:
            self._devices.record_served(tx, device_id, job.tag, now=self._clock.utc())

        await self._in_tx(write)

    async def device_package(self, serial: str | None) -> DevicePackage | ManifestRefusal:
        """The `.deb` of the tag this device was actually served this boot (F4)."""
        device_id = self._device_id(serial)
        if device_id is None:
            return ManifestRefusal("app_manifest_unresolved")

        def read(tx: Transaction) -> DevicePackage | ManifestRefusal:
            row = self._devices.get(tx, device_id)
            if row is None or row.last_served_tag is None:
                return ManifestRefusal("app_manifest_unresolved")
            package = _package(self._releases.get(tx, row.last_served_tag))
            return package if package is not None else ManifestRefusal("app_manifest_undeployable")

        return await self._in_tx(read)

    async def promoted_package(self) -> DevicePackage | ManifestRefusal:
        def read(tx: Transaction) -> DevicePackage | ManifestRefusal:
            promoted = self._releases.promoted_tag(tx)
            package = _package(self._releases.get(tx, promoted)) if promoted else None
            return package if package is not None else ManifestRefusal("app_unconfigured")

        return await self._in_tx(read)

    # -- operator actions -------------------------------------------------------------------------

    async def pin(self, device_id: str, tag: str) -> None:
        _require_tag(tag)

        def write(tx: Transaction) -> None:
            release = self._releases.get(tx, tag)
            if release is None:
                raise CatalogError("release_not_found", "not_found")
            if not self._devices.set_pin(tx, device_id, tag):
                raise CatalogError("device_not_found", "not_found")  # rolls back: no write
            self._publisher.publish(FetchOsImage(tag=tag), within=tx, retry_terminal=True)
            if (package := _package(release)) is not None:
                self._publisher.publish(FetchPackage(sha256=package.sha256), within=tx,
                                        retry_terminal=True)
            self._publisher.publish(Prefetch(), within=tx)

        await self._in_tx(write)

    async def unpin(self, device_id: str) -> None:
        def write(tx: Transaction) -> None:
            if not self._devices.set_pin(tx, device_id, None):
                raise CatalogError("device_not_found", "not_found")
            self._publisher.publish(Prefetch(), within=tx)

        await self._in_tx(write)

    async def promote(self, tag: str) -> None:
        _require_tag(tag)

        def write(tx: Transaction) -> None:
            release = self._releases.get(tx, tag)
            if release is None:
                raise CatalogError("release_not_found", "not_found")
            package = _package(release)
            if package is None:
                raise CatalogError("release_undeployable", "conflict")
            self._releases.set_promoted(tx, tag)
            self._publisher.publish(FetchPackage(sha256=package.sha256), within=tx,
                                    retry_terminal=True)
            self._publisher.publish(Prefetch(), within=tx)

        await self._in_tx(write)

    async def refresh(self) -> None:
        await self._publisher.publish_now(SyncReleases(), retry_terminal=True)
        await self._publisher.publish_now(Prefetch())

    # -- operator views ---------------------------------------------------------------------------

    async def releases_view(self) -> tuple[ReleaseView, ...]:
        """Every release, semver DESC (non-semver legacy tags last)."""
        def read(tx: Transaction) -> tuple[ReleaseView, ...]:
            promoted = self._releases.promoted_tag(tx)
            return tuple(
                ReleaseView(row.tag, row.is_prerelease, _package(row) is not None,
                            row.tag == promoted, row.os_image is not None)
                for row in _semver_desc(self._releases.all(tx))
            )

        return await self._in_tx(read)

    async def netboot_view(self) -> NetbootView:
        def read(tx: Transaction) -> NetbootView:
            active = self._devices.active(tx)
            return NetbootView(_frontier(active), active)

        return await self._in_tx(read)

    def sanitize_serial(self, serial: str | None) -> str | None:
        """The serial if it matches the safe charset, else None (the route logs this value)."""
        if serial is not None and _SAFE_SERIAL.fullmatch(serial):
            return serial
        return None

    # -- internals --------------------------------------------------------------------------------

    def _device_id(self, serial: str | None) -> str | None:
        serial = self.sanitize_serial(serial)
        return None if serial is None else equipment_device_id(DEVICE_KIND, serial.encode())

    def _resolve_base(self, tx: Transaction, request: NetbootBaseRequest) -> Resolution:
        serial = self.sanitize_serial(request.serial)
        device_id = self._device_id(serial)
        device = None
        if serial is not None and device_id is not None:
            device = self._devices.lock(tx, device_id, serial, now=self._clock.utc())
        releases = self._releases.all(tx)
        choice = choose_base(device, frontier=_frontier(self._devices.active(tx)),
                             bootstrap=_bootstrap(releases))
        if choice is None:
            return Unknown("no_release")
        if choice.update is not None and device is not None:
            self._devices.apply(tx, device.device_id, choice.update)
        with_image = {row.tag for row in releases if row.os_image is not None}
        jobs = tuple(job for tag in choice.tags if tag in with_image
                     and (job := _os_image_job(tag)) is not None)
        if not jobs:
            return Unknown("no_release")
        return Candidates(jobs, pinned=choice.pinned)

    def _resolve_package(self, tx: Transaction, request: PackageRequest) -> Resolution:
        if any(row.package is not None and row.package.sha256 == request.sha256
               for row in self._releases.all(tx)):
            return Candidates((FetchPackage(sha256=request.sha256),), pinned=True)
        return Unknown("unknown_package")

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
