"""The mounted F1/F3 seam serves only frozen, measured offer bytes."""

import base64
import hashlib
import os
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

import central.fleet.routes as routes
from central.assets.reader import Opened
from central.fleet.models import FleetError
from central.fleet.service import OfferAsset
from contracts.time import ManualClock

OFFER_ID = UUID(int=1)
BOOT_ID = UUID(int=2)
BLOB = b"exact fleet app package"
DIGEST = hashlib.sha256(BLOB).hexdigest()


class Reader:
    def __init__(self, path):
        self.path = path

    async def read(self, candidates):
        return Opened(candidates.jobs[0], os.open(self.path, os.O_RDONLY),
                      len(BLOB), "0" * 64)


class Service:
    def __init__(self, _db, _clock):
        self.calls = []

    def create_offer(self, body):
        self.calls.append(("offer", body.serial))
        return {"schema": 1, "offer_id": str(OFFER_ID),
                "base": {"sha256": DIGEST, "size": len(BLOB)},
                "initial_app": None}

    def offer_asset(self, offer_id, kind):
        assert offer_id == OFFER_ID
        if kind == "app":
            raise FleetError("app_unconfigured", 503)
        return OfferAsset("base", "v1", DIGEST, DIGEST, len(BLOB))

    def record_check_in(self, body):
        self.calls.append(("check_in", body.observation_sequence))
        return {"accepted": True}

    def status(self):
        return {"devices": [], "commands_available": False}


def _client(monkeypatch, path, *, content=True):
    monkeypatch.setattr(routes, "FleetService", Service)
    app = FastAPI()
    service = routes.mount_fleet_routes(
        app, db=None, clock=ManualClock(100), admin=lambda: None,
        content=SimpleNamespace(reader=Reader(path)) if content else None,
    )
    return TestClient(app), service


def test_offer_preflight_then_exact_stream_with_digest_header(tmp_path, monkeypatch) -> None:
    path = tmp_path / "base.squashfs"
    path.write_bytes(BLOB)
    client, service = _client(monkeypatch, path)
    body = {"schema": 1, "kind": "pi", "serial": "abcdef1234567890",
            "kernel_boot_id": str(BOOT_ID), "boot_nonce": "a" * 32}
    response = client.post("/v1/netboot/offers", json=body)
    assert response.status_code == 200
    assert response.json()["offer_id"] == str(OFFER_ID)
    assert service.calls == [("offer", body["serial"])]
    result = client.get(f"/v1/netboot/offers/{OFFER_ID}/base")
    assert result.status_code == 200
    assert result.content == BLOB
    assert result.headers["content-length"] == str(len(BLOB))
    assert result.headers["digest"] == "sha-256=" + base64.b64encode(
        bytes.fromhex(DIGEST)).decode()
    assert client.get(f"/v1/netboot/offers/{OFFER_ID}/app").json() == {
        "error": "app_unconfigured"}


def test_missing_pod_bytes_do_not_reselect_or_claim_delivery(tmp_path, monkeypatch) -> None:
    path = tmp_path / "absent.squashfs"
    client, service = _client(monkeypatch, path, content=False)
    body = {"schema": 1, "kind": "pi", "serial": "abcdef1234567890",
            "kernel_boot_id": str(BOOT_ID), "boot_nonce": "a" * 32}
    response = client.post("/v1/netboot/offers", json=body)
    assert response.status_code == 503
    assert response.json() == {"error": "content_unavailable"}
    assert service.calls == [("offer", body["serial"])]
