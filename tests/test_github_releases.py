"""Offline unit tests for the GitHub release-source client (0010, bead 2).

No network: every request is served by an `httpx.MockTransport` route double
(mirroring `tests/test_immich.py`). The double serves the paginated releases
API on `api.github.com` and each release asset (`manifest.json`, the `.deb`) on
its `browser_download_url`, including a CDN 302 redirect hop.

Each guard has a test that turns red if the guard is removed (mutation-probe
intent): non-semver skip, draft/prerelease exclusion, schema!=1 undeployable,
missing/absent-asset undeployable, ETag 304 short-circuit, the streamed sha256
corruption check, and the running-total oversize abort with no partial file.
"""

import asyncio
import hashlib
import json

import httpx
import pytest

from central.github_releases import (
    GithubReleaseError,
    GithubReleaseSource,
)

REPO = "mcurcio/photo-wall"
ETAG = 'W/"releases-v1"'
BODY = b"pretend photo-wall-player_0.1.0+gabc_arm64.deb bytes; no real package.\n"
SHA = hashlib.sha256(BODY).hexdigest()
DEB_NAME = "photo-wall-player_0.1.0+gabc_arm64.deb"


def manifest_bytes(filename, sha256, size, *, schema=1):
    return json.dumps(
        {
            "schema": schema,
            "revision": "a" * 40,
            "player_deb": {
                "filename": filename,
                "sha256": sha256,
                "size": size,
                "version": "0.1.0+ga",
            },
        }
    ).encode()


def release(tag, *, prerelease=False, draft=False, assets=None):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "assets": assets or []}


def asset(name, url):
    return {"name": name, "browser_download_url": url}


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
        self.list_retry_after = None
        self.blobs = {}
        self.requests = []

    def blob(self, url, *, status=200, chunks=(BODY,), headers=None, redirect=None):
        self.blobs[url] = dict(
            status=status, chunks=list(chunks), headers=headers or {}, redirect=redirect
        )
        return url

    def deployable(self, tag="v1.2.3", *, prerelease=False, deb_body=BODY, schema=1):
        deb_url = f"https://github.com/{REPO}/releases/download/{tag}/{DEB_NAME}"
        manifest_url = f"https://github.com/{REPO}/releases/download/{tag}/manifest.json"
        sha = hashlib.sha256(deb_body).hexdigest()
        self.blob(manifest_url, chunks=[manifest_bytes(DEB_NAME, sha, len(deb_body), schema=schema)])
        self.blob(deb_url, chunks=[deb_body])
        row = release(
            tag,
            prerelease=prerelease,
            assets=[asset("manifest.json", manifest_url), asset(DEB_NAME, deb_url)],
        )
        self.releases.append(row)
        return row, sha, deb_url

    def handle(self, request):
        self.requests.append(request)
        if request.url.host == "api.github.com":
            return self._releases(request)
        url = str(request.url)
        if url not in self.blobs:
            return httpx.Response(404, content=_stream([]))
        spec = self.blobs[url]
        if spec["redirect"]:
            return httpx.Response(302, headers={"Location": spec["redirect"]})
        return httpx.Response(
            spec["status"], content=_stream(spec["chunks"]), headers=spec["headers"]
        )

    def _releases(self, request):
        if self.list_status != 200:
            headers = {}
            if self.list_retry_after is not None:
                headers["Retry-After"] = self.list_retry_after
            return httpx.Response(self.list_status, headers=headers, content=_stream([]))
        if self.match_etag is not None and request.headers.get("If-None-Match") == self.match_etag:
            return httpx.Response(304, headers={"ETag": self.etag})
        page = int(dict(request.url.params).get("page", "1"))
        start = (page - 1) * 100
        return httpx.Response(
            200, json=self.releases[start : start + 100], headers={"ETag": self.etag}
        )


def source(server, **kwargs):
    return GithubReleaseSource(REPO, transport=httpx.MockTransport(server.handle), **kwargs)


def discover(server, **kwargs):
    async def perform():
        async with source(server) as client:
            return await client.list_releases(**kwargs)

    return asyncio.run(perform())


def run_download(server, url, dest, **kwargs):
    async def perform():
        async with source(server, **kwargs.pop("client", {})) as client:
            return await client.download(url, dest, **kwargs)

    return asyncio.run(perform())


def only(records):
    assert len(records) == 1, records
    return records[0]


# -- discovery ---------------------------------------------------------------


def test_normal_release_becomes_a_deployable_record():
    server = Server()
    _, sha, deb_url = server.deployable("v1.2.3")
    result = discover(server)
    record = only(result.releases)
    assert (record.tag, record.deployable, record.reason) == ("v1.2.3", True, None)
    assert record.asset_sha256 == sha
    assert record.asset_size == len(BODY)
    assert record.asset_url == deb_url
    assert record.is_prerelease is False
    assert result.etag == ETAG and result.unchanged is False


def test_draft_release_is_excluded():
    server = Server()
    server.deployable("v1.2.3")
    server.releases.append(release("v9.9.9", draft=True))
    assert {r.tag for r in discover(server).releases} == {"v1.2.3"}


def test_prerelease_excluded_by_default_and_included_when_configured():
    server = Server()
    server.deployable("v1.2.3")
    server.deployable("v1.3.0-rc.1", prerelease=True)
    assert {r.tag for r in discover(server).releases} == {"v1.2.3"}
    included = discover(server, include_prereleases=True)
    record = next(r for r in included.releases if r.tag == "v1.3.0-rc.1")
    assert record.is_prerelease is True and record.deployable is True


def test_non_semver_tag_is_skipped_with_no_record():
    server = Server()
    server.deployable("v1.2.3")
    server.releases.append(release("release-2024"))
    server.releases.append(release("v1.2"))  # missing patch component
    assert {r.tag for r in discover(server).releases} == {"v1.2.3"}


def test_schema_mismatch_is_undeployable_and_does_not_crash():
    server = Server()
    server.deployable("v1.2.3", schema=2)
    record = only(discover(server).releases)
    assert record.deployable is False and record.reason == "schema_mismatch"
    assert record.asset_url is None


def test_missing_manifest_asset_is_undeployable():
    server = Server()
    deb_url = f"https://github.com/{REPO}/releases/download/v1.2.3/{DEB_NAME}"
    server.blob(deb_url, chunks=[BODY])
    server.releases.append(release("v1.2.3", assets=[asset(DEB_NAME, deb_url)]))
    record = only(discover(server).releases)
    assert record.deployable is False and record.reason == "no_manifest"
    assert record.asset_sha256 is None and record.asset_url is None


def test_manifest_names_deb_absent_from_assets_is_undeployable_divergent_signal():
    server = Server()
    manifest_url = f"https://github.com/{REPO}/releases/download/v1.2.3/manifest.json"
    server.blob(manifest_url, chunks=[manifest_bytes(DEB_NAME, SHA, len(BODY))])
    # manifest.json is attached, but the .deb it names is NOT.
    server.releases.append(release("v1.2.3", assets=[asset("manifest.json", manifest_url)]))
    record = only(discover(server).releases)
    assert record.deployable is False and record.reason == "asset_missing"
    # sha256/size are still reported so the store can tell undeployable from divergent.
    assert record.asset_sha256 == SHA and record.asset_size == len(BODY)
    assert record.asset_url is None


def test_manifest_asset_listed_but_absent_upstream_is_undeployable():
    server = Server()
    manifest_url = f"https://github.com/{REPO}/releases/download/v1.2.3/manifest.json"
    # listed in assets but no blob registered -> 404 on fetch.
    server.releases.append(release("v1.2.3", assets=[asset("manifest.json", manifest_url)]))
    record = only(discover(server).releases)
    assert record.deployable is False and record.reason == "no_manifest"


def test_etag_304_short_circuits_the_whole_poll():
    server = Server()
    server.deployable("v1.2.3")
    server.match_etag = ETAG
    result = discover(server, etag=ETAG)
    assert result.unchanged is True and result.releases == [] and result.etag == ETAG
    # Only the conditional list request was made; no manifest/asset was re-fetched.
    assert len(server.requests) == 1


def test_rate_limited_list_aborts_with_retry_after():
    server = Server()
    server.deployable("v1.2.3")
    server.list_status = 403
    server.list_retry_after = "60"
    with pytest.raises(GithubReleaseError) as excinfo:
        discover(server)
    assert excinfo.value.code == "rate_limited" and excinfo.value.retry_after == 60


def test_transient_manifest_failure_aborts_the_poll():
    server = Server()
    _, _, _ = server.deployable("v1.2.3")
    manifest_url = f"https://github.com/{REPO}/releases/download/v1.2.3/manifest.json"
    server.blob(manifest_url, status=500, chunks=[])
    with pytest.raises(GithubReleaseError) as excinfo:
        discover(server)
    assert excinfo.value.code == "manifest_unavailable"


# -- download ----------------------------------------------------------------


def test_download_streams_and_verifies_sha256(tmp_path):
    server = Server()
    _, sha, deb_url = server.deployable("v1.2.3")
    dest = tmp_path / "app.deb"
    result = run_download(server, deb_url, dest, sha256=sha)
    assert dest.read_bytes() == BODY
    assert result.sha256 == sha and result.size == len(BODY) and result.path == dest


def test_download_follows_cdn_redirect(tmp_path):
    server = Server()
    origin = f"https://github.com/{REPO}/releases/download/v1.2.3/{DEB_NAME}"
    cdn = "https://codeload.githubusercontent.com/signed/blob"
    server.blob(origin, redirect=cdn)
    server.blob(cdn, chunks=[BODY])
    dest = tmp_path / "app.deb"
    result = run_download(server, origin, dest, sha256=SHA)
    assert dest.read_bytes() == BODY and result.sha256 == SHA


def test_download_refuses_on_byte_flip_and_leaves_no_file(tmp_path):
    server = Server()
    _, _, deb_url = server.deployable("v1.2.3")
    dest = tmp_path / "app.deb"
    with pytest.raises(GithubReleaseError) as excinfo:
        run_download(server, deb_url, dest, sha256="0" * 64)
    assert excinfo.value.code == "download_corrupt"
    assert not dest.exists()


def test_download_refuses_over_max_bytes_streaming_and_leaves_no_file(tmp_path):
    server = Server()
    deb_url = f"https://github.com/{REPO}/releases/download/v1.2.3/{DEB_NAME}"
    # No Content-Length: the running-total abort is the only bound (0010 probe 4).
    server.blob(deb_url, chunks=[b"x" * 4096])
    dest = tmp_path / "app.deb"
    with pytest.raises(GithubReleaseError) as excinfo:
        run_download(server, deb_url, dest, max_bytes=1024)
    assert excinfo.value.code == "download_too_large"
    assert not dest.exists()


def test_download_refuses_over_max_bytes_via_declared_length(tmp_path):
    server = Server()
    deb_url = f"https://github.com/{REPO}/releases/download/v1.2.3/{DEB_NAME}"
    server.blob(deb_url, chunks=[b"x" * 4096], headers={"content-length": "4096"})
    dest = tmp_path / "app.deb"
    with pytest.raises(GithubReleaseError) as excinfo:
        run_download(server, deb_url, dest, max_bytes=1024)
    assert excinfo.value.code == "download_too_large"
    assert not dest.exists()


def test_download_refuses_truncated_body_against_declared_length(tmp_path):
    server = Server()
    deb_url = f"https://github.com/{REPO}/releases/download/v1.2.3/{DEB_NAME}"
    server.blob(deb_url, chunks=[BODY], headers={"content-length": "9999"})
    dest = tmp_path / "app.deb"
    with pytest.raises(GithubReleaseError) as excinfo:
        run_download(server, deb_url, dest)
    assert excinfo.value.code == "download_truncated"
    assert not dest.exists()


def test_download_refuses_to_overwrite_existing_destination(tmp_path):
    server = Server()
    _, sha, deb_url = server.deployable("v1.2.3")
    dest = tmp_path / "app.deb"
    dest.write_bytes(b"prior")
    with pytest.raises(GithubReleaseError) as excinfo:
        run_download(server, deb_url, dest, sha256=sha)
    assert excinfo.value.code == "destination_exists"
    assert dest.read_bytes() == b"prior"  # the prior file is never touched


def test_invalid_repo_slug_is_refused():
    with pytest.raises(GithubReleaseError) as excinfo:
        GithubReleaseSource("not-a-slug")
    assert excinfo.value.code == "invalid_repo"
