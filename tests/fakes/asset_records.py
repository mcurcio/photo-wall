"""An in-memory `AssetRecords` with the same semantics as the Postgres repository."""

from __future__ import annotations

from dataclasses import replace

from central.kernel.assets import Asset, AssetKey, AssetReady, AssetReference
from central.kernel.handling import ProducedFactsConflict
from central.kernel.transactions import Transaction


def _require_open(tx: Transaction) -> None:
    if tx.state != "open":
        raise AssertionError(f"AssetRecords used outside an open transaction ({tx.state})")


class InMemoryAssetRecords:
    """Implements `AssetRecords`; every call asserts `tx.state == "open"`.

    Writes apply immediately (there is no rollback); `assets` is the stored state.
    """

    def __init__(self) -> None:
        self.assets: dict[AssetKey, Asset] = {}

    def get(self, tx: Transaction, key: AssetKey) -> Asset | None:
        _require_open(tx)
        return self.assets.get(key)

    def reference(self, tx: Transaction, key: AssetKey, ref: AssetReference) -> bool:
        _require_open(tx)
        asset = self.assets.get(key)
        if asset is None:
            self.assets[key] = Asset(key, (ref,), produced=None, last_served_at=None)
            return True
        refs = list(asset.references)
        for index, existing in enumerate(refs):
            if existing.owner == ref.owner:
                if existing == ref:
                    return False
                refs[index] = ref
                break
        else:
            refs.insert(0, ref)  # newest first, by time added
        self.assets[key] = replace(asset, references=tuple(refs))
        return True

    def retire(self, tx: Transaction, key: AssetKey, owner: str) -> None:
        _require_open(tx)
        asset = self.assets.get(key)
        if asset is None:
            return
        refs = tuple(ref for ref in asset.references if ref.owner != owner)
        if not refs:
            del self.assets[key]
        elif len(refs) != len(asset.references):
            self.assets[key] = replace(asset, references=refs)

    def record_produced(self, tx: Transaction, key: AssetKey, facts: AssetReady) -> None:
        _require_open(tx)
        asset = self.assets.get(key)
        if asset is None or asset.produced == facts:
            return
        if asset.produced is not None:
            raise ProducedFactsConflict(f"{key}: recorded {asset.produced}, given {facts}")
        self.assets[key] = replace(asset, produced=facts)

    def touch_served(self, tx: Transaction, key: AssetKey, at: float) -> None:
        _require_open(tx)
        asset = self.assets.get(key)
        if asset is not None:
            self.assets[key] = replace(asset, last_served_at=at)
