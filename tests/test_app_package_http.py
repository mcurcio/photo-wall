"""HTTP composition of the 0009 app package service (central-only, additive).

Independent of the release authority: no BootRequest/BootTicket involved,
mirrors the rootfs artifact route's O_NOFOLLOW/fstat/size-match/streaming
discipline instead (see tests/test_release_http.py).

0013 (decision 5): the operator hand-staging routes ``POST /v1/operator/app``
and ``PUT /v1/operator/app/current`` are retired -- GitHub Releases are the
sole ``.deb`` source. Those paths now return 410 (asserted below). The domain
methods ``AppPackages.register``/``.promote`` are KEPT -- the GitHub mirror and
reconcile still call them -- so the serving tests here stage promoted state by
invoking those methods directly, exactly as the mirror does.
"""

import hashlib
import os

from fastapi.testclient import TestClient

from central.app import create_app
from central.app_packages import AppPackages

ADMIN = "app-package-http-operator-" + "x" * 32


def _stage_and_promote(registry, *, version, payload):
    """Record + promote a `.deb` via the kept domain methods (as the mirror does)."""
    sha256 = hashlib.sha256(payload).hexdigest()
    packages = AppPackages(registry.db, registry.clock)
    packages.register(version, sha256, len(payload))
    packages.promote(sha256)
    return sha256


class _FakeDatabase:
    """Non-Database stand-in: create_app builds no queues/gateway against it, so
    the app composes with zero DB access -- enough for the 410 tombstone routes,
    which never touch persistence."""

    def migrate(self):
        pass


def test_register_promote_and_stream_exact_bytes(registry, tmp_path):
    payload = b"debian binary package bytes for the Player app"
    sha256 = hashlib.sha256(payload).hexdigest()
    (tmp_path / f"app-{sha256}.deb").write_bytes(payload)

    app = create_app(registry.db, registry.clock, ADMIN, app_root=tmp_path)
    with TestClient(app) as client:
        _stage_and_promote(registry, version="1.2.3", payload=payload)

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
    with TestClient(app) as client:
        AppPackages(registry.db, registry.clock).register("1.0.0", sha256, len(payload))
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
    with TestClient(app) as client:
        AppPackages(registry.db, registry.clock).register("1.0.0", sha256, len(payload))
        response = client.get(f"/v1/app/package/{sha256}.deb")
        assert response.status_code == 503
        assert response.json() == {"error": "app_artifact_unavailable"}


def test_retired_hand_staging_routes_are_410_and_player_routes_are_open(registry, tmp_path):
    app = create_app(registry.db, registry.clock, ADMIN, app_root=tmp_path)
    with TestClient(app) as client:
        # 0013 decision 5: hand-staging is retired. Both former operator routes
        # answer 410 for ANY caller (no auth gate), even a well-formed body.
        registered = client.post(
            "/v1/operator/app", json={"version": "1.0.0", "sha256": "a" * 64, "size": 1}
        )
        assert registered.status_code == 410
        promoted = client.put("/v1/operator/app/current", json={"sha256": "a" * 64})
        assert promoted.status_code == 410
        # Player-facing routes are unauthenticated on the trusted LAN (0009).
        assert client.get("/v1/app/manifest").status_code == 503
        assert client.get(f"/v1/app/package/{'a' * 64}.deb").status_code == 404


def test_retired_hand_staging_routes_are_410_without_a_database():
    """Hermetic (no DB): the 410 tombstones never touch persistence, so the
    probe-7 assertion holds with the app composed against a fake database."""
    app = create_app(_FakeDatabase(), None, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        assert client.post(
            "/v1/operator/app", json={"version": "1.0.0", "sha256": "a" * 64, "size": 1}
        ).status_code == 410
        assert client.put(
            "/v1/operator/app/current", json={"sha256": "a" * 64}
        ).status_code == 410


def test_promoting_a_different_version_flips_the_manifest(registry, tmp_path):
    app = create_app(registry.db, registry.clock, ADMIN, app_root=tmp_path)
    with TestClient(app) as client:
        first = b"player app version one"
        (tmp_path / f"app-{hashlib.sha256(first).hexdigest()}.deb").write_bytes(first)
        sha_one = _stage_and_promote(registry, version="1.0.0", payload=first)
        assert client.get("/v1/app/manifest").json()["sha256"] == sha_one

        second = b"player app version two, longer than the first"
        (tmp_path / f"app-{hashlib.sha256(second).hexdigest()}.deb").write_bytes(second)
        sha_two = _stage_and_promote(registry, version="2.0.0", payload=second)

        manifest = client.get("/v1/app/manifest").json()
        assert manifest == {"version": "2.0.0", "sha256": sha_two, "size": len(second)}
        assert sha_two != sha_one
