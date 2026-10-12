"""Faces and key content against real Immich v2.5.6 (roadmap 5a, run ledger slice M1; CI's
`photo-sources` leg). The published fixture's `anna-only` photo carries one manual face
(scripts/immich_actions.py `add_face`), posted on the photo's own 96 x 64 size at its top-left.
"""

from __future__ import annotations

import pytest
from media_queue import RecordingMediaQueue
from pydantic import SecretStr
from support.immich_library import run
from support.library_store_fake import FakeLibraryStore
from support.photo_pools import FIXTURE_CONNECTION, seed_pool

from central.db import ProcessTransactionClock
from central.infra.key_content_store import PgKeyContent
from central.media_repository import MediaRepository
from central.photo_library.model import ConnectionSecret, LibraryAsset
from media.immich import ImmichClient
from media.worker import MediaWorker

LABEL = "anna-only"


@pytest.fixture
def client(immich_library):
    made = ImmichClient.for_connection(ConnectionSecret(
        connection_ref=FIXTURE_CONNECTION, base_url=immich_library.base_url,
        api_key=SecretStr(immich_library.api_key)))
    yield made
    run(made.close())


def test_the_manual_face_comes_back_on_the_size_it_was_posted_with(client, immich_library):
    asset = immich_library.assets[LABEL]
    (face,) = run(client.faces(asset.upstream_id))
    assert (face.image_width, face.image_height) == (asset.width, asset.height)
    assert face.x1 < face.x2 <= asset.width and face.y1 < face.y2 <= asset.height


def test_measuring_the_real_thumbnail_records_one_face_at_the_top_left(
        client, immich_library, registry, tmp_path):
    fixture = immich_library.assets[LABEL]
    item, revision = run(client.revision_now(fixture.upstream_id))
    asset = LibraryAsset(connection_ref=FIXTURE_CONNECTION, upstream_id=item.upstream_id,
                         revision=revision, kind=item.kind, taken_at=item.taken_at,
                         width=item.width, height=item.height, duration=item.duration)
    seed_pool(registry, "faces:1", (asset,))
    times = ProcessTransactionClock(registry.clock)
    repository = MediaRepository(registry.db, registry.clock, queue=RecordingMediaQueue(),
                                 times=times)
    library = FakeLibraryStore(registry.clock)
    library.add_connection(FIXTURE_CONNECTION, immich_library.base_url, immich_library.api_key)
    store = PgKeyContent(registry.db, times=times)
    worker = MediaWorker(repository, library,
                         client_factory=ImmichClient.for_connection, key_content=store)
    try:
        run(worker.measure(asset.asset_id))
    finally:
        run(worker._close_clients())
    with registry.db.transaction() as conn:
        analysis = store.analyses_in(conn, [asset.asset_id], now=registry.clock.utc())[
            asset.asset_id]
    assert analysis.status == "measured"
    (face,) = analysis.key.faces
    assert (face.left + face.right) / 2 < 0.5 and (face.top + face.bottom) / 2 < 0.5
    assert sum(analysis.key.interest.weights) == pytest.approx(1.0, abs=1e-6)


def test_a_face_on_an_edited_photo_comes_back_on_the_edited_image(client, immich_library):
    """media/key_content.py divides each box by the size `/faces` reports, and the interest grid
    comes from the edited thumbnail: both must be the edited image. A 128 x 96 photo with a face
    on its right half, cropped in Immich to that half: the face comes back on 64 x 96, moved
    left by the crop (checked against Immich v2.5.6, 2026-10-11)."""
    import uuid

    asset = immich_library.upload("edited-face-" + uuid.uuid4().hex[:8],
                                  taken="2023-03-03T03:03:03Z", size=(128, 96))
    admin = immich_library._admin()
    person = admin.request("POST", "/people", (201,), json={"name": "Edited Face"})
    try:
        admin.request("POST", "/faces", (201,), json={
            "assetId": asset.upstream_id, "personId": person["id"], "imageWidth": 128,
            "imageHeight": 96, "x": 80, "y": 10, "width": 30, "height": 30})
        admin.request("PUT", f"/assets/{asset.upstream_id}/edits", json={"edits": [
            {"action": "crop", "parameters": {"x": 64, "y": 0, "width": 64, "height": 96}}]})
        (face,) = run(client.faces(asset.upstream_id))
    finally:
        admin.request("DELETE", "/assets", (204,), json={"ids": [asset.upstream_id],
                                                         "force": True})
        admin.request("DELETE", "/people/" + person["id"], (204,))
        admin.close()
    assert (face.image_width, face.image_height) == (64, 96)
    assert (face.x1, face.x2, face.y1, face.y2) == (16, 46, 10, 40)
