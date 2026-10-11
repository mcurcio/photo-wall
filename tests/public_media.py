"""Generated public JPEGs published through the production media transaction.

Build metadata is deliberately synthetic: this qualifies publication, never a
conversion worker, an upstream adapter, or native presentation.
"""

import hashlib
import io
import uuid
from dataclasses import dataclass

from PIL import Image

from central.media_store import MediaStore
from central.photo_library.model import LibraryAsset
from contracts.models import Variant
from media.prepare import BuildIdentity, PreparedMedia


@dataclass(frozen=True)
class PublicPhoto:
    asset: LibraryAsset
    original: bytes
    derivative: bytes

    @property
    def original_sha256(self) -> str:
        return hashlib.sha256(self.original).hexdigest()


def public_photo(*, connection_id="fixture-library", number=1,
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


def publish_photo(storage: MediaStore, photo: PublicPhoto) -> Variant:
    """Consume a previously requested job; never seed catalog or authored state."""
    with storage.worker_lock():
        lease = storage.repository.claim_job()
        assert lease is not None and lease.asset == photo.asset
        paths = storage.staging(lease)
        paths.original.write_bytes(photo.original)
        paths.variant.write_bytes(photo.derivative)
        variant = Variant(sha256=hashlib.sha256(photo.derivative).hexdigest(),
            size=len(photo.derivative), media_type="image/jpeg",
            width=photo.asset.width, height=photo.asset.height)
        build = BuildIdentity(preparation_sha256="a" * 64, ffmpeg_sha256="b" * 64,
            ffprobe_sha256="c" * 64, ffmpeg_version="synthetic", ffprobe_version="synthetic",
            python_version="synthetic", pillow_version="synthetic", littlecms_version="synthetic",
            jpeg_version="synthetic", zlib_version="synthetic", platform="synthetic",
            memory_limit_enforced=False)
        prepared = PreparedMedia(path=paths.variant, variant=variant,
            original_sha256=photo.original_sha256, recipe_id=lease.recipe_id, build=build,
            made_from="preview")
        assert storage.publish(lease, prepared) == variant
        return variant
