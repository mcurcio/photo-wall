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
from central.content_catalog.ports import (
    DeviceRecords,
    DeviceRow,
    ReleaseRecords,
    ReleaseRow,
    StoredAssets,
)
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


def _bootable(releases: Iterable[ReleaseRow]) -> tuple[str, ...]:
    """The full releases that ship an OS image: the bootstrap pool and the substitute pool."""
    return tuple(r.tag for r in releases if not r.is_prerelease and r.os_image is not None)


def _bootstrap(releases: Iterable[ReleaseRow]) -> str | None:
    """The newest full release that ships an OS image: the empty-frontier target only."""
    return newest(_bootable(releases))


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
                 stored: StoredAssets, transactions: Transactions, publisher: Publisher,
                 clock: Clock) -> None:
        self._releases = releases
        self._devices = devices
        self._stored = stored
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

        OS images and `.deb`s for active devices' pins, known-goods and last-served tags (what
        they run now) and `frontier or bootstrap`; the `.deb` of the promoted and the last-good
        tag. Tags without the locator are skipped.
        """
        named = self._devices.named_tags(tx)
        by_tag = {row.tag: row for row in self._releases.all(tx)}
        tags = set(named.pinned | named.known_good | named.served)
        target = newest(named.known_good) or _bootstrap(by_tag.values())
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
        for tag in self._policy_tags(tx):
            if (package := _package(by_tag.get(tag))) is not None:
                jobs.add(FetchPackage(sha256=package.sha256))
        return frozenset(jobs)

    def _policy_tags(self, tx: Transaction) -> set[str]:
        """The promoted and the last-good tag: the `.deb`s `/v1/app/manifest` may name."""
        tags = {self._releases.promoted_tag(tx), self._releases.last_good_tag(tx)}
        return {tag for tag in tags if tag is not None}

    def _package_desired(self, tx: Transaction, tags: frozenset[str]) -> bool:
        """Whether `desired_in` names the `.deb` these tags ship, without computing the set.

        The same three sources as `desired_in`: a device-named tag, a policy tag, or
        `frontier or bootstrap`.
        """
        if self._devices.names_any(tx, tags) or tags & self._policy_tags(tx):
            return True
        frontier = newest(self._devices.known_good_tags(tx))
        return (frontier or _bootstrap(self._releases.all(tx))) in tags

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
            self.promote_in(tx, tag)
            self._publisher.publish(FetchPackage(sha256=package.sha256), within=tx,
                                    retry_terminal=True)
            self._publisher.publish(Prefetch(), within=tx)

        await self._in_tx(write)

    def promote_in(self, tx: Transaction, tag: str) -> None:
        """Move the promoted pointer to a known tag with a `.deb` (the caller checked that).

        The outgoing promoted tag becomes the last-good fallback when its `.deb` is on disk
        (otherwise the older last-good stays). The operator route and auto-promote both use it.
        """
        outgoing = self._releases.promoted_tag(tx)
        if outgoing is not None and outgoing != tag:
            package = _package(self._releases.get(tx, outgoing))
            if package is not None and self._on_disk(tx, package):
                self._releases.set_last_good(tx, outgoing)
        self._releases.set_promoted(tx, tag)

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
            return NetbootView(newest(self._devices.known_good_tags(tx)),
                               self._devices.active(tx))

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
        choice = choose_base(device, frontier=newest(self._devices.known_good_tags(tx)),
                             bootstrap=_bootstrap(releases), substitutes=_bootable(releases))
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
        """A desired `.deb` resolves (a miss fetches it); any other known one only while it is
        on disk, so an unauthenticated client can never make Central download an arbitrary
        historical `.deb`. An unknown sha costs one indexed lookup.

        The on-disk answer is a snapshot: a file removed between here and the reader's open is
        still fetched once. No cache cleanup ships in the MVP, so today nothing removes one.
        """
        tags = frozenset(row.tag for row in self._releases.shipping(tx, request.sha256)
                         if _package(row) is not None)
        if not tags:
            return Unknown("unknown_package")
        job = FetchPackage(sha256=request.sha256)
        if self._package_desired(tx, tags) or self._stored.present(tx, job):
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
