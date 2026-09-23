"""In-memory `ReleaseRecords` / `DeviceRecords` / `StoredAssets` for the content-catalog lane.

They follow the Postgres repositories' semantics (`central/infra/catalog_records.py`) and assert
every call runs inside an open transaction. Writes apply immediately (there is no rollback).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace

from central.content_catalog.ports import DeviceRow, DeviceUpdate, NamedTags, ReleaseRow
from central.kernel.job_types import AssetJob
from central.kernel.ports import PublishedRelease
from central.kernel.transactions import Transaction


def _require_open(tx: Transaction) -> None:
    if tx.state != "open":
        raise AssertionError(f"catalog records used outside an open transaction ({tx.state})")


def release_row(release: PublishedRelease) -> ReleaseRow:
    return ReleaseRow(release.tag, release.is_prerelease, release.package, release.os_image)


class InMemoryReleaseRecords:
    """Implements `ReleaseRecords`; `rows`, `promoted`, `last_good`, `etag` and `bound` are the
    stored state."""

    def __init__(self, rows: tuple[ReleaseRow, ...] = (), *, promoted: str | None = None,
                 last_good: str | None = None, etag: str | None = None, bound: int = 0) -> None:
        self.rows: dict[str, ReleaseRow] = {row.tag: row for row in rows}
        self.promoted = promoted
        self.last_good = last_good
        self.etag = etag
        self.bound = bound
        self.upserted_at: dict[str, float] = {}

    def get(self, tx: Transaction, tag: str) -> ReleaseRow | None:
        _require_open(tx)
        return self.rows.get(tag)

    def all(self, tx: Transaction) -> tuple[ReleaseRow, ...]:
        _require_open(tx)
        return tuple(self.rows.values())

    def shipping(self, tx: Transaction, sha256: str) -> tuple[ReleaseRow, ...]:
        _require_open(tx)
        return tuple(row for _, row in sorted(self.rows.items())
                     if row.package is not None and row.package.sha256 == sha256)

    def upsert(self, tx: Transaction, release: PublishedRelease, *,
               now: float) -> ReleaseRow | None:
        _require_open(tx)
        previous = self.rows.get(release.tag)
        divergent = previous is not None and previous.divergent  # mirror_state survives updates
        self.rows[release.tag] = replace(release_row(release), divergent=divergent)
        self.upserted_at[release.tag] = now
        return previous

    def mark_divergent(self, tx: Transaction, tag: str) -> None:
        _require_open(tx)
        self.rows[tag] = replace(self.rows[tag], divergent=True)

    def promoted_tag(self, tx: Transaction) -> str | None:
        _require_open(tx)
        return self.promoted

    def set_promoted(self, tx: Transaction, tag: str) -> None:
        _require_open(tx)
        if tag not in self.rows:  # the app_release_policy FK
            raise AssertionError(f"promoted tag {tag} is not a release")
        self.promoted = tag

    def last_good_tag(self, tx: Transaction) -> str | None:
        _require_open(tx)
        return self.last_good

    def set_last_good(self, tx: Transaction, tag: str) -> None:
        _require_open(tx)
        if self.promoted is None:  # the policy row exists only once something is promoted
            raise AssertionError("no app_release_policy row")
        if tag not in self.rows:
            raise AssertionError(f"last-good tag {tag} is not a release")
        self.last_good = tag

    def load_etag(self, tx: Transaction) -> str | None:
        _require_open(tx)
        return self.etag

    def store_etag(self, tx: Transaction, etag: str | None) -> None:
        _require_open(tx)
        self.etag = etag

    def bound_player_count(self, tx: Transaction) -> int:
        _require_open(tx)
        return self.bound


def device(device_id: str, *, serial: str | None = None, attached_tag: str | None = None,
           known_good_tag: str | None = None, last_served_tag: str | None = None,
           boot_outcome: str | None = None, failed_tag: str | None = None,
           last_served_at: float | None = None, retired: bool = False) -> DeviceRow:
    """A `DeviceRow` with every field defaulted to the freshly-seen state."""
    return DeviceRow(device_id, serial, attached_tag, known_good_tag, last_served_tag,
                     boot_outcome, failed_tag, last_served_at, retired)  # type: ignore[arg-type]


class InMemoryDeviceRecords:
    """Implements `DeviceRecords`; `rows` is the stored state, `seen` the last_seen per device."""

    def __init__(self, rows: tuple[DeviceRow, ...] = ()) -> None:
        self.rows: dict[str, DeviceRow] = {row.device_id: row for row in rows}
        self.seen: dict[str, float] = {}
        self.locked: list[str] = []

    def lock(self, tx: Transaction, device_id: str, serial: str, *, now: float) -> DeviceRow:
        _require_open(tx)
        row = self.rows.get(device_id)
        self.rows[device_id] = (device(device_id, serial=serial) if row is None
                                else replace(row, serial=serial))
        self.seen[device_id] = now
        self.locked.append(device_id)
        return self.rows[device_id]

    def active(self, tx: Transaction) -> tuple[DeviceRow, ...]:
        _require_open(tx)
        return tuple(row for _, row in sorted(self.rows.items()) if not row.retired)

    def known_good_tags(self, tx: Transaction) -> frozenset[str]:
        return self.named_tags(tx).known_good

    def named_tags(self, tx: Transaction) -> NamedTags:
        _require_open(tx)  # the Pg version is a DISTINCT per role, never a row-per-device read
        active = [row for row in self.rows.values() if not row.retired]
        return NamedTags(
            frozenset(row.attached_tag for row in active if row.attached_tag is not None),
            frozenset(row.known_good_tag for row in active if row.known_good_tag is not None),
            frozenset(row.last_served_tag for row in active if row.last_served_tag is not None))

    def names_any(self, tx: Transaction, tags: Collection[str]) -> bool:
        named = self.named_tags(tx)
        return bool(set(tags) & (named.pinned | named.known_good | named.served))

    def get(self, tx: Transaction, device_id: str) -> DeviceRow | None:
        _require_open(tx)
        return self.rows.get(device_id)

    def apply(self, tx: Transaction, device_id: str, update: DeviceUpdate) -> None:
        _require_open(tx)
        row = self.rows.get(device_id)
        if row is None:
            return
        self.rows[device_id] = replace(
            row, failed_tag=update.failed_tag,
            boot_outcome="failed" if update.mark_boot_failed else row.boot_outcome)

    def record_served(self, tx: Transaction, device_id: str, tag: str, *, now: float) -> None:
        _require_open(tx)
        row = self.rows.get(device_id)
        if row is not None:
            self.rows[device_id] = replace(row, last_served_tag=tag, boot_outcome="pending",
                                           last_served_at=now)

    def set_pin(self, tx: Transaction, device_id: str, tag: str | None) -> bool:
        _require_open(tx)
        row = self.rows.get(device_id)
        if row is None:
            return False
        self.rows[device_id] = replace(row, attached_tag=tag)
        return True

    def sweep_failed_boots(self, tx: Transaction, *, served_before: float) -> int:
        _require_open(tx)
        swept = 0
        for device_id, row in list(self.rows.items()):
            if (row.boot_outcome == "pending" and not row.retired
                    and row.last_served_at is not None and row.last_served_at < served_before):
                fence = row.failed_tag if row.failed_tag is not None else (
                    row.attached_tag if row.attached_tag is not None else row.last_served_tag)
                self.rows[device_id] = replace(row, boot_outcome="failed", failed_tag=fence)
                swept += 1
        return swept


class InMemoryStoredAssets:
    """Implements `StoredAssets`; `on_disk` is the set of jobs whose asset is present."""

    def __init__(self, on_disk: Collection[AssetJob] = ()) -> None:
        self.on_disk: set[AssetJob] = set(on_disk)

    def present(self, tx: Transaction, job: AssetJob) -> bool:
        _require_open(tx)
        return job in self.on_disk
