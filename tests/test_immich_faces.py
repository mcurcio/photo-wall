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
from central.media_store import MediaStore
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
    worker = MediaWorker(repository, MediaStore(repository, tmp_path / "media"), library,
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
