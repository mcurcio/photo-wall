"""HTTP streaming with real PostgreSQL authority and generated local bytes."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from test_media_store import VARIANT, grant, ready
from test_media_store import storage as _storage
from test_registry import ADMIN, enroll

from central.app import create_app
from central.media_gateway import MediaResponse
from central.media_repository import StoreLimits

storage = _storage


def transfers(storage):
    with storage.repository.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM media_references "
                            "WHERE owner LIKE 'transfer:%'").fetchone()["n"]


def test_http_exact_body_and_headers_requires_current_player_offer(storage, registry):
    with storage.worker_lock():
        _, _, prepared = ready(storage)
        player, _, _ = grant(storage, registry, prepared.variant)
        with TestClient(create_app(registry.db, registry.clock, ADMIN,
                                   media_root=storage.root)) as client:
            url = f"/v1/media/{prepared.variant.sha256}"
            headers = {"Authorization": f"Bearer {player['token']}"}
            assert client.get(url).status_code == 401
            assert client.get(url, headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 401
            assert client.get(url, headers={**headers, "Range": "bytes=0-4"}).status_code == 416
            response = client.get(url, headers=headers)
            assert response.status_code == 200
            assert response.content == VARIANT
            assert response.headers["Content-Length"] == str(len(VARIANT))
            assert response.headers["Content-Type"] == "image/jpeg"
            assert response.headers["ETag"] == f'"{prepared.variant.sha256}"'
            assert response.headers["Cache-Control"] == "private, no-store"
            assert "location" not in response.headers
            assert transfers(storage) == 0
            assert client.get("/v1/media/invalid", headers=headers).status_code == 400
            assert client.get("/v1/media/" + "0" * 64, headers=headers).status_code == 404
            stranger, _, _ = enroll(registry)
            assert client.get(url, headers={"Authorization": f"Bearer {stranger['token']}"}).status_code == 403
            registry.retire(player["player_id"])
            assert client.get(url, headers=headers).status_code == 401


@pytest.mark.parametrize("fault", ["backpressure", "disconnect", "send_error"])
def test_complete_response_lifetime_closes_descriptor_and_transfer_reference(storage, registry, fault):
    storage.repository.limits = StoreLimits(max_bytes=1000000, max_original_bytes=1000,
                                           max_image_bytes=200000, max_video_bytes=200000)
    with storage.worker_lock():
        storage.set_quota(1000000)
        _, _, prepared = ready(storage, derivative=b"x" * 150000)
        player, _, _ = grant(storage, registry, prepared.variant)
        opened = []
        original_open = storage.open_read

        def record_open(*args):
            lease = original_open(*args)
            opened.append(lease)
            return lease

        storage.open_read = record_open

        async def run():
            sent_body = asyncio.Event()

            async def receive():
                if fault == "disconnect":
                    await sent_body.wait()
                    return {"type": "http.disconnect"}
                await asyncio.Event().wait()

            async def send(message):
                if message["type"] == "http.response.body":
                    assert len(message["body"]) <= 65536
                    sent_body.set()
                    if fault == "send_error":
                        raise OSError("simulated transport failure")
                    await asyncio.Event().wait()

            response = MediaResponse(storage, player["token"], prepared.variant.sha256, seconds=.3)
            if fault == "backpressure":
                with pytest.raises(TimeoutError):
                    await response({}, receive, send)
            elif fault == "send_error":
                with pytest.raises(ExceptionGroup):
                    await response({}, receive, send)
            else:
                await response({}, receive, send)

        asyncio.run(run())
        assert len(opened) == 1
        assert opened[0]._fd is None
        assert transfers(storage) == 0
