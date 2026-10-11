"""Preparation fetches the photo Immich shows, against real Immich (roadmap 4, slice M3).

The fixture (slice FX1) holds `edited`, cropped through Immich's own edits, and `video-1`, a 2 s
MP4. The real `ImmichClient` fetches; the real `Preparer` (ffmpeg) prepares (C10).
"""

import asyncio
import io
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image
from pydantic import SecretStr

from central.photo_library.model import ConnectionSecret, LibraryAsset
from contracts.time import ManualClock
from media.immich import ImmichClient
from media.prepare import Preparer

if not os.environ.get("PHOTO_WALL_IMMICH_FIXTURE"):
    pytest.skip("set PHOTO_WALL_IMMICH_FIXTURE", allow_module_level=True)

pytestmark = pytest.mark.immich


def _tool(name):
    path = shutil.which(name) or (f"/opt/homebrew/bin/{name}"
                                  if Path(f"/opt/homebrew/bin/{name}").is_file() else None)
    if path is None:
        pytest.skip("ffmpeg is required for real preparation integration tests")
    return path


@pytest.fixture
def client(immich_library):
    made = ImmichClient.for_connection(ConnectionSecret(
        connection_ref="main", base_url=immich_library.base_url,
        api_key=SecretStr(immich_library.api_key)))
    yield made
    asyncio.run(made.close())


def pool_asset(client, label, immich_library):
    async def read():
        return await client.revision_now(immich_library.assets[label].upstream_id)
    item, revision = asyncio.run(read())
    return LibraryAsset(connection_ref="main", upstream_id=item.upstream_id, revision=revision,
                        kind=item.kind, taken_at=item.taken_at, width=item.width,
                        height=item.height, duration=item.duration)


def test_the_edited_photo_is_fetched_thumbnailed_and_revised_edited(
        client, immich_library, tmp_path):
    edited = immich_library.assets["edited"]
    asset = pool_asset(client, "edited", immich_library)
    assert "-" in asset.revision            # checksum, then the edit fingerprint
    assert (asset.width, asset.height) == (edited.width, edited.height)
    fetched = asyncio.run(client.fetch_rendition(asset, frame_long_edge=None,
                                                 destination=tmp_path / "full"))
    assert fetched.width / fetched.height == pytest.approx(edited.width / edited.height,
                                                           rel=0.02)
    thumbnail = asyncio.run(client.thumbnail(asset.upstream_id, asset.revision))
    with Image.open(io.BytesIO(thumbnail)) as image:
        assert image.width / image.height == pytest.approx(edited.width / edited.height,
                                                           rel=0.05)


def test_the_preview_serves_a_frame_it_covers_and_the_full_size_copy_a_larger_one(
        client, immich_library, tmp_path):
    asset = pool_asset(client, "garden-1", immich_library)
    preview = asyncio.run(client.fetch_rendition(asset, frame_long_edge=1,
                                                 destination=tmp_path / "preview"))
    assert preview.rendition == "preview"
    larger = max(preview.width, preview.height) + 1
    full = asyncio.run(client.fetch_rendition(asset, frame_long_edge=larger,
                                              destination=tmp_path / "full"))
    assert full.rendition == "fullsize"
    assert max(full.width, full.height) == max(asset.width, asset.height)


def test_the_video_is_fetched_as_its_original_and_prepares_to_mp4(
        client, immich_library, tmp_path):
    asset = pool_asset(client, "video-1", immich_library)
    fetched = asyncio.run(client.fetch_rendition(asset, frame_long_edge=1920,
                                                 destination=tmp_path / "original"))
    assert fetched.rendition == "original"
    preparer = Preparer(ffmpeg=_tool("ffmpeg"), ffprobe=_tool("ffprobe"),
                        clock=ManualClock(asset.taken_at))
    prepared = asyncio.run(preparer.prepare_rendition(asset, fetched, tmp_path / "variant"))
    assert prepared.variant.media_type == "video/mp4"
    assert prepared.original_sha256 == fetched.sha256
