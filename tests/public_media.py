"""Generated public JPEGs, made into media variants as the media maker records them.

The bytes are generated, not encoded by a preparer: this qualifies the variant record and the
read path, never a conversion worker, an upstream adapter, or native presentation.
"""

import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from support.photo_pools import FIXTURE_CONNECTION

from central.assets.frame_media import variant_asset_key
from central.assets.layout import CacheLayout
from central.infra.transactions import PgTransaction
from central.kernel.assets import AssetKind, MediaReady
from central.kernel.types import media_variant_key
from central.media_repository import MediaRepository
from central.photo_library.model import LibraryAsset
from contracts.models import Variant


@dataclass(frozen=True)
class PublicPhoto:
    asset: LibraryAsset
    original: bytes
    derivative: bytes

    @property
    def original_sha256(self) -> str:
        return hashlib.sha256(self.original).hexdigest()

    @property
    def asset_id(self) -> str:
        return self.asset.asset_id


@dataclass(frozen=True)
class MediaCache:
    """A repository and the cache root its variants live under (`media/blobs/<sha256>`)."""

    repository: MediaRepository
    root: Path

    @property
    def layout(self) -> CacheLayout:
        return CacheLayout(self.root)


# The fixture connection's photos (follow-ups A2: an Immich asset is keyed by its connection, so a
# pool's photos must be its connection's for its counts to find them).
def public_photo(*, connection_id=FIXTURE_CONNECTION, number=1,
                 width=108, height=192, captured_at=1000) -> PublicPhoto:
    buffers = (io.BytesIO(), io.BytesIO())
    for buffer, quality in zip(buffers, (95, 90)):
        Image.new("RGB", (width, height), (35, 93, 161)).save(
            buffer, format="JPEG", quality=quality,
        )
    original, derivative = (buffer.getvalue() for buffer in buffers)
    asset = LibraryAsset(connection_ref=connection_id, upstream_id=str(uuid.UUID(int=number)),
        revision=hashlib.sha1(original).hexdigest(), kind="image", width=width, height=height,
        taken_at=captured_at)
    return PublicPhoto(asset, original, derivative)


def request_photo(cache: MediaCache, asset_id: str) -> str:
    """Plan the photo's preparation as coordination does (`request_preparations_in`): its
    variant's record and `PrepareMedia`. Returns the variant's name."""
    from central.planner import AcquisitionRequest

    repository = cache.repository
    with repository.db.transaction() as conn:
        assert repository.request_preparations_in(conn, (AcquisitionRequest(
            asset_id=asset_id, assignment_ids=("assignment",), earliest_start=0),)) == 1
        return media_variant_key(asset_id, repository.wall_recipe_in(conn))


def make_variant(cache: MediaCache, key: str, derivative: bytes, *, width: int, height: int,
                 made_from: str = "preview") -> Variant:
    """What the media maker records for a variant already requested: its file at its digest and
    its produced facts."""
    repository = cache.repository
    digest = hashlib.sha256(derivative).hexdigest()
    facts = MediaReady(size=len(derivative), sha256=digest, media_type="image/jpeg",
                       width=width, height=height, duration=None, made_from=made_from)
    path = cache.layout.path(variant_asset_key(key), facts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(derivative)
    with repository.db.transaction() as conn:
        tx = PgTransaction(conn)
        assert repository.records.get(tx, variant_asset_key(key)) is not None
        repository.records.record_produced(tx, variant_asset_key(key), facts)
        return repository.variants_in(conn, (key,))[key]


def publish_photo(cache: MediaCache, photo: PublicPhoto) -> Variant:
    """Make a requested photo's variant; never seed catalog or authored state."""
    repository = cache.repository
    with repository.db.transaction() as conn:
        recipe = repository.wall_recipe_in(conn)
        key = media_variant_key(photo.asset_id, recipe)
        assert conn.execute("SELECT 1 FROM assets WHERE kind=%s AND identity=%s",
                            (AssetKind.MEDIA_VARIANT.value, key)).fetchone(), "not requested"
    return make_variant(cache, key, photo.derivative, width=photo.asset.width,
                        height=photo.asset.height)
