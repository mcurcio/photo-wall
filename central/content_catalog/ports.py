"""The catalog's persistence seams: release rows and device rows over the existing tables.

`ReleaseRecords` covers `app_releases`, `app_release_policy`, `app_release_poll` and the
`bindings` count; `DeviceRecords` covers `devices`. Both take the kernel's opaque `Transaction`,
so the domain never sees psycopg (`central/infra/catalog_records.py` implements them).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from central.kernel.assets import OriginLocator
from central.kernel.ports import PublishedRelease
from central.kernel.transactions import Transaction

BootOutcome: TypeAlias = Literal["pending", "healthy", "failed"]


@dataclass(frozen=True, slots=True)
class ReleaseRow:
    tag: str
    is_prerelease: bool
    package: OriginLocator | None  # the .deb (asset_url/asset_sha256/asset_size)
    os_image: OriginLocator | None  # the base tarball (base_tarball_url/_sha256/_size)


@dataclass(frozen=True, slots=True)
class DeviceRow:
    device_id: str
    serial: str | None
    attached_tag: str | None  # the operator pin
    known_good_tag: str | None
    last_served_tag: str | None
    boot_outcome: BootOutcome | None
    failed_tag: str | None  # the sticky rollback fence
    last_served_at: float | None
    retired: bool


@dataclass(frozen=True, slots=True)
class DeviceUpdate:
    failed_tag: str | None  # the new devices.failed_tag
    mark_boot_failed: bool  # also set boot_outcome='failed'


class ReleaseRecords(Protocol):
    def get(self, tx: Transaction, tag: str) -> ReleaseRow | None: ...

    def all(self, tx: Transaction) -> tuple[ReleaseRow, ...]: ...

    def upsert(self, tx: Transaction, release: PublishedRelease, *,
               now: float) -> ReleaseRow | None:
        """Write tag, semver columns, is_prerelease, asset_* and base_*; return the PREVIOUS row.

        None when the tag is new. On insert the legacy NOT NULL `mirror_state` is 'discovered'
        with a package, else 'undeployable'; nothing reads it.
        """
        ...

    def promoted_tag(self, tx: Transaction) -> str | None: ...

    def set_promoted(self, tx: Transaction, tag: str) -> None: ...

    def load_etag(self, tx: Transaction) -> str | None: ...

    def store_etag(self, tx: Transaction, etag: str | None) -> None: ...

    def bound_player_count(self, tx: Transaction) -> int:
        """count(DISTINCT player_id) FROM bindings."""
        ...


class DeviceRecords(Protocol):
    def lock(self, tx: Transaction, device_id: str, serial: str, *, now: float) -> DeviceRow:
        """Upsert first_seen/last_seen/serial, then SELECT ... FOR UPDATE (E1)."""
        ...

    def active(self, tx: Transaction) -> tuple[DeviceRow, ...]:
        """Every device with retired_at IS NULL."""
        ...

    def get(self, tx: Transaction, device_id: str) -> DeviceRow | None: ...

    def apply(self, tx: Transaction, device_id: str, update: DeviceUpdate) -> None: ...

    def record_served(self, tx: Transaction, device_id: str, tag: str, *, now: float) -> None:
        """last_served_tag=tag, boot_outcome='pending', last_served_at=now."""
        ...

    def set_pin(self, tx: Transaction, device_id: str, tag: str | None) -> bool:
        """Set or clear attached_tag; False when no such device."""
        ...

    def sweep_failed_boots(self, tx: Transaction, *, served_before: float) -> int:
        """Fail devices left 'pending' since before `served_before` (E2/E2b); return the count.

        Fences COALESCE(attached_tag, last_served_tag) only when failed_tag IS NULL.
        """
        ...
