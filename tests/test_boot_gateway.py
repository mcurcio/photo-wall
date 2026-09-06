"""The boot fixture serves only its verified signed public bundle."""

import hashlib

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from contracts.release import Release, configuration_digest
from scripts.boot_gateway import BootBundle


@pytest.fixture
def bundle(tmp_path):
    private = Ed25519PrivateKey.generate()
    public, root = tmp_path / "public", tmp_path / "bundle"
    public.mkdir()
    root.mkdir()
    files = {"public.json": b"public Player fixture", "bootstrap.json": b"public bootstrap fixture",
             "ca.pem": b"public fixture CA", "release.pub.pem": private.public_key().public_bytes(
                 serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)}
    for name, data in files.items():
        (public / name).write_bytes(data)
    payload = b"signed synthetic rootfs bytes"
    release = Release(revision="a"*40, boot_abi="b"*64,
                      configuration_sha256=configuration_digest(files),
                      rootfs_sha256=hashlib.sha256(payload).hexdigest(), rootfs_size=len(payload))
    (root / "release.json").write_bytes(release.encode())
    (root / "release.sig").write_bytes(private.sign(release.encode()))
    (root / release.rootfs_name).write_bytes(payload)
    return root, public, release, payload


def test_only_three_signed_public_files_are_served(bundle):
    root, public, release, payload = bundle
    loaded = BootBundle.load(root, public)
    app = FastAPI()

    @app.get("/appliance/{filename}")
    def artifact(filename: str):
        return loaded.response(filename)

    (root / "private.txt").write_bytes(b"must stay unserved")
    with TestClient(app) as client:
        for name in ("release.json", "release.sig", release.rootfs_name):
            response = client.get("/appliance/" + name)
            assert response.status_code == 200
            assert response.content == (root / name).read_bytes()
            assert response.headers["cache-control"] == "no-store"
            assert int(response.headers["content-length"]) == len(response.content)
        for name in ("private.txt", "public.json", "rootfs-" + "0"*64 + ".squashfs", "%2e%2e"):
            assert client.get("/appliance/" + name).status_code == 404


@pytest.mark.parametrize("failure", ["signature", "configuration", "corrupt", "truncated",
                                      "manifest_symlink", "rootfs_symlink", "directory_symlink"])
def test_unverified_or_symlinked_bundle_is_never_served(bundle, failure):
    root, public, release, payload = bundle
    if failure == "signature":
        (root / "release.sig").write_bytes(b"x"*64)
    elif failure == "configuration":
        (public / "public.json").write_bytes(b"changed public configuration")
    elif failure in ("corrupt", "truncated"):
        (root / release.rootfs_name).write_bytes(b"x"*len(payload) if failure == "corrupt" else payload[:-1])
    elif failure == "directory_symlink":
        link = root.with_name("link")
        link.symlink_to(root, target_is_directory=True)
        root = link
    else:
        path = root / ("release.json" if failure == "manifest_symlink" else release.rootfs_name)
        saved = path.with_name("saved")
        path.rename(saved)
        path.symlink_to(saved)
    with pytest.raises((OSError, ValueError, InvalidSignature)):
        BootBundle.load(root, public)


def test_persistent_media_fault_denies_bodies_but_preserves_control(tmp_path):
    from scripts.boot_gateway import install_media_denial

    control = tmp_path / "control"
    control.mkdir()
    delivered = []
    def app():
        result = FastAPI()
        @result.get("/v1/media/{digest}")
        def media(digest):
            delivered.append(digest)
            return {"bytes": "photo"}
        @result.get("/v1/player/state")
        def state():
            return {"control": "available"}
        install_media_denial(result, control)
        return result
    with TestClient(app()) as client:
        assert client.get("/v1/media/" + "a" * 64).status_code == 200
    (control / "media-blocked").write_bytes(b"photo-wall-ci-media-blocked-v1\n")
    # Recreating the app models the central restart: denial is persistent.
    for _ in range(2):
        with TestClient(app()) as client:
            for method in ("GET", "HEAD"):
                response = client.request(method, "/v1/media/" + "a" * 64)
                assert response.status_code == 503
                assert response.headers["x-photo-wall-fixture"] == "media-blocked"
                if method == "GET":
                    assert response.json() == {"error": "fixture_media_blocked"}
            assert client.get("/v1/player/state").json() == {"control": "available"}
    assert len(delivered) == 1


def test_invalid_or_unreadable_fault_marker_keeps_delivery_denied(tmp_path, monkeypatch):
    from scripts import boot_gateway

    app = FastAPI()
    boot_gateway.install_media_denial(app, tmp_path)
    marker = tmp_path / "media-blocked"
    marker.symlink_to(tmp_path / "missing")
    with TestClient(app) as client:
        assert client.get("/v1/media/" + "a" * 64).status_code == 503
        original = boot_gateway.os.lstat
        def denied(path, *args, **kwargs):
            if path == marker:
                raise PermissionError("private-path")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(boot_gateway.os, "lstat", denied)
        response = client.get("/v1/media/" + "a" * 64)
        assert response.status_code == 503 and "private-path" not in response.text
