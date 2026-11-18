"""Frame media: which Player may read which media variant, and the one read path it takes.

A media variant is named by its inputs (`<asset_id>.<recipe_id>`, kernel/types.py) and stored by
its bytes (`media/blobs/<sha256>`, `CacheLayout`). `FrameMedia.resolve` decides, in one
transaction and in the order the coordination lock imposes, whether a Player's bearer may read a
variant now, and stamps it served; the route then reads it through the AssetReader, which
serves the file on a hit and publishes `PrepareMedia` on a miss (a purged or corrupt file is
re-made). Nothing here reaches the library: Players never do (requirements.md).

The order (it serializes with coordination, so a grant reads one cut of its offers):
1. the grant's timeouts and the coordination lock;
2. the bearer's session; none: 401;
3. the session's configuration at its epoch; none: 401;
4. the media lock, then the variant's Asset record; none: 404 `media_unavailable`;
5. the execution's authorization (a current offer or lock of this session that names the variant:
   that is the variant's pin, decision 0034); refused: 403 `media_forbidden`;
6. the variant stamped served (`assets.last_served_at`, the database's clock): the media cache
   spares a file served within `TRANSFER_SECONDS` (central/assets/maintenance.py), so it is never
   evicted mid-transfer. Nothing is held or released after.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from central.kernel.assets import AssetKey, AssetKind, AssetReference, OriginLocator
from central.kernel.job_types import PrepareMedia
from central.kernel.ports import AssetRecords, Candidates
from central.kernel.transactions import Transaction, Transactions
from central.kernel.types import require_media_variant_key
from contracts.models import PlayerConfiguration
from contracts.time import Clock

# How long a transfer runs at most: the media cache spares a variant served this recently.
TRANSFER_SECONDS: Final = 150
# The reference a planned variant carries: "wanted", never where from. Its photo's library and key
# are the media worker's alone (R22), which finds the revision from the variant's name; `.invalid`
# (RFC 2606) never resolves.
VARIANT_REFERENCE: Final = AssetReference(
    owner="frame-media",
    locator=OriginLocator("http://library.invalid/", sha256=None, size=None),
    expected_size=None, expected_sha256=None)
# The owner of a variant migrated from the media store (087): its recipe names no need, so it can
# be served while its file lasts but never re-made.
LEGACY_OWNER: Final = "legacy-087"


def variant_asset_key(key: str) -> AssetKey:
    return AssetKey(AssetKind.MEDIA_VARIANT, require_media_variant_key(key))


@dataclass(frozen=True, slots=True)
class Grantee:
    """An authenticated Player session and its configuration at its epoch."""

    player_id: str
    authority_epoch: int
    configuration: PlayerConfiguration


class FrameMediaRefused(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class FrameGrant:
    candidates: Candidates  # the variant's one PrepareMedia job, pinned


class FrameMediaGrants(Protocol):
    """The grant's reads and writes, one named method per question, in the caller's
    transaction (PostgreSQL: `central.infra.frame_media_grants`)."""

    def serialize(self, tx: Transaction) -> None:
        """The grant's lock and statement timeouts, then the coordination lock."""
        ...

    def grantee(self, tx: Transaction, token: str) -> Grantee | None:
        """The bearer's current session with its configuration, or None."""
        ...

    def lock_media(self, tx: Transaction) -> None: ...

    def authorized(self, tx: Transaction, grantee: Grantee, key: str, now: float) -> bool:
        """Whether a current offer or lock of the grantee's session names the variant `key`."""
        ...


class FrameMedia:
    """Blocking: call `resolve` from a worker thread."""

    def __init__(self, *, transactions: Transactions, records: AssetRecords,
                 grants: FrameMediaGrants, clock: Clock) -> None:
        self._transactions = transactions
        self._records = records
        self._grants = grants
        self._clock = clock

    def resolve(self, token: str, key: str) -> FrameGrant:
        try:
            asset = variant_asset_key(key)
        except ValueError:
            raise FrameMediaRefused("media_unavailable", 404) from None
        with self._transactions.begin() as tx:
            self._grants.serialize(tx)
            grantee = self._grants.grantee(tx, token)
            if grantee is None:
                raise FrameMediaRefused("unauthorized", 401)
            self._grants.lock_media(tx)
            if self._records.get(tx, asset) is None:
                raise FrameMediaRefused("media_unavailable", 404)
            if not self._grants.authorized(tx, grantee, key, self._clock.utc()):
                raise FrameMediaRefused("media_forbidden", 403)
            self._records.touch_served(tx, asset)
        return FrameGrant(Candidates(jobs=(PrepareMedia(key=key),), pinned=True))
