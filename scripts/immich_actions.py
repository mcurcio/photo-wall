"""Container-side Immich fixture client and synthetic mutation operations.

This module deliberately has no Docker or host-process dependencies.  It is a
small, explicit dependency of helper images that need upstream authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from scripts.harness_failure import CodedFailure

PERMISSIONS = ["user.read", "asset.read", "asset.download"]
UPSTREAM = "http://immich:2283/api"
MAX_DOCUMENT_BYTES = 10 * 1024**2


class ActionError(CodedFailure):
    """A bounded container-side failure code safe for public evidence."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ActionError(code)


def read_json(path: Path) -> dict:
    with path.open("rb") as stream:
        data = stream.read(MAX_DOCUMENT_BYTES + 1)
    require(len(data) <= MAX_DOCUMENT_BYTES, "fixture_document_bound")
    value = json.loads(data)
    require(isinstance(value, dict), "fixture_document_invalid")
    return value


def write_json(path: Path, value: object) -> None:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    require(len(data) <= MAX_DOCUMENT_BYTES, "fixture_document_bound")
    with path.open("wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


class UpstreamFixture:
    """Admin-only synthetic mutations; no runtime key can modify upstream."""

    def __init__(self) -> None:
        import httpx

        self.client = httpx.Client(base_url=UPSTREAM, trust_env=False,
                                   follow_redirects=False, timeout=30)
        self.setup, self.runtime = Path("/setup"), Path("/runtime")

    def request(self, method: str, path: str, expected: tuple[int, ...] = (200,),
                **kwargs: object) -> dict:
        response = self.client.request(method, path, **kwargs)
        require(response.status_code in expected, "fixture_http_" + str(response.status_code))
        return response.json() if response.content else {}

    def authenticate(self) -> dict:
        state = read_json(self.setup / "session.json")
        self.client.headers["Authorization"] = "Bearer " + state["token"]
        return state

    def initialize(self) -> dict:
        password = secrets.token_urlsafe(32)
        deadline = time.monotonic() + 120
        while True:
            try:
                version = self.request("GET", "/server/version")
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise ActionError("upstream_startup_timeout") from None
                time.sleep(1)
        require(version == {"major": 2, "minor": 5, "patch": 6},
                "upstream_version_mismatch")
        credentials = {"email": "fixture@example.invalid", "password": password}
        owner = self.request("POST", "/auth/admin-sign-up", (201,),
                             json={**credentials, "name": "Disposable synthetic fixture"})
        login = self.request("POST", "/auth/login", (201,), json=credentials)
        self.client.headers["Authorization"] = "Bearer " + login["accessToken"]
        config = self.request("GET", "/system-config")
        config["machineLearning"]["enabled"] = False
        config["newVersionCheck"]["enabled"] = False
        config["reverseGeocoding"]["enabled"] = False
        self.request("PUT", "/system-config", json=config)
        key = self.request("POST", "/api-keys", (201,),
                           json={"name": "Photo Wall read-only fixture",
                                 "permissions": PERMISSIONS})
        require(sorted(key["apiKey"]["permissions"]) == sorted(PERMISSIONS),
                "key_scope_mismatch")
        write_json(self.setup / "session.json", {
            "token": login["accessToken"], "key_id": key["apiKey"]["id"],
        })
        write_json(self.runtime / "connection.json", {
            "connection_id": "fixture-library", "base_url": UPSTREAM,
            "owner_id": owner["id"], "api_key": key["secret"], "allow_http": True,
        })
        manifest = {}
        for orientation in range(1, 9):
            self.upload(manifest, f"orientation-{orientation}", (96, 64), orientation,
                        "2024-12-10T12:00:00Z", True)
        self.upload(manifest, "portrait", (72, 128), 1, "2024-12-11T12:00:00Z", True)
        self.upload(manifest, "square", (90, 90), 1, "2024-12-12T12:00:00Z", False)
        write_json(self.runtime / "fixtures.json", manifest)
        return {"version": version, "uploaded": len(manifest),
                "runtime_permissions": PERMISSIONS, "machine_learning_enabled": False}

    def upload(self, manifest: dict, label: str, size: tuple[int, int], orientation: int,
               captured: str, favorite: bool) -> None:
        from PIL import Image, ImageDraw

        path = self.setup / (label + ".jpg")
        image = Image.new("RGB", size, (24, 65, 90))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, size[0] // 2, size[1] // 2), fill=(235, 75, 40))
        draw.rectangle((size[0] // 2, size[1] // 2, size[0], size[1]), fill=(40, 220, 115))
        draw.text((3, 3), label, fill="white")
        exif = Image.Exif()
        exif[274] = orientation
        exif[306] = captured.replace("-", ":").replace("T", " ").removesuffix("Z")
        image.save(path, quality=95, exif=exif)
        data = path.read_bytes()
        with path.open("rb") as stream:
            result = self.request("POST", "/assets", (201,), data={
                "deviceAssetId": label, "deviceId": "photo-wall-synthetic-fixture",
                "fileCreatedAt": captured, "fileModifiedAt": captured,
                "isFavorite": str(favorite).lower(),
            }, files={"assetData": (label + ".jpg", stream, "image/jpeg")})
        manifest[label] = {
            "upstream_id": result["id"], "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "raw_width": size[0], "raw_height": size[1], "orientation": orientation,
            "captured": captured, "favorite": favorite, "deleted": False,
        }

    def mutate(self, action: str) -> dict:
        state = self.authenticate()
        fixtures = read_json(self.runtime / "fixtures.json")
        if action == "live":
            self.upload(fixtures, "older-upload", (72, 128), 1,
                        "2020-12-01T12:00:00Z", True)
            self.upload(fixtures, "new-upload", (96, 64), 1,
                        "2025-12-01T12:00:00Z", True)
            self.request("PUT", "/assets/" + fixtures["square"]["upstream_id"],
                         json={"isFavorite": True})
            fixtures["square"]["favorite"] = True
        elif action == "delete":
            self.request("DELETE", "/assets", (204,),
                         json={"ids": [fixtures["portrait"]["upstream_id"]], "force": True})
            fixtures["portrait"]["deleted"] = True
        elif action in ("deny", "restore"):
            permissions = ["user.read"] if action == "deny" else PERMISSIONS
            self.request("PUT", "/api-keys/" + state["key_id"],
                         json={"permissions": permissions})
        else:
            raise ActionError("unknown_fixture_action")
        write_json(self.runtime / "fixtures.json", fixtures)
        return {"action": action, "ok": True}


def run_action(action: str) -> dict:
    fixture = UpstreamFixture()
    try:
        return fixture.initialize() if action == "initialize" else fixture.mutate(action)
    finally:
        fixture.client.close()
