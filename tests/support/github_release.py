"""The GitHub Releases wire as tests serve it, and a fake origin HTTP server.

A release here carries the base tarball `scripts/package_release_artifacts.py` builds and one
sealed environment blob; its list entry names both (`tag_name`, `draft`, `prerelease`,
`assets[].browser_download_url`). It attaches no node release manifest, so the release sync lists
and skips it: the test that serves it writes its node deployment directly. The real-process
two-pod test serves these bytes.
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
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TARBALL_NAME = "photo-wall-base.tar.gz"
ENVIRONMENT_NAME = "environment.tar"
# The first asset's (updated_at, id): GitHub's asset `updated_at` format.
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


def release_entry(tag: str, *, tarball_url: str, environment_url: str,
                  uploaded: tuple[str, int] = FIRST_UPLOAD) -> dict:
    """One published (non-draft, non-prerelease) entry of the releases list. Every asset was
    uploaded at `uploaded` = (updated_at, the first asset's id); the other takes the next id."""
    updated_at, first_id = uploaded
    names_and_urls = ((TARBALL_NAME, tarball_url), (ENVIRONMENT_NAME, environment_url))
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [{"id": first_id + offset, "name": name, "updated_at": updated_at,
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
    environment: bytes
    tarball_status: int = 200  # 404 makes the OS image terminally unobtainable
    uploaded: tuple[str, int] = FIRST_UPLOAD  # the first asset's (updated_at, id)


def make_release(tag: str, *, squashfs_bytes: int = 4 * 1024 * 1024,
                 environment_bytes: int = 64 * 1024) -> FakeRelease:
    """Random (incompressible) squashfs and environment bytes, so either is big enough to
    throttle or hold."""
    tarball, tarball_sha, squashfs_sha = real_tarball(os.urandom(squashfs_bytes))
    return FakeRelease(tag, tarball, tarball_sha, squashfs_sha, os.urandom(environment_bytes))


@dataclass
class FakeGitHubOrigin:
    """The GitHub Releases API for `repo` plus every release asset, served over real HTTP on
    127.0.0.1 so separate processes reach it (`PHOTO_WALL_RELEASE_API_BASE=api_base`).

    Counts every asset GET per (tag, "tarball"|"environment"). Either can be throttled (`chunk_delay` per 64 KiB) or held at half its body until `hold` is set, and a
    tarball answered with `tarball_status` instead. Use as a context manager; closing releases any held download.
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
                release.tag, tarball_url=f"{url}/{TARBALL_NAME}",
                environment_url=f"{url}/{ENVIRONMENT_NAME}", uploaded=release.uploaded))
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
                what = {TARBALL_NAME: "tarball", ENVIRONMENT_NAME: "environment"}.get(name)
                if what is None:
                    self._send(404)
                    return
                with origin.lock:
                    origin.hits[(release.tag, what)] += 1
                if what == "environment":
                    self._stream(release.environment)
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
