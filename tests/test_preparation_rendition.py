"""Preparation fetches the photo Immich shows, against real Immich (roadmap 4, slice M3; the
copy choice: follow-ups 2026-10-11, slice A4).

The fixture (slice FX1) holds `edited`, cropped through Immich's own edits, and `video-1`, a 2 s
MP4, and keeps no full-size copies. The real `ImmichClient` fetches the plan `fetch_plan` makes;
the real `Preparer` (ffmpeg) prepares (C10).
"""

import io
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image
from pydantic import SecretStr
from support.immich_library import run

from central.photo_library.model import ConnectionSecret, LibraryAsset
from contracts.time import ManualClock
from media.immich import ImmichClient
from media.library_client import FrameNeed, fetch_plan
from media.prepare import STILL_MEDIA_TYPES, Preparer

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
    run(made.close())


def pool_asset(client, label, immich_library):
    async def read():
        return await client.revision_now(immich_library.assets[label].upstream_id)
    item, revision = run(read())
    return LibraryAsset(connection_ref="main", upstream_id=item.upstream_id, revision=revision,
                        kind=item.kind, taken_at=item.taken_at, width=item.width,
                        height=item.height, duration=item.duration, original=item.original)


def need(width: int, height: int) -> tuple[FrameNeed, ...]:
    return (FrameNeed(aspect=width / height, width=width, height=height),)


def fetched_for(client, asset, needs, destination):
    """The real choice: the plan from the asset and the needs, then the real fetch."""
    plan = fetch_plan(asset, needs, {}, STILL_MEDIA_TYPES)
    return run(client.fetch_rendition(asset, plan=plan, needs=needs, destination=destination))


def test_the_edited_photo_is_fetched_thumbnailed_and_revised_edited(
        client, immich_library, tmp_path):
    edited = immich_library.assets["edited"]
    asset = pool_asset(client, "edited", immich_library)
    assert "-" in asset.revision            # checksum, then the edit fingerprint
    assert (asset.width, asset.height) == (edited.width, edited.height)
    fetched = fetched_for(client, asset, (), tmp_path / "full")
    assert fetched.rendition == "fullsize"   # Immich always keeps an edited photo's
    assert fetched.width / fetched.height == pytest.approx(edited.width / edited.height,
                                                           rel=0.02)
    thumbnail = run(client.thumbnail(asset.upstream_id, asset.revision))
    with Image.open(io.BytesIO(thumbnail)) as image:
        assert image.width / image.height == pytest.approx(edited.width / edited.height,
                                                           rel=0.05)


def test_the_preview_serves_a_frame_it_covers_and_the_original_a_larger_one(
        client, immich_library, tmp_path):
    """A4 case 5 (E-P4-I1-3's strict xfail, now a plain test). The fixture keeps no full-size
    copies, so an unedited JPEG larger than its preview is fetched as its original, at the
    original's size: directly when discovery read its type, and through `size=fullsize&
    edited=false` (Immich redirects a web original to it) when it did not. Mutation probe: send
    `edited=true` for the unedited full-size step and Immich redirects to the preview (the
    defect E-P4-I1-3 found), so the last assertion fails."""
    asset = pool_asset(client, "garden-1", immich_library)
    assert not asset.edited and asset.original is not None
    preview = fetched_for(client, asset, need(1, 1), tmp_path / "preview")
    assert preview.rendition == "preview"
    assert [copy.name for copy in preview.measured] == ["preview"]
    larger = need(preview.width + 1, preview.height + 1)
    full = fetched_for(client, asset, larger, tmp_path / "full")
    assert full.rendition == "original"
    assert (full.width, full.height) == (asset.width, asset.height)
    assert [copy.name for copy in full.measured] == ["preview"]
    unread = asset.model_copy(update={"original": None})
    assert fetch_plan(unread, larger, {}, STILL_MEDIA_TYPES) == ("preview", "fullsize")
    redirected = fetched_for(client, unread, larger, tmp_path / "redirected")
    assert (redirected.rendition, redirected.width, redirected.height) == (
        "original", asset.width, asset.height)


def test_the_video_is_fetched_as_its_original_and_prepares_to_mp4(
        client, immich_library, tmp_path):
    asset = pool_asset(client, "video-1", immich_library)
    fetched = fetched_for(client, asset, need(1920, 1080), tmp_path / "original")
    assert fetched.rendition == "original"
    preparer = Preparer(ffmpeg=_tool("ffmpeg"), ffprobe=_tool("ffprobe"),
                        clock=ManualClock(asset.taken_at))
    prepared = run(preparer.prepare_rendition(asset, fetched, tmp_path / "variant"))
    assert prepared.variant.media_type == "video/mp4"
    assert prepared.original_sha256 == fetched.sha256
