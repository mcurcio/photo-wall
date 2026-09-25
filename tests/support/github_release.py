"""The GitHub Releases wire as tests serve it, and a fake origin HTTP server.

One place states what `GitHubReleaseOrigin` reads: the release-list entry (`tag_name`, `draft`,
`prerelease`, `assets[].browser_download_url`, and the manifest asset's `id` and `updated_at`:
the release's upstream version), the release's `manifest.json` (`base_image`, `player_deb`) and
the base tarball `scripts/package_release_artifacts.py` builds. The
`httpx.MockTransport` e2e and the real-process two-pod test both serve these bytes.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TARBALL_NAME = "photo-wall-base.tar.gz"
MANIFEST_NAME = "manifest.json"
# The manifest asset's (updated_at, id) of a first upload: GitHub's asset `updated_at` format.
FIRST_UPLOAD = ("2026-09-01T00:00:00Z", 7)


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def real_tarball(squashfs: bytes) -> tuple[bytes, str, str]:
    """A REAL base tarball: photo-wall-base/{squashfs, SHA256SUMS}. Returns
    (bytes, tarball_sha256, squashfs_sha256) -- the exact shape
    scripts/package_release_artifacts.py produces (arcname prefix + inner SHA256SUMS)."""
    sq_sha = hashlib.sha256(squashfs).hexdigest()
    sums = f"{sq_sha}  ./photo-wall-base.squashfs\n".encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add(tar, "photo-wall-base/photo-wall-base.squashfs", squashfs)
        _add(tar, "photo-wall-base/SHA256SUMS", sums)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest(), sq_sha


def deb_name(tag: str) -> str:
    return f"photo-wall-player_{tag.lstrip('v')}_all.deb"


def manifest_bytes(*, tarball: bytes, tarball_sha: str, deb: bytes, deb_filename: str) -> bytes:
    """A release's `manifest.json` (schema 1) naming its base tarball and Player `.deb`."""
    return json.dumps({
        "schema": 1,
        "revision": "0" * 40,
        "base_image": {"filename": TARBALL_NAME, "sha256": tarball_sha, "size": len(tarball)},
        "player_deb": {"filename": deb_filename, "sha256": hashlib.sha256(deb).hexdigest(),
                       "size": len(deb)},
    }).encode()


def release_entry(tag: str, *, manifest_url: str, tarball_url: str, deb_filename: str,
                  deb_url: str, uploaded: tuple[str, int] = FIRST_UPLOAD) -> dict:
    """One published (non-draft, non-prerelease) entry of the releases list. Every asset was
    uploaded at `uploaded` = (updated_at, the manifest asset's id); the others take the next ids.
    """
    updated_at, manifest_id = uploaded
    names_and_urls = ((MANIFEST_NAME, manifest_url), (TARBALL_NAME, tarball_url),
                      (deb_filename, deb_url))
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [{"id": manifest_id + offset, "name": name, "updated_at": updated_at,
                    "browser_download_url": url}
                   for offset, (name, url) in enumerate(names_and_urls)],
    }


# -- a fake origin over real HTTP -----------------------------------------------------------------


@dataclass
class FakeRelease:
    tag: str
    tarball: bytes
    tarball_sha: str
    squashfs_sha: str
    deb: bytes
    tarball_status: int = 200  # 404 makes the OS image terminally unobtainable
    uploaded: tuple[str, int] = FIRST_UPLOAD  # the manifest asset's (updated_at, id)

    @property
    def deb_filename(self) -> str:
        return deb_name(self.tag)


def make_release(tag: str, *, squashfs_bytes: int = 4 * 1024 * 1024) -> FakeRelease:
    """Random (incompressible) squashfs bytes, so the tarball is big enough to throttle or hold."""
    tarball, tarball_sha, squashfs_sha = real_tarball(os.urandom(squashfs_bytes))
    return FakeRelease(tag, tarball, tarball_sha, squashfs_sha, os.urandom(64 * 1024))


def recut(release: FakeRelease, *, squashfs_bytes: int = 4 * 1024 * 1024) -> FakeRelease:
    """`release` re-cut upstream (`--clobber`): new tarball bytes under the same tag, uploaded one
    hour later as new assets (a newer manifest `updated_at` and id), so the sync takes it."""
    tarball, tarball_sha, squashfs_sha = real_tarball(os.urandom(squashfs_bytes))
    updated_at, manifest_id = release.uploaded
    later = datetime.fromisoformat(updated_at) + timedelta(hours=1)
    return replace(release, tarball=tarball, tarball_sha=tarball_sha, squashfs_sha=squashfs_sha,
                   uploaded=(later.isoformat().replace("+00:00", "Z"), manifest_id + 3))


@dataclass
class FakeGitHubOrigin:
    """The GitHub Releases API for `repo` plus every release asset, served over real HTTP on
    127.0.0.1 so separate processes reach it (`PHOTO_WALL_RELEASE_API_BASE=api_base`).

    Counts every asset GET per (tag, "manifest"|"tarball"|"deb"). A tarball can be throttled
    (`chunk_delay` per 64 KiB) or held at half its body until `hold` is set, and answered with
    `tarball_status` instead. Use as a context manager; closing releases any held download.
    """

    repo: str
    releases: dict[str, FakeRelease] = field(default_factory=dict)
    hits: Counter = field(default_factory=Counter)
    hold: threading.Event | None = None
    chunk_delay: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    api_base: str = ""
    _server: ThreadingHTTPServer | None = None

    def count(self, tag: str, what: str) -> int:
        with self.lock:
            return self.hits[(tag, what)]

    def release_list(self) -> bytes:
        entries = []
        for release in sorted(self.releases.values(), key=lambda r: r.tag, reverse=True):
            url = f"{self.api_base}/dl/{release.tag}"
            entries.append(release_entry(
                release.tag, manifest_url=f"{url}/{MANIFEST_NAME}",
                tarball_url=f"{url}/{TARBALL_NAME}", deb_filename=release.deb_filename,
                deb_url=f"{url}/{release.deb_filename}", uploaded=release.uploaded))
        return json.dumps(entries).encode()

    def __enter__(self) -> FakeGitHubOrigin:
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _send(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                if url.path == f"/repos/{origin.repo}/releases":
                    page = parse_qs(url.query).get("page", ["1"])[0]
                    self._send(200, origin.release_list() if page == "1" else b"[]")
                    return
                parts = url.path.split("/")  # ['', 'dl', tag, name]
                release = origin.releases.get(parts[2]) if len(parts) == 4 else None
                name = parts[3] if release is not None else None
                what = {MANIFEST_NAME: "manifest", TARBALL_NAME: "tarball"}.get(
                    name, "deb" if release is not None and name == release.deb_filename else None)
                if what is None:
                    self._send(404)
                    return
                with origin.lock:
                    origin.hits[(release.tag, what)] += 1
                if what == "manifest":
                    self._send(200, manifest_bytes(
                        tarball=release.tarball, tarball_sha=release.tarball_sha,
                        deb=release.deb, deb_filename=release.deb_filename))
                elif what == "deb":
                    self._send(200, release.deb)
                elif release.tarball_status != 200:
                    self._send(release.tarball_status)
                else:
                    self._stream(release.tarball)

            def _stream(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                chunk, half = 64 * 1024, len(body) // 2
                try:
                    for offset in range(0, len(body), chunk):
                        hold = origin.hold
                        if hold is not None and offset >= half and not hold.is_set():
                            hold.wait(300)
                        self.wfile.write(body[offset:offset + chunk])
                        if origin.chunk_delay:
                            time.sleep(origin.chunk_delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the downloading process was killed

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.api_base = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True, name="fake-github").start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.hold is not None:
            self.hold.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
