"""The catalog's seams: release rows and device rows over the existing tables, and disk presence.

`ReleaseRecords` covers `app_releases`, `app_release_policy`, `app_release_poll` and the
`bindings` count; `DeviceRecords` covers `devices`. Both take the kernel's opaque `Transaction`,
so the domain never sees psycopg (`central/infra/catalog_records.py` implements them).
`StoredAssets` answers "is this asset on disk now" (`central/infra/stored_assets.py`).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from central.kernel.assets import OriginLocator
from central.kernel.job_types import AssetJob
from central.kernel.ports import PublishedRelease
from central.kernel.transactions import Transaction

BootOutcome: TypeAlias = Literal["pending", "healthy", "failed"]
# Who set the current promotion (027's `promoted_by`). The periodic sync moves only an "auto"
# promotion; an "operator" one is never overridden (issue #23, owner ruling).
Promoter: TypeAlias = Literal["auto", "operator"]


@dataclass(frozen=True, slots=True)
class ReleaseRow:
    tag: str
    is_prerelease: bool
    package: OriginLocator | None  # the .deb (asset_url/asset_sha256/asset_size)
    os_image: OriginLocator | None  # the base tarball (base_tarball_url/_sha256/_size)
    divergent: bool = False  # the .deb was re-cut upstream after it was produced: frozen


@dataclass(frozen=True, slots=True)
class Promotion:
    tag: str
    by: Promoter


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
class NamedTags:
    """The DISTINCT tags active devices name; bounded by the release count, not the device count."""

    pinned: frozenset[str]
    known_good: frozenset[str]
    served: frozenset[str]  # last_served_tag, served within the window: what a device runs now


@dataclass(frozen=True, slots=True)
class DeviceUpdate:
    failed_tag: str | None  # the new devices.failed_tag
    mark_boot_failed: bool  # also set boot_outcome='failed'


class ReleaseRecords(Protocol):
    def get(self, tx: Transaction, tag: str) -> ReleaseRow | None: ...

    def all(self, tx: Transaction) -> tuple[ReleaseRow, ...]: ...

    def shipping(self, tx: Transaction, sha256: str) -> tuple[ReleaseRow, ...]:
        """The releases whose `.deb` sha is `sha256` (an indexed WHERE, never a scan)."""
        ...

    def upsert(self, tx: Transaction, release: PublishedRelease, *,
               now: float) -> ReleaseRow | None:
        """Write tag, semver columns, is_prerelease, asset_* and base_*; return the PREVIOUS row.

        None when the tag is new. On insert the legacy NOT NULL `mirror_state` is 'discovered'
        with a package, else 'undeployable'.
        """
        ...

    def mark_divergent(self, tx: Transaction, tag: str) -> None:
        """Freeze the tag's `.deb` facts (`mirror_state='divergent'`, main's re-cut rule)."""
        ...

    def promoted_tag(self, tx: Transaction) -> str | None: ...

    def promotion(self, tx: Transaction) -> Promotion | None:
        """The promoted tag and who set it; None when nothing is promoted."""
        ...

    def set_promoted(self, tx: Transaction, tag: str, *, by: Promoter) -> None:
        """Move the promoted pointer and record who moved it (`by` has no default)."""
        ...

    def last_good_tag(self, tx: Transaction) -> str | None:
        """The last promoted tag whose `.deb` was on disk (main's `current_sha256` pointer)."""
        ...

    def set_last_good(self, tx: Transaction, tag: str) -> None:
        """Only while a tag is promoted (the policy row exists)."""
        ...

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
        """Every device with retired_at IS NULL (the operator view; never a request path)."""
        ...

    def known_good_tags(self, tx: Transaction) -> frozenset[str]:
        """DISTINCT known_good_tag of active devices: the frontier's input, per netboot."""
        ...

    def named_tags(self, tx: Transaction, *, served_since: float) -> NamedTags:
        """The served role counts a device only when `last_served_at >= served_since`."""
        ...

    def names_any(self, tx: Transaction, tags: Collection[str], *, served_since: float) -> bool:
        """Whether an active device pins, ran healthy on, or was last served (at or after
        `served_since`) one of `tags`: the same predicate per role as `named_tags`."""
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


class StoredAssets(Protocol):
    def present(self, tx: Transaction, job: AssetJob) -> bool:
        """The asset has produced facts and its file is on disk now (a snapshot: the cache is
        ephemeral, so a caller must still survive the file going away)."""
        ...
