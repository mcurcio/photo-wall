"""In-memory `ReleaseRecords` / `DeviceRecords` for the content-catalog lane.

They follow the Postgres repositories' semantics (`central/infra/catalog_records.py`) and assert
every call runs inside an open transaction. Writes apply immediately (there is no rollback).
"""

from __future__ import annotations

from dataclasses import replace

from central.content_catalog.ports import DeviceRow, DeviceUpdate, ReleaseRow
from central.kernel.ports import PublishedRelease
from central.kernel.transactions import Transaction


def _require_open(tx: Transaction) -> None:
    if tx.state != "open":
        raise AssertionError(f"catalog records used outside an open transaction ({tx.state})")


def release_row(release: PublishedRelease) -> ReleaseRow:
    return ReleaseRow(release.tag, release.is_prerelease, release.package, release.os_image)


class InMemoryReleaseRecords:
    """Implements `ReleaseRecords`; `rows`, `promoted`, `etag` and `bound` are the stored state."""

    def __init__(self, rows: tuple[ReleaseRow, ...] = (), *, promoted: str | None = None,
                 etag: str | None = None, bound: int = 0) -> None:
        self.rows: dict[str, ReleaseRow] = {row.tag: row for row in rows}
        self.promoted = promoted
        self.etag = etag
        self.bound = bound
        self.upserted_at: dict[str, float] = {}

    def get(self, tx: Transaction, tag: str) -> ReleaseRow | None:
        _require_open(tx)
        return self.rows.get(tag)

    def all(self, tx: Transaction) -> tuple[ReleaseRow, ...]:
        _require_open(tx)
        return tuple(self.rows.values())

    def upsert(self, tx: Transaction, release: PublishedRelease, *,
               now: float) -> ReleaseRow | None:
        _require_open(tx)
        previous = self.rows.get(release.tag)
        self.rows[release.tag] = release_row(release)
        self.upserted_at[release.tag] = now
        return previous

    def promoted_tag(self, tx: Transaction) -> str | None:
        _require_open(tx)
        return self.promoted

    def set_promoted(self, tx: Transaction, tag: str) -> None:
        _require_open(tx)
        if tag not in self.rows:  # the app_release_policy FK
            raise AssertionError(f"promoted tag {tag} is not a release")
        self.promoted = tag

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
