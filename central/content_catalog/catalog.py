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
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, TypeVar, overload

from pydantic import ValidationError

from central.content_catalog.boot_policy import choose_base, newest
from central.content_catalog.ports import (
    DeviceRecords,
    DeviceRow,
    Promoter,
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

# The `equipment_device_id` kind, shared with appliance/bootstrap.py and player/service.py via
# contracts.equipment, so one Pi resolves to one device_id at netboot and at enrollment.
DEVICE_KIND = "pi"
# The only shape a client serial may take before it can select an image or be logged: a Pi serial
# is 16 hex digits; a small safe superset leaves room for another scheme. The serial is
# unauthenticated, so control characters, separators and whitespace are refused here.
_SAFE_SERIAL = re.compile(r"[A-Za-z0-9:_.-]{1,128}")
# How long a device's last served tag stays desired (owner ruling, issue #25). The netboot route
# is unauthenticated, so any serial mints a device row that names what it was served; without a
# window those tags stayed desired forever. Pins and known-goods are not aged.
SERVED_TAG_WINDOW = timedelta(days=30)

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
    promoted_by: Promoter | None  # who promoted this release; None when it is not promoted
    has_os_image: bool
    promoted: bool = field(init=False)  # derived from promoted_by, so the two cannot disagree

    def __post_init__(self) -> None:
        object.__setattr__(self, "promoted", self.promoted_by is not None)


@dataclass(frozen=True, slots=True)
class NetbootCandidates(Candidates):
    """The OS images a netboot request may be served, with the tag each one is served as.

    One candidate per distinct base tarball (its content key), tagged with the first tag in
    preference order that ships it: two tags sharing a tarball are one asset.
    """

    tags: tuple[str, ...]  # same length and order as `jobs`

    def __post_init__(self) -> None:
        Candidates.__post_init__(self)  # slots=True: a zero-argument super() cannot bind here
        if (not isinstance(self.tags, tuple) or len(self.tags) != len(self.jobs)
                or not all(isinstance(tag, str) for tag in self.tags)
                or not all(isinstance(job, FetchOsImage) for job in self.jobs)):
            raise ValueError("invalid_netboot_candidates")


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


def sanitize_serial(serial: str | None) -> str | None:
    """The serial if it matches the safe charset, else None (the route logs only this value)."""
    if serial is not None and _SAFE_SERIAL.fullmatch(serial):
        return serial
    return None


def device_id_for_serial(serial: str | None) -> str | None:
    """The canonical `device-<64hex>` id of a safe serial, else None."""
    serial = sanitize_serial(serial)
    return None if serial is None else equipment_device_id(DEVICE_KIND, serial.encode())


def _os_image_job(row: ReleaseRow) -> FetchOsImage | None:
    """The fetch of the row's OS image, keyed by its base tarball's sha256; None without one.

    The one rule for which OS images the catalog offers (`_resolve_base`) and wants
    (`desired_in`, `pin`): a legacy tag that `release_version` refuses is never a netboot
    candidate or substitute and never desired, as before the image was keyed by its tarball
    (the tag-keyed job refused such a tag). The key no longer checks the tag, so this does.
    """
    if row.os_image is None or row.os_image.sha256 is None:
        return None
    try:
        release_version(row.tag)
    except ValueError:
        return None
    try:
        return FetchOsImage(tarball_sha256=row.os_image.sha256)
    except ValidationError:  # a legacy row whose stored sha cannot name an asset job
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

    @overload
    async def resolve(self, request: NetbootBaseRequest) -> NetbootCandidates | Unknown: ...

    @overload
    async def resolve(self, request: ContentRequest) -> Resolution: ...

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
        they run now; only those served within `SERVED_TAG_WINDOW`) and `frontier or bootstrap`;
        the `.deb` of the promoted and the last-good tag. Tags without the locator are skipped, and so is a frozen (divergent) tag's `.deb`
        once its file is gone: upstream serves other bytes for it, so fetching it can only fail.
        """
        named = self._devices.named_tags(tx, served_since=self._served_since())
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
            if (job := _os_image_job(row)) is not None:
                jobs.add(job)
            if (package := self._obtainable(tx, row)) is not None:
                jobs.add(FetchPackage(sha256=package.sha256))
        for tag in self._policy_tags(tx):
            if (package := self._obtainable(tx, by_tag.get(tag))) is not None:
                jobs.add(FetchPackage(sha256=package.sha256))
        return frozenset(jobs)

    def _obtainable(self, tx: Transaction, row: ReleaseRow | None) -> DevicePackage | None:
        """The row's `.deb`, unless the tag is frozen and its file is gone (unobtainable)."""
        package = _package(row)
        if package is not None and row is not None and row.divergent and not self._on_disk(
                tx, package):
            return None
        return package

    def _served_since(self) -> float:
        """The oldest serve that still makes a device's last served tag desired."""
        return self._clock.utc() - SERVED_TAG_WINDOW.total_seconds()

    def _policy_tags(self, tx: Transaction) -> set[str]:
        """The promoted and the last-good tag: the `.deb`s `/v1/app/manifest` may name."""
        tags = {self._releases.promoted_tag(tx), self._releases.last_good_tag(tx)}
        return {tag for tag in tags if tag is not None}

    def _package_desired(self, tx: Transaction, tags: frozenset[str]) -> bool:
        """Whether `desired_in` names the `.deb` these tags ship, without computing the set.

        The same three sources as `desired_in`: a device-named tag, a policy tag, or
        `frontier or bootstrap`.
        """
        if (self._devices.names_any(tx, tags, served_since=self._served_since())
                or tags & self._policy_tags(tx)):
            return True
        frontier = newest(self._devices.known_good_tags(tx))
        return (frontier or _bootstrap(self._releases.all(tx))) in tags

    # -- serving ----------------------------------------------------------------------------------

    async def record_served(self, request: NetbootBaseRequest, resolution: NetbootCandidates,
                            job: FetchOsImage) -> None:
        """After a 200 only: record the tag whose bytes were served (never on a 503 miss).

        `job` is the candidate the reader served; the tag is the one `resolution` served it as.
        """
        device_id = device_id_for_serial(request.serial)
        if device_id is None:
            return
        tag = resolution.tags[resolution.jobs.index(job)]

        def write(tx: Transaction) -> None:
            self._devices.record_served(tx, device_id, tag, now=self._clock.utc())

        await self._in_tx(write)

    async def device_package(self, serial: str | None) -> DevicePackage | ManifestRefusal:
        """The `.deb` of the tag this device was actually served this boot (F4)."""
        device_id = device_id_for_serial(serial)
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
            if (image := _os_image_job(release)) is not None:
                self._publisher.publish(image, within=tx, retry_terminal=True)
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
            self.promote_in(tx, tag, by="operator")
            self._publisher.publish(FetchPackage(sha256=package.sha256), within=tx,
                                    retry_terminal=True)
            self._publisher.publish(Prefetch(), within=tx)

        await self._in_tx(write)

    def promote_in(self, tx: Transaction, tag: str, *, by: Promoter) -> bool:
        """Move the promoted pointer to a known tag with a `.deb` (the caller checked that), and
        record who moved it: the operator route passes "operator", auto-promote "auto". Returns
        whether it moved: `set_promoted` refuses an "auto" move of an "operator" promotion,
        including one committed after the caller's own read.

        When it moved, the outgoing promoted tag (read under the policy row's lock by the same
        write) becomes the last-good fallback if its `.deb` is on disk (otherwise the older
        last-good stays). An operator re-promoting the sync's current tag still writes: the
        promotion becomes the operator's.
        """
        write = self._releases.set_promoted(tx, tag, by=by)
        outgoing = write.outgoing
        if write.moved and outgoing is not None and outgoing.tag != tag:
            package = _package(self._releases.get(tx, outgoing.tag))
            if package is not None and self._on_disk(tx, package):
                self._releases.set_last_good(tx, outgoing.tag)
        return write.moved

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

    async def netboot_view(self) -> NetbootView:
        def read(tx: Transaction) -> NetbootView:
            return NetbootView(newest(self._devices.known_good_tags(tx)),
                               self._devices.active(tx))

        return await self._in_tx(read)

    # -- internals --------------------------------------------------------------------------------

    def _resolve_base(self, tx: Transaction,
                      request: NetbootBaseRequest) -> NetbootCandidates | Unknown:
        serial = sanitize_serial(request.serial)
        device_id = device_id_for_serial(serial)
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
        by_tag = {row.tag: row for row in releases}
        jobs: list[FetchOsImage] = []
        tags: list[str] = []
        for tag in choice.tags:  # preference order: the first tag shipping a tarball names it
            row = by_tag.get(tag)
            job = _os_image_job(row) if row is not None else None
            if job is not None and job not in jobs:
                jobs.append(job)
                tags.append(tag)
        if not jobs:
            return Unknown("no_release")
        return NetbootCandidates(tuple(jobs), pinned=choice.pinned, tags=tuple(tags))

    def _resolve_package(self, tx: Transaction, request: PackageRequest) -> Resolution:
        """A desired `.deb` resolves (a miss fetches it); any other known one only while it is
        on disk, so an unauthenticated client can never make Central download an arbitrary
        historical `.deb`. An unknown sha costs one indexed lookup.

        The on-disk answer is a snapshot: a file removed between here and the reader's open is
        still fetched once. No cache cleanup ships in the MVP, so today nothing removes one.
        A frozen (divergent) tag never makes its `.deb` desired here (see `desired_in`).
        """
        rows = [row for row in self._releases.shipping(tx, request.sha256)
                if _package(row) is not None]
        if not rows:
            return Unknown("unknown_package")
        job = FetchPackage(sha256=request.sha256)
        live = frozenset(row.tag for row in rows if not row.divergent)
        if (live and self._package_desired(tx, live)) or self._stored.present(tx, job):
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
