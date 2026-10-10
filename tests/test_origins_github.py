"""Offline unit tests for the GitHub `ReleaseOrigin`.

Ported from `tests/test_github_releases.py`, then extended with one test per failure
classification. No network: every request is served by an `httpx.MockTransport` route double
that serves the paginated releases API on `api.github.com` and each release asset on its
`browser_download_url`, including a CDN 302 hop.
"""

import asyncio
import hashlib
import os
import stat
from datetime import timedelta

import httpx
import pytest

from central.kernel.assets import OriginLocator
from central.kernel.handling import OriginRejected, OriginUnavailable
from central.kernel.ports import NODE_RELEASE_INVALID, ReleaseOrigin, UpstreamVersion
from central.origins.github import MAX_DOWNLOAD_BYTES, NODE_MANIFEST_MISSING, GitHubReleaseOrigin
from contracts.node_release import NODE_RELEASE_MANIFEST

REPO = "mcurcio/photo-wall"
ETAG = 'W/"releases-v1"'
BODY = b"pretend photo-wall-player_0.1.0+gabc_arm64.deb bytes; no real package.\n"
SHA = hashlib.sha256(BODY).hexdigest()
DEB_NAME = "photo-wall-player_0.1.0+gabc_arm64.deb"
DOWNLOAD = f"https://github.com/{REPO}/releases/download/v1.2.3/{DEB_NAME}"
# A node manifest body the contract refuses: the release is listed with `node_release_invalid`.
# Valid node manifests are tests/test_node_release_ingest.py's.
REFUSED = b"{}"


def manifest_url(tag="v1.2.3"):
    return f"https://github.com/{REPO}/releases/download/{tag}/{NODE_RELEASE_MANIFEST}"


def release(tag, *, prerelease=False, draft=False, assets=None):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "assets": assets or []}


def asset(name, url, **version):
    """A release asset; `version` may add GitHub's `id` and `updated_at`."""
    return {"name": name, "browser_download_url": url, **version}


def _stream(chunks):
    async def gen():
        for chunk in chunks:
            yield chunk

    return gen()


class Server:
    """Route-aware GitHub double: the releases API plus per-URL asset blobs."""

    def __init__(self):
        self.releases = []
        self.etag = ETAG
        self.match_etag = None
        self.list_status = 200
        self.list_headers = {}
        self.list_body = None  # raw bytes override for page bodies
        self.list_error = None  # an httpx exception raised for the list request
        self.blobs = {}
        self.requests = []

    def blob(self, url, *, status=200, chunks=(BODY,), headers=None, redirect=None, error=None):
        self.blobs[url] = dict(status=status, chunks=list(chunks), headers=headers or {},
                               redirect=redirect, error=error)
        return url

    def deployable(self, tag="v1.2.3", *, prerelease=False, body=REFUSED, **version):
        """A release attaching a node manifest `body` (None: listed but 404 upstream); `version`
        may add the manifest asset's GitHub `id` and `updated_at`."""
        if body is not None:
            self.blob(manifest_url(tag), chunks=[body])
        row = release(tag, prerelease=prerelease,
                      assets=[asset(NODE_RELEASE_MANIFEST, manifest_url(tag), **version)])
        self.releases.append(row)
        return row

    def handle(self, request):
        self.requests.append(request)
        if request.url.host == "api.github.com":
            return self._releases(request)
        url = str(request.url)
        if url not in self.blobs:
            return httpx.Response(404, content=_stream([]))
        spec = self.blobs[url]
        if spec["error"] is not None:
            raise spec["error"]
        if spec["redirect"]:
            return httpx.Response(302, headers={"Location": spec["redirect"]})
        return httpx.Response(spec["status"], content=_stream(spec["chunks"]),
                              headers=spec["headers"])

    def _releases(self, request):
        if self.list_error is not None:
            raise self.list_error
        if self.list_status != 200:
            return httpx.Response(self.list_status, headers=self.list_headers,
                                  content=_stream([]))
        if self.match_etag is not None and request.headers.get("If-None-Match") == self.match_etag:
            return httpx.Response(304, headers={"ETag": self.etag})
        if self.list_body is not None:
            return httpx.Response(200, content=_stream([self.list_body]),
                                  headers={"ETag": self.etag, **self.list_headers})
        page = int(dict(request.url.params).get("page", "1"))
        start = (page - 1) * 100
        return httpx.Response(200, json=self.releases[start : start + 100],
                              headers={"ETag": self.etag})


def origin(server, **kwargs):
    return GitHubReleaseOrigin(REPO, transport=httpx.MockTransport(server.handle), **kwargs)


def discover(server, *, etag=None, **kwargs):
    return asyncio.run(origin(server, **kwargs).list_releases(etag=etag))


def run_download(server, locator, into, *, max_bytes=MAX_DOWNLOAD_BYTES, **kwargs):
    return asyncio.run(origin(server, **kwargs).download(locator, into, max_bytes=max_bytes))


def locator(url=DOWNLOAD, *, sha256=SHA, size=len(BODY)):
    return OriginLocator(url=url, sha256=sha256, size=size)


def only(records):
    assert len(records) == 1, records
    return records[0]


def raises(kind, reason, fn, *args, **kwargs):
    with pytest.raises(kind) as excinfo:
        fn(*args, **kwargs)
    assert excinfo.value.reason == reason
    return excinfo.value


def test_implements_the_kernel_port():
    port: ReleaseOrigin = GitHubReleaseOrigin(REPO)
    assert port is not None


# -- D1 listing ----------------------------------------------------------------


def test_a_release_is_listed_by_its_node_manifest():
    server = Server()
    server.deployable("v1.2.3")
    result = discover(server)
    record = only(result.releases)
    assert (record.tag, record.is_prerelease) == ("v1.2.3", False)
    assert (record.node_publication, record.node_problem) == (None, NODE_RELEASE_INVALID)
    assert result.etag == ETAG and result.unchanged is False


def test_a_release_without_a_node_manifest_is_not_listed_and_its_v1_files_are_not_read():
    server = Server()
    v1 = [f"https://github.com/{REPO}/releases/download/v1.2.3/{name}"
          for name in ("manifest.json", "manifest.v2.json", DEB_NAME)]
    for url in v1:
        server.blob(url, chunks=[b"{}"])
    server.releases.append(release("v1.2.3", assets=[asset(url.rsplit("/", 1)[1], url) for url in v1]))
    assert discover(server).releases == ()
    assert [r.url.host for r in server.requests] == ["api.github.com"]


def test_draft_release_is_excluded():
    server = Server()
    server.deployable("v1.2.3")
    server.releases.append(release("v9.9.9", draft=True))
    assert {r.tag for r in discover(server).releases} == {"v1.2.3"}


def test_a_prerelease_is_listed_and_labelled():
    server = Server()
    server.deployable("v1.2.3")
    server.deployable("v1.3.0-rc.1", prerelease=True)
    assert {(r.tag, r.is_prerelease) for r in discover(server).releases} == {
        ("v1.2.3", False), ("v1.3.0-rc.1", True)}


def test_non_semver_tag_is_skipped_with_no_record():
    server = Server()
    server.deployable("v1.2.3")
    for tag in ("release-2024", "v1.2", "v1.2.3+build"):  # no patch; build metadata is rejected
        server.deployable(tag)
    assert {r.tag for r in discover(server).releases} == {"v1.2.3"}


def test_a_node_manifest_listed_but_absent_upstream_is_that_release_problem():
    server = Server()
    server.deployable("v1.2.3", body=None)
    assert only(discover(server).releases).node_problem == NODE_MANIFEST_MISSING


# -- the upstream version: the node manifest asset's (updated_at, id) ----------


UPLOADED = {"id": 7, "updated_at": "2026-09-01T00:00:00Z"}


def _versioned(**version):
    server = Server()
    server.deployable("v1.2.3", **version)
    return only(discover(server).releases)


def test_the_node_manifest_carries_its_assets_updated_at_and_id():
    assert _versioned(**UPLOADED).node_version == UpstreamVersion(1788220800.0, 7)


def test_an_offset_updated_at_is_read_as_the_same_instant():
    record = _versioned(id=7, updated_at="2026-09-01T02:00:00+02:00")
    assert record.node_version == UpstreamVersion(1788220800.0, 7)


@pytest.mark.parametrize("version", [
    {},  # neither
    {"updated_at": "2026-09-01T00:00:00Z"},  # no id
    {"id": 7},  # no updated_at
    {"id": 0, "updated_at": "2026-09-01T00:00:00Z"},
    {"id": "7", "updated_at": "2026-09-01T00:00:00Z"},
    {"id": True, "updated_at": "2026-09-01T00:00:00Z"},
    {"id": 7, "updated_at": "yesterday"},
    {"id": 7, "updated_at": "2026-09-01T00:00:00"},  # naive: no offset
    {"id": 7, "updated_at": 1788220800},
])
def test_an_invalid_id_or_updated_at_is_no_version(version):
    assert _versioned(**version).node_version is None


def test_etag_304_short_circuits_the_whole_listing():
    server = Server()
    server.deployable("v1.2.3")
    server.match_etag = ETAG
    result = discover(server, etag=ETAG)
    assert result.unchanged is True and result.releases == () and result.etag == ETAG
    assert len(server.requests) == 1  # no manifest was re-fetched


def test_pagination_follows_full_pages_and_stops_at_a_short_one():
    server = Server()
    for index in range(150):
        server.deployable(f"v0.0.{index}")
    result = discover(server)
    assert len(result.releases) == 150
    pages = [r for r in server.requests if r.url.host == "api.github.com"]
    assert [dict(r.url.params)["page"] for r in pages] == ["1", "2"]


def test_pagination_is_capped():
    server = Server()
    for index in range(100 * 21):
        server.releases.append(release(f"v0.{index // 1000}.{index % 1000}"))
    discover(server)
    assert len([r for r in server.requests if r.url.host == "api.github.com"]) == 20


# -- failure classification (one test per case) --------------------------------


def test_list_connect_error_is_origin_unreachable():
    server = Server()
    server.list_error = httpx.ConnectError("refused")
    raises(OriginUnavailable, "origin_unreachable", discover, server)


def test_list_timeout_is_origin_unreachable():
    server = Server()
    server.list_error = httpx.ReadTimeout("slow")
    raises(OriginUnavailable, "origin_unreachable", discover, server)


def test_list_5xx_is_origin_error():
    server = Server()
    server.list_status = 502
    raises(OriginUnavailable, "origin_error", discover, server)


def test_list_other_4xx_is_rejected():
    server = Server()
    server.list_status = 404
    raises(OriginRejected, "list_rejected", discover, server)


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limited_list_carries_retry_after(status):
    server = Server()
    server.list_status = status
    server.list_headers = {"Retry-After": "60"}
    error = raises(OriginUnavailable, "rate_limited", discover, server)
    assert error.retry_after == timedelta(seconds=60)


def test_rate_limited_without_retry_after_has_none():
    server = Server()
    server.list_status = 429
    assert raises(OriginUnavailable, "rate_limited", discover, server).retry_after is None


def test_rate_limited_manifest_aborts_the_listing():
    server = Server()
    server.deployable("v1.2.3")
    server.blob(manifest_url(), status=429, chunks=[], headers={"Retry-After": "5"})
    error = raises(OriginUnavailable, "rate_limited", discover, server)
    assert error.retry_after == timedelta(seconds=5)


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limited_download(tmp_path, status):
    server = Server()
    server.blob(DOWNLOAD, status=status, chunks=[], headers={"Retry-After": "7"})
    into = tmp_path / "app.deb"
    error = raises(OriginUnavailable, "rate_limited", run_download, server, locator(), into)
    assert error.retry_after == timedelta(seconds=7)
    assert not into.exists()


def test_transient_manifest_5xx_aborts_the_listing_never_partial():
    server = Server()
    server.deployable("v1.2.3")
    server.deployable("v1.2.4")
    server.blob(manifest_url("v1.2.4"), status=500, chunks=[])
    raises(OriginUnavailable, "manifest_unavailable", discover, server)


def test_unreachable_manifest_aborts_the_listing():
    server = Server()
    server.deployable("v1.2.3")
    server.blob(manifest_url(), error=httpx.ConnectError("reset"))
    raises(OriginUnavailable, "manifest_unavailable", discover, server)


@pytest.mark.parametrize("status", [404, 410])
def test_download_gone_is_rejected_not_found(tmp_path, status):
    server = Server()
    server.blob(DOWNLOAD, status=status, chunks=[])
    into = tmp_path / "app.deb"
    raises(OriginRejected, "download_not_found", run_download, server, locator(), into)
    assert not into.exists()


def test_download_5xx_is_origin_error(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, status=503, chunks=[])
    into = tmp_path / "app.deb"
    raises(OriginUnavailable, "origin_error", run_download, server, locator(), into)
    assert not into.exists()


def test_download_connect_error_is_origin_unreachable(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, error=httpx.ConnectError("refused"))
    into = tmp_path / "app.deb"
    raises(OriginUnavailable, "origin_unreachable", run_download, server, locator(), into)
    assert not into.exists()


def test_download_refuses_over_max_bytes_streaming(tmp_path):
    server = Server()
    # No Content-Length: the running-total abort is the only bound.
    server.blob(DOWNLOAD, chunks=[b"x" * 4096])
    into = tmp_path / "app.deb"
    raises(OriginRejected, "download_too_large", run_download, server,
           locator(sha256=None, size=None), into, max_bytes=1024)
    assert not into.exists()


def test_download_refuses_over_max_bytes_via_declared_length(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, chunks=[b"x" * 4096], headers={"content-length": "4096"})
    into = tmp_path / "app.deb"
    raises(OriginRejected, "download_too_large", run_download, server,
           locator(sha256=None, size=None), into, max_bytes=1024)
    assert not into.exists()


def test_download_refuses_when_locator_size_exceeds_max_bytes(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, chunks=[BODY])
    into = tmp_path / "app.deb"
    raises(OriginRejected, "download_too_large", run_download, server, locator(), into,
           max_bytes=len(BODY) - 1)
    assert not into.exists()


def test_download_refuses_non_identity_encoding(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, chunks=[BODY], headers={"content-encoding": "gzip"})
    into = tmp_path / "app.deb"
    raises(OriginRejected, "download_encoding", run_download, server, locator(), into)
    assert not into.exists()


def test_download_refuses_truncated_body_against_declared_length(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, chunks=[BODY], headers={"content-length": "9999"})
    into = tmp_path / "app.deb"
    raises(OriginUnavailable, "download_truncated", run_download, server, locator(), into)
    assert not into.exists()


def test_download_refuses_empty_body_as_truncated(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, chunks=[])
    into = tmp_path / "app.deb"
    raises(OriginUnavailable, "download_truncated", run_download, server,
           locator(sha256=None, size=None), into)
    assert not into.exists()


def test_download_refuses_on_byte_flip(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, chunks=[BODY])
    into = tmp_path / "app.deb"
    raises(OriginUnavailable, "download_corrupt", run_download, server,
           locator(sha256="0" * 64), into)
    assert not into.exists()


def test_download_refuses_on_size_mismatch(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, chunks=[BODY])
    into = tmp_path / "app.deb"
    raises(OriginUnavailable, "download_corrupt", run_download, server,
           locator(size=len(BODY) + 1), into)
    assert not into.exists()


def test_list_page_not_json_is_list_invalid():
    server = Server()
    server.list_body = b"<html>oops</html>"
    raises(OriginRejected, "list_invalid", discover, server)


def test_list_page_not_a_list_is_list_invalid():
    server = Server()
    server.list_body = b'{"message": "not a list"}'
    raises(OriginRejected, "list_invalid", discover, server)


def test_download_refuses_to_overwrite_existing_destination(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, chunks=[BODY])
    into = tmp_path / "app.deb"
    into.write_bytes(b"prior")
    with pytest.raises(FileExistsError):
        run_download(server, locator(), into)
    assert into.read_bytes() == b"prior"  # the prior file is never touched
    assert server.requests == []  # nothing was fetched


def test_local_write_error_is_reraised_and_into_removed(tmp_path, monkeypatch):
    server = Server()
    server.blob(DOWNLOAD, chunks=[BODY])
    into = tmp_path / "app.deb"
    real_fdopen = os.fdopen

    class FullDisk:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, _chunk):
            raise OSError(28, "No space left on device")

        def __getattr__(self, name):
            return getattr(self.handle, name)

    monkeypatch.setattr(os, "fdopen", lambda fd, mode: FullDisk(real_fdopen(fd, mode)))
    with pytest.raises(OSError) as excinfo:
        run_download(server, locator(), into)
    assert excinfo.value.errno == 28
    assert not isinstance(excinfo.value, (OriginUnavailable, OriginRejected))
    assert not into.exists()


# -- D3 download ---------------------------------------------------------------


def test_download_streams_verifies_and_writes_mode_0600(tmp_path):
    server = Server()
    server.blob(DOWNLOAD, chunks=[BODY[:10], BODY[10:]])
    into = tmp_path / "app.deb"
    assert run_download(server, locator(), into) is None
    assert into.read_bytes() == BODY
    assert stat.S_IMODE(into.stat().st_mode) == 0o600


def test_download_fsyncs_off_the_event_loop(tmp_path, monkeypatch):
    import threading

    import central.origins.github as module

    synced = []
    real = os.fsync

    def fsync(fd):
        synced.append(threading.current_thread() is threading.main_thread())
        real(fd)

    monkeypatch.setattr(module.os, "fsync", fsync)
    server = Server()
    server.blob(DOWNLOAD, chunks=[BODY])
    run_download(server, locator(), tmp_path / "app.deb")
    assert synced == [False]  # a worker thread, never the loop's


def test_download_follows_cdn_redirect(tmp_path):
    server = Server()
    cdn = "https://objects.githubusercontent.com/signed/blob"
    server.blob(DOWNLOAD, redirect=cdn)
    server.blob(cdn, chunks=[BODY])
    into = tmp_path / "app.deb"
    run_download(server, locator(), into)
    assert into.read_bytes() == BODY


def test_authorization_never_follows_a_cross_host_redirect(tmp_path):
    server = Server()
    cdn = "https://objects.githubusercontent.com/signed/blob"
    server.blob(DOWNLOAD, redirect=cdn)
    server.blob(cdn, chunks=[BODY])
    run_download(server, locator(), tmp_path / "app.deb", token="secret-token")
    first, second = server.requests
    assert first.headers.get("Authorization") == "Bearer secret-token"
    assert second.url.host == "objects.githubusercontent.com"
    assert "Authorization" not in second.headers


def test_listing_sends_the_token_to_the_api_host():
    server = Server()
    discover(server, token="secret-token")
    assert server.requests[0].headers.get("Authorization") == "Bearer secret-token"


@pytest.mark.parametrize("max_bytes", [0, -1, MAX_DOWNLOAD_BYTES + 1, True, 1.5])
def test_download_max_bytes_bound_is_validated(tmp_path, max_bytes):
    into = tmp_path / "app.deb"
    with pytest.raises(ValueError):
        run_download(Server(), locator(), into, max_bytes=max_bytes)
    assert not into.exists()


def test_max_download_bytes_is_one_gibibyte():
    assert MAX_DOWNLOAD_BYTES == 1024**3


# -- construction ----------------------------------------------------------------


def test_invalid_repo_slug_is_refused():
    with pytest.raises(ValueError, match="invalid_repo"):
        GitHubReleaseOrigin("not-a-slug")


def test_from_env_defaults():
    built = GitHubReleaseOrigin.from_env({})
    assert built.repo == "mcurcio/photo-wall"


def test_from_env_repo_and_token_reach_the_wire():
    server = Server()
    built = GitHubReleaseOrigin.from_env(
        {"PHOTO_WALL_RELEASE_REPO": "acme/wall", "PHOTO_WALL_RELEASE_TOKEN": "tok"})
    built._transport = httpx.MockTransport(server.handle)
    asyncio.run(built.list_releases(etag=None))
    request = server.requests[0]
    assert request.url.path == "/repos/acme/wall/releases"
    assert request.headers.get("Authorization") == "Bearer tok"


def _listed_urls(env):
    """The listing URLs (no query) a `from_env` origin requests; every page answers `[]`."""
    urls = []

    def handle(request):
        urls.append(str(request.url.copy_with(query=None)))
        return httpx.Response(200, json=[])

    built = GitHubReleaseOrigin.from_env(env)
    built._transport = httpx.MockTransport(handle)
    asyncio.run(built.list_releases(etag=None))
    return urls


@pytest.mark.parametrize("env", [{}, {"PHOTO_WALL_RELEASE_API_BASE": ""}])
def test_from_env_api_base_defaults_to_github(env):
    assert _listed_urls(env) == ["https://api.github.com/repos/mcurcio/photo-wall/releases"]


@pytest.mark.parametrize("base", ["http://127.0.0.1:8123", "https://ghe.example.test/api/v3/"])
def test_from_env_api_base_reaches_the_wire(base):
    assert _listed_urls({"PHOTO_WALL_RELEASE_API_BASE": base}) == [
        base.rstrip("/") + "/repos/mcurcio/photo-wall/releases"]


@pytest.mark.parametrize("base", ["api.github.com", "ftp://api.github.com", "file:///etc/passwd",
                                  "http://", "https://h.test/api?x=1", "https://h.test/#frag",
                                  "https://h.test:notaport", " https://h.test"])
def test_from_env_invalid_api_base_is_refused_at_construction(base):
    with pytest.raises(ValueError, match="invalid_api_base"):
        GitHubReleaseOrigin.from_env({"PHOTO_WALL_RELEASE_API_BASE": base})
