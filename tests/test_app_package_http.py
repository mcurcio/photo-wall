"""HTTP composition of the 0009 app package service (central-only, additive).

Independent of the release authority: no BootRequest/BootTicket involved,
mirrors the rootfs artifact route's O_NOFOLLOW/fstat/size-match/streaming
discipline instead (see tests/test_release_http.py).
"""

import hashlib
import os

from fastapi.testclient import TestClient

from central.app import create_app

ADMIN = "app-package-http-operator-" + "x" * 32


def _register_and_promote(client, *, version, payload, headers):
    sha256 = hashlib.sha256(payload).hexdigest()
    registered = client.post(
        "/v1/operator/app",
        headers=headers,
        json={"version": version, "sha256": sha256, "size": len(payload)},
    )
    assert registered.status_code == 201
    promoted = client.put("/v1/operator/app/current", headers=headers, json={"sha256": sha256})
    assert promoted.status_code == 200
    return sha256


def test_register_promote_and_stream_exact_bytes(registry, tmp_path):
    payload = b"debian binary package bytes for the Player app"
    sha256 = hashlib.sha256(payload).hexdigest()
    (tmp_path / f"app-{sha256}.deb").write_bytes(payload)

    app = create_app(registry.db, registry.clock, ADMIN, app_root=tmp_path)
    headers = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app) as client:
        _register_and_promote(client, version="1.2.3", payload=payload, headers=headers)

        manifest = client.get("/v1/app/manifest")
        assert manifest.status_code == 200
        assert manifest.json() == {"version": "1.2.3", "sha256": sha256, "size": len(payload)}

        fetched = client.get(f"/v1/app/package/{sha256}.deb")
        assert fetched.status_code == 200
        assert fetched.content == payload
        assert fetched.headers["content-length"] == str(len(payload))


def test_unknown_sha256_is_404(registry, tmp_path):
    app = create_app(registry.db, registry.clock, ADMIN, app_root=tmp_path)
    with TestClient(app) as client:
        response = client.get(f"/v1/app/package/{'a' * 64}.deb")
        assert response.status_code == 404
        assert response.json() == {"error": "app_package_not_found"}


def test_nothing_promoted_yields_503_app_unconfigured(registry):
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        response = client.get("/v1/app/manifest")
        assert response.status_code == 503
        assert response.json() == {"error": "app_unconfigured"}


def test_size_mismatched_artifact_is_refused_like_the_rootfs_route(registry, tmp_path):
    payload = b"the real, full-length package bytes"
    sha256 = hashlib.sha256(payload).hexdigest()
    app = create_app(registry.db, registry.clock, ADMIN, app_root=tmp_path)
    headers = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app) as client:
        registered = client.post(
            "/v1/operator/app",
            headers=headers,
            json={"version": "1.0.0", "sha256": sha256, "size": len(payload)},
        )
        assert registered.status_code == 201
        # Staged bytes are truncated relative to the registered size --
        # mutation probe: a build that skips the fstat size-match must let
        # this pass with 200, which the assertion below refuses.
        (tmp_path / f"app-{sha256}.deb").write_bytes(payload[:4])

        response = client.get(f"/v1/app/package/{sha256}.deb")
        assert response.status_code == 503
        assert response.json() == {"error": "app_artifact_invalid"}


def test_symlinked_artifact_is_refused(registry, tmp_path):
    payload = b"outside-root secret bytes that must never be served"
    sha256 = hashlib.sha256(payload).hexdigest()
    outside = tmp_path.parent / "outside-app-root.deb"
    outside.write_bytes(payload)
    app_root = tmp_path / "app-root"
    app_root.mkdir()
    os.symlink(outside, app_root / f"app-{sha256}.deb")

    app = create_app(registry.db, registry.clock, ADMIN, app_root=app_root)
    headers = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app) as client:
        registered = client.post(
            "/v1/operator/app",
            headers=headers,
            json={"version": "1.0.0", "sha256": sha256, "size": len(payload)},
        )
        assert registered.status_code == 201
        response = client.get(f"/v1/app/package/{sha256}.deb")
        assert response.status_code == 503
        assert response.json() == {"error": "app_artifact_unavailable"}


def test_operator_routes_require_admin_player_routes_do_not(registry, tmp_path):
    app = create_app(registry.db, registry.clock, ADMIN, app_root=tmp_path)
    with TestClient(app) as client:
        assert client.post(
            "/v1/operator/app", json={"version": "1.0.0", "sha256": "a" * 64, "size": 1}
        ).status_code == 401
        assert client.put(
            "/v1/operator/app/current", json={"sha256": "a" * 64}
        ).status_code == 401
        # Player-facing routes are unauthenticated on the trusted LAN (0009).
        assert client.get("/v1/app/manifest").status_code == 503
        assert client.get(f"/v1/app/package/{'a' * 64}.deb").status_code == 404


def test_promoting_a_different_version_flips_the_manifest(registry, tmp_path):
    app = create_app(registry.db, registry.clock, ADMIN, app_root=tmp_path)
    headers = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app) as client:
        first = b"player app version one"
        (tmp_path / f"app-{hashlib.sha256(first).hexdigest()}.deb").write_bytes(first)
        sha_one = _register_and_promote(client, version="1.0.0", payload=first, headers=headers)
        assert client.get("/v1/app/manifest").json()["sha256"] == sha_one

        second = b"player app version two, longer than the first"
        (tmp_path / f"app-{hashlib.sha256(second).hexdigest()}.deb").write_bytes(second)
        sha_two = _register_and_promote(client, version="2.0.0", payload=second, headers=headers)

        manifest = client.get("/v1/app/manifest").json()
        assert manifest == {"version": "2.0.0", "sha256": sha_two, "size": len(second)}
        assert sha_two != sha_one
