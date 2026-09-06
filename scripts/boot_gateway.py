"""HTTPS central/static-release adapter for the isolated generic-VM boot fixture.

Run with Uvicorn TLS, PHOTO_WALL_APPLIANCE_BUNDLE, and PHOTO_WALL_BOOT_PUBLIC_CONFIG.
Normal central database/admin settings remain private fixture configuration.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse

from appliance.bootstrap import read_regular
from central.app import create_app as create_central_app
from contracts.release import MAX_MANIFEST_BYTES, Release, configuration_digest


@dataclass(frozen=True)
class BootBundle:
    directory: Path
    release: Release

    @classmethod
    def load(cls, directory: Path, public_config: Path) -> BootBundle:
        if directory.is_symlink() or public_config.is_symlink():
            raise ValueError("boot fixture directory symlink")
        directory, public_config = directory.resolve(strict=True), public_config.resolve(strict=True)
        files = {name: read_regular(public_config / name, 1024**2) for name in (
            "public.json", "bootstrap.json", "ca.pem", "release.pub.pem")}
        payload = read_regular(directory / "release.json", MAX_MANIFEST_BYTES)
        signature = read_regular(directory / "release.sig", 64)
        key = load_pem_public_key(files["release.pub.pem"])
        if not isinstance(key, Ed25519PublicKey) or len(signature) != 64:
            raise ValueError("invalid boot fixture signing key or signature")
        key.verify(signature, payload)
        release = Release.decode(payload)
        if release.configuration_sha256 != configuration_digest(files):
            raise ValueError("boot fixture configuration mismatch")
        descriptor = os.open(directory / release.rootfs_name,
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != release.rootfs_size:
                raise ValueError("boot fixture rootfs size")
            digest, count = hashlib.sha256(), 0
            while block := os.read(descriptor, 65536):
                count += len(block)
                if count > release.rootfs_size:
                    raise ValueError("boot fixture rootfs size")
                digest.update(block)
            if count != release.rootfs_size or digest.hexdigest() != release.rootfs_sha256:
                raise ValueError("boot fixture rootfs integrity")
        finally:
            os.close(descriptor)
        return cls(directory, release)

    def response(self, filename: str) -> FileResponse:
        types = {"release.json": "application/json", "release.sig": "application/octet-stream",
                 self.release.rootfs_name: "application/octet-stream"}
        if filename not in types:
            raise HTTPException(404)
        # The fixture mounts this verified public bundle read-only for its whole lifetime.
        return FileResponse(self.directory / filename, media_type=types[filename],
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def install_media_denial(app, control: Path):
    """CI-only persistent delivery fault; control and enrollment stay available."""
    @app.middleware("http")
    async def media_fault(request, call_next):
        try:
            os.lstat(control / "media-blocked")
            denied = True
        except FileNotFoundError:
            denied = False
        except OSError:
            denied = True
        if request.url.path.startswith("/v1/media/") and denied:
            return JSONResponse({"error": "fixture_media_blocked"}, status_code=503,
                                headers={"X-Photo-Wall-Fixture": "media-blocked", "Cache-Control": "no-store"})
        return await call_next(request)


def create_app():
    bundle = BootBundle.load(Path(os.environ["PHOTO_WALL_APPLIANCE_BUNDLE"]),
                             Path(os.environ["PHOTO_WALL_BOOT_PUBLIC_CONFIG"]))
    app = create_central_app()
    control = os.environ.get("PHOTO_WALL_FIXTURE_MEDIA_CONTROL")
    if control is not None:
        if control != "/fixture-control":
            raise ValueError("invalid fixture media control")
        install_media_denial(app, Path(control))

    @app.get("/appliance/{filename}")
    def artifact(filename: str):
        return bundle.response(filename)

    return app
