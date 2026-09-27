"""The seal (scripts/release_seal.py), the ONE writer of a release, against a scripted GitHub
releases API served on localhost (the real adapter talks HTTP to it) and a scripted registry.

Each guarantee has its test: the claim and its compare-and-swap refuse a stale run; nothing is
written before the packaged release is exactly the declared one and every image resolves; the
newest commit wins an unpublished version's image tags, and a release is published only while
they name its own build; the release is invisible until the one publish, which reconciles an
ambiguous error to published; a re-run after a partial seal converges, and after a publish is a
no-op; and the seal never issues a DELETE nor writes to a published release -- every test's
GitHub asserts both at teardown.
"""

from __future__ import annotations

import hashlib
import http.server
import itertools
import json
import os
import socket
import stat
import threading
from dataclasses import dataclass, field, replace
from urllib.parse import parse_qs, urlparse

import pytest
from support.release_build import (
    EPOCH,
    IMAGE_REFERENCES,
    REPOSITORY,
    REVISION,
    digest,
    inputs,
    manifest_blob,
    reference,
)

from contracts.release import CHECKSUMS, IMAGES, MANIFEST
from scripts import release_seal
from scripts.package_release_artifacts import package, verify
from scripts.release_seal import (
    Build,
    Buildx,
    Draft,
    GitHubApi,
    SealError,
    claim,
    highest_published,
    promote,
    seal,
)

OTHER = "b" * 40
PREVIOUS = "c" * 40
NEWER = "d" * 40                     # a later commit on main than REVISION
TAG, SINCE = "v0.9.1", "v0.9.0"
REPO_PATH = "/repos/owner/repo"


# --- a scripted GitHub ---------------------------------------------------------------------------
#
# Two hosts, as GitHub has: the API (api.github.com) and the upload/download host
# (uploads.github.com, and the signed download host an asset URL redirects to). Each refuses the
# other's requests, and the download host refuses a request still carrying the token.

@dataclass
class State:
    """One repository's releases and tags, as GitHub keeps them."""
    visible: bool = True
    refs: dict[str, str] = field(default_factory=dict)          # tag -> commit or tag object
    tag_objects: dict[str, str] = field(default_factory=dict)   # annotated tag -> what it tags
    heads: dict[str, str] = field(default_factory=lambda: {"main": REVISION})
    history: list[str] = field(default_factory=lambda: [PREVIOUS, REVISION, NEWER])  # main's
    releases: list[dict] = field(default_factory=list)       # newest first, as GitHub lists them
    blobs: dict[tuple[int, str], bytes] = field(default_factory=dict)
    latest: int | None = None
    # (method, route) -> queued faults, each "after" (apply, then 502), "before" (502 unapplied),
    # "drop" (apply, then close the connection unanswered), "reject" (422 unapplied), "broken"
    # (an upload left half-done, then 502) or "error" (a read's 500).
    faults: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    read_lag: int = 0          # reads of a release that still show it as it was before a PATCH
    digest_lag: int = 0        # reads of a new asset that show its digest as null
    seen: list[tuple[str, str]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    journal: list[str] = field(default_factory=list)          # shared with the registry
    ids: itertools.count = field(default_factory=lambda: itertools.count(100))
    api: str = ""
    uploads: str = ""

    def release(self, release_id: int) -> dict | None:
        return next((release for release in self.releases if release["id"] == release_id), None)

    def add(self, tag: str, target: str, *, draft: bool, body: str = "") -> dict:
        release = {"id": next(self.ids), "tag_name": tag, "target_commitish": target,
                   "name": tag, "body": body, "draft": draft, "prerelease": False, "assets": []}
        self.releases.insert(0, release)
        if not draft:
            self.refs.setdefault(tag, target)
        return release

    def attach(self, release: dict, name: str, content: bytes, *, state: str = "uploaded") -> dict:
        release["assets"].append({
            "id": next(self.ids), "name": name, "size": len(content), "state": state,
            "digest": f"sha256:{hashlib.sha256(content).hexdigest()}" if state == "uploaded"
            else None, "_pending": self.digest_lag})
        self.blobs[(release["id"], name)] = content
        return release["assets"][-1]

    def asset_view(self, asset: dict) -> dict:
        view = {key: value for key, value in asset.items() if not key.startswith("_")}
        view["url"] = f"{self.api}{REPO_PATH}/releases/assets/{asset['id']}"
        if asset["_pending"] > 0:                     # GitHub has not computed the digest yet
            asset["_pending"] -= 1
            view["digest"] = None
        return view

    def view(self, release: dict) -> dict:
        shown = release
        if release.get("_stale"):                     # a read that lags the last PATCH
            count, shown = release["_stale"]
            release["_stale"] = (count - 1, shown) if count > 1 else None
        return {**{key: value for key, value in shown.items() if not key.startswith("_")},
                "html_url": f"https://github.com/owner/repo/releases/{release['id']}",
                "upload_url": f"{self.uploads}{REPO_PATH}/releases/{release['id']}/assets"
                              "{?name,label}",
                "assets": [self.asset_view(asset) for asset in shown["assets"]]}

    def writes(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path in self.seen if method != "GET"]


class _Base(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> State:
        return self.server.state

    def _send(self, status: int, body: object, headers: dict[str, str] | None = None) -> None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _fault(self, method: str, route: str) -> str | None:
        queued = self.state.faults.get((method, route)) or []
        return queued.pop(0) if queued else None

    def _answer(self, fault: str | None, status: int, body: object) -> None:
        if fault == "drop":
            self.close_connection = True
            return
        if fault in ("after", "broken"):
            self._send(502, {"message": "Bad Gateway"})
            return
        self._send(status, body)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _refuse(self, method: str, why: str) -> None:
        self.state.seen.append((method, urlparse(self.path).path))
        self.state.violations.append(f"{method} {self.path}: {why}")
        self._send(405, {"message": why})

    def do_DELETE(self):  # noqa: N802 (the stdlib's name)
        self._refuse("DELETE", "the seal never deletes")

    def do_PUT(self):  # noqa: N802
        self._refuse("PUT", "the seal never replaces")

    def log_message(self, *args):
        pass


class _Api(_Base):
    """api.github.com."""

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        state = self.state
        state.seen.append(("GET", url.path))
        parts = url.path.removeprefix(REPO_PATH).strip("/").split("/")
        route = "repo" if parts == [""] else parts[0] if parts[0] != "git" else "ref"
        if (status := self._fault("GET", route)) == "error":
            return self._send(500, {"message": "trouble"})
        if status and status.isdigit():
            return self._send(int(status), {"message": "scripted"})
        if not url.path.startswith(REPO_PATH) or not state.visible:
            return self._send(404, {"message": "Not Found"})
        if parts == [""]:
            return self._send(200, {"full_name": "owner/repo"})
        if parts[:2] == ["git", "ref"] and parts[2] in ("tags", "heads"):
            name, refs = "/".join(parts[3:]), state.refs if parts[2] == "tags" else state.heads
            if name not in refs:
                return self._send(404, {"message": "Not Found"})
            kind = "tag" if refs[name] in state.tag_objects else "commit"
            return self._send(200, {"ref": f"refs/{parts[2]}/{name}",
                                    "object": {"type": kind, "sha": refs[name]}})
        if parts[:2] == ["git", "tags"] and parts[2] in state.tag_objects:
            tagged = state.tag_objects[parts[2]]
            kind = "tag" if tagged in state.tag_objects else "commit"
            return self._send(200, {"sha": parts[2], "object": {"type": kind, "sha": tagged}})
        if parts[0] == "compare":
            base, head = parts[1].split("...")
            if base not in state.history or head not in state.history:
                return self._send(404, {"message": "No common ancestor"})
            order = state.history.index(head) - state.history.index(base)
            return self._send(200, {"status": "ahead" if order > 0 else "behind" if order < 0
                                    else "identical"})
        if parts == ["releases"]:
            query = parse_qs(url.query)
            per_page, page = int(query["per_page"][0]), int(query["page"][0])
            chunk = state.releases[(page - 1) * per_page:page * per_page]
            return self._send(200, [state.view(release) for release in chunk])
        if parts[:2] == ["releases", "assets"]:
            for release in state.releases:
                for asset in release["assets"]:
                    if str(asset["id"]) == parts[2]:
                        location = f"{state.uploads}/cdn/{release['id']}/{asset['name']}"
                        return self._send(302, b"", {"Location": location})
            return self._send(404, {"message": "Not Found"})
        if len(parts) == 2 and parts[0] == "releases" and parts[1].isdigit():
            release = state.release(int(parts[1]))
            if release is None:
                return self._send(404, {"message": "Not Found"})
            return self._send(200, state.view(release))
        return self._send(404, {"message": "Not Found"})

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        state, body = self.state, self._body()
        if url.path != f"{REPO_PATH}/releases":
            return self._refuse("POST", "the API host takes no upload")
        state.seen.append(("POST", url.path))
        fault = self._fault("POST", "create")
        if fault in ("before", "reject"):
            return self._send(502 if fault == "before" else 422, {"message": "scripted"})
        request = json.loads(body)
        if request.get("draft") is not True:
            state.violations.append(f"created a non-draft release {request}")
        release = state.add(request["tag_name"], request["target_commitish"], draft=True,
                            body=request["body"])
        release["name"] = request["name"]
        state.journal.append(f"create {release['id']}")
        return self._answer(fault, 201, state.view(release))

    def do_PATCH(self):  # noqa: N802
        url = urlparse(self.path)
        state, request = self.state, json.loads(self._body())
        state.seen.append(("PATCH", url.path))
        release = state.release(int(url.path.rsplit("/", 1)[1]))
        fault = self._fault("PATCH", "publish")
        if release is None:
            return self._send(404, {"message": "Not Found"})
        if release["draft"] is not True:
            state.violations.append(f"PATCH to published release {release['id']}: {request}")
        if fault in ("before", "reject"):
            return self._send(502 if fault == "before" else 422, {"message": "scripted"})
        before = json.loads(json.dumps(release))
        tag = release["tag_name"]
        if request.get("draft") is False:
            if any(other["tag_name"] == tag and not other["draft"] for other in state.releases):
                return self._send(422, {"message": "tag_name already_exists"})
            release["draft"] = False
            state.refs.setdefault(tag, release["target_commitish"])
            if request.get("make_latest") == "true":
                state.latest = release["id"]
            state.journal.append(f"publish {release['id']}")
        release.update({key: request[key] for key in ("name", "body") if key in request})
        answer = state.view(release)
        if state.read_lag:
            before.pop("_stale", None)
            release["_stale"] = (state.read_lag, before)
        return self._answer(fault, 200, answer)


class _Uploads(_Base):
    """uploads.github.com, and the signed download host asset URLs redirect to."""

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        self.state.seen.append(("GET", url.path))
        parts = url.path.strip("/").split("/")
        if parts[0] != "cdn" or len(parts) != 3:
            return self._refuse("GET", "the download host serves assets only")
        if self.headers.get("Authorization"):
            return self._send(400, {"message": "Only one auth mechanism allowed"})
        return self._send(200, self.state.blobs[(int(parts[1]), parts[2])])

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        state, body = self.state, self._body()
        state.seen.append(("POST", url.path))
        prefix = f"{REPO_PATH}/releases/"
        if not (url.path.startswith(prefix) and url.path.endswith("/assets")):
            return self._refuse("POST", "the upload host takes uploads only")
        release = state.release(int(url.path[len(prefix):].split("/")[0]))
        name = parse_qs(url.query)["name"][0]
        fault = self._fault("POST", "upload")
        if release is None:
            return self._send(404, {"message": "Not Found"})
        if release["draft"] is not True:
            state.violations.append(f"uploaded {name} to published release {release['id']}")
        if fault in ("before", "reject"):
            return self._send(502 if fault == "before" else 422, {"message": "scripted"})
        if any(asset["name"] == name for asset in release["assets"]):
            return self._send(422, {"message": "already_exists"})
        asset = state.attach(release, name, body,
                             state="starter" if fault == "broken" else "uploaded")
        state.journal.append(f"upload {name}")
        return self._answer(fault, 201, state.asset_view(asset))


def _serve(handler, state: State):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.state = state
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    return server


@pytest.fixture(autouse=True)
def naps(monkeypatch):
    """The seal's read-back pauses, recorded instead of slept."""
    slept: list[float] = []
    monkeypatch.setattr(release_seal.time, "sleep", slept.append)
    return slept


@pytest.fixture
def github(monkeypatch):
    """The scripted GitHub, holding v0.9.0 published at PREVIOUS, and the real adapter pointed
    at its API host. At teardown: no DELETE or PUT was ever sent, nothing went to the wrong host,
    and no published release was written."""
    for proxy in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(proxy, raising=False)
    state = State()
    api, uploads = _serve(_Api, state), _serve(_Uploads, state)
    state.api, state.uploads = (f"http://127.0.0.1:{server.server_port}"
                                for server in (api, uploads))
    state.add(SINCE, PREVIOUS, draft=False)
    yield state, GitHubApi(state.api, "owner/repo", "t0ken")
    for server in (api, uploads):
        server.shutdown()
        server.server_close()
    assert state.violations == []
    assert not [method for method, _ in state.seen if method not in ("GET", "POST", "PATCH")]


# --- a scripted registry -------------------------------------------------------------------------

class FakeRegistry:
    """Manifests pushed by digest (each `repository@digest`), and tags naming digests."""

    def __init__(self, journal: list[str], pushed=IMAGE_REFERENCES.values(), *,
                 retag_as: str | None = None):
        self.manifests, self.tags, self.writes = set(pushed), {}, []
        self.journal, self.retag_as = journal, retag_as

    def digest(self, ref: str) -> str | None:
        if "@" in ref:
            return ref.split("@", 1)[1] if ref in self.manifests else None
        return self.tags.get(ref)

    def tag(self, tag: str, source: str) -> None:
        assert source in self.manifests, source
        self.writes.append((tag, source))
        self.journal.append(f"tag {tag}")
        self.tags[tag] = self.retag_as or source.split("@", 1)[1]


@pytest.fixture
def registry(github):
    return FakeRegistry(github[0].journal)


@pytest.fixture
def build(tmp_path):
    given = inputs(tmp_path / "inputs")
    return Build(TAG, REVISION, SINCE, given.base_bundle, given.player_deb,
                 given.bootstrapper_deb, dict(IMAGE_REFERENCES), tmp_path / "artifacts", EPOCH,
                 "owner/repo")


def _again(build: Build, name: str) -> Build:
    """The same build, sealed again (a re-run packages into a fresh runner directory)."""
    return replace(build, destination=build.destination.parent / name)


def _declared(build: Build) -> dict[str, str]:
    """name -> sha256 of every asset the build's release declares."""
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in build.destination.iterdir()}


def _for(state: State, tag: str) -> list[dict]:
    return [release for release in state.releases if release["tag_name"] == tag]


# --- the whole seal ------------------------------------------------------------------------------

def test_a_seal_publishes_the_complete_release_as_the_newest(github, registry, build):
    state, api = github
    release = seal(api, registry, build)
    [published] = _for(state, TAG)
    assert release["id"] == published["id"] and published["draft"] is False
    assert state.refs[TAG] == REVISION and state.latest == published["id"]
    assert {asset["name"]: asset["digest"] for asset in published["assets"]} == {
        name: f"sha256:{sha}" for name, sha in _declared(build).items()}
    assert {tag: registry.tags[tag] for tag in registry.tags} == {
        f"{REPOSITORY}/{name}:{TAG}": digest(name) for name in IMAGES}
    assert published["name"] == f"Photo Wall appliance {TAG}"
    for name in IMAGES:
        assert f"`{REPOSITORY}/{name}:{TAG}` = `{digest(name)}`" in published["body"]


def test_the_release_is_invisible_until_every_asset_and_image_is_ready(github, registry, build):
    """Every asset is attached to the (invisible) draft before the first visible write, the
    images' version tags; the one publish is the last write: nothing is visible half-made."""
    state, api = github
    seal(api, registry, build)
    journal = state.journal
    assert [entry.split()[0] for entry in journal] == (
        ["create"] + ["upload"] * len(_declared(build)) + ["tag"] * len(IMAGES) + ["publish"])
    assert state.writes()[-1][0] == "PATCH"
    assert [method for method, _ in state.writes()].count("PATCH") == 1


def test_the_published_manifest_carries_this_runs_image_digests(github, registry, build):
    state, api = github
    seal(api, registry, build)
    [published] = _for(state, TAG)
    manifest = json.loads(state.blobs[(published["id"], MANIFEST)])
    assert manifest["images"] == {
        name: {"repository": f"{REPOSITORY}/{name}", "digest": digest(name)} for name in IMAGES}
    assert manifest["revision"] == REVISION


# --- 1. the claim, and its compare-and-swap ----------------------------------------------------

@pytest.mark.parametrize("since, published", [
    (SINCE, "v0.10.0"),          # a newer run released v0.10.0 after this run planned
    (SINCE, "v0.9.1"),           # ... or this very version (at another commit)
    (None, None),                # the plan saw no release, but v0.9.0 is published
    ("v0.8.0", None),            # the plan diffed from an older tag than the newest
], ids=["newer-release", "same-version", "first-release-but-some", "older-since"])
def test_a_stale_plan_is_refused_before_anything_is_written(github, registry, build, since,
                                                            published):
    state, api = github
    if published:
        state.add(published, OTHER, draft=False)
    with pytest.raises(SealError, match="stale|already published|tagged at"):
        seal(api, registry, replace(build, since=since))
    assert state.writes() == [] and registry.writes == []
    assert not build.destination.exists()                      # refused before packaging


def test_the_highest_published_ignores_drafts_and_orders_by_version():
    releases = [{"tag_name": "v0.10.0", "draft": True}, {"tag_name": "v0.9.0"},
                {"tag_name": "v0.9.10", "draft": False}, {"tag_name": "v0.9.2", "draft": False},
                {"tag_name": "v1.0.0-rc.1", "draft": False}, {"tag_name": "nightly"}]
    assert highest_published(releases) == "v1.0.0-rc.1"
    assert highest_published(releases[:4]) == "v0.9.10"
    assert highest_published([{"tag_name": "v1.0.0-rc.1"}, {"tag_name": "v1.0.0"}]) == "v1.0.0"
    assert highest_published([]) is None


@pytest.mark.parametrize("setup", [
    lambda state: None,                                                  # a new version
    lambda state: state.refs.update({TAG: REVISION}),                    # tagged here
    lambda state: state.add(TAG, REVISION, draft=True),                  # a draft of it
    lambda state: state.add(TAG, OTHER, draft=True),                     # another run's draft
], ids=["untagged", "tagged-here", "own-draft", "other-draft"])
def test_the_claim_admits_a_version_that_is_this_revisions_and_unpublished(github, setup):
    state, api = github
    setup(state)
    claimed = claim(api, TAG, REVISION, SINCE)
    assert "unpublished, and follows v0.9.0" in claimed.summary and claimed.sealed is None


@pytest.mark.parametrize("target, admitted", [(REVISION, True), (OTHER, False)])
def test_the_claim_peels_an_annotated_tag_to_its_commit(github, target, admitted):
    """A tag made by `git tag -a` (here an annotated tag of an annotated tag) names a tag
    object; the claim peels it to the commit."""
    state, api = github
    state.tag_objects.update({"e" * 40: "f" * 40, "f" * 40: target})
    state.refs[TAG] = "e" * 40
    assert api.tag_commit(TAG) == target
    if admitted:
        assert claim(api, TAG, REVISION, SINCE).sealed is None
    else:
        with pytest.raises(SealError, match=f"{TAG} is tagged at {OTHER}"):
            claim(api, TAG, REVISION, SINCE)


def test_the_claim_refuses_a_tag_at_another_commit(github):
    state, api = github
    state.refs[TAG] = OTHER
    with pytest.raises(SealError, match=f"{TAG} is tagged at {OTHER}, but this run built "
                                        f"{REVISION}"):
        claim(api, TAG, REVISION, SINCE)


def test_the_claim_hands_back_a_release_this_revision_published_and_refuses_any_other(github):
    state, api = github
    published = state.add(TAG, REVISION, draft=False)
    claimed = claim(api, TAG, REVISION, SINCE)
    assert claimed.sealed["id"] == published["id"] and "already published" in claimed.summary
    del state.refs[TAG]                                  # published, but no tag names REVISION
    with pytest.raises(SealError, match=f"{TAG} is already published, but no tag names"):
        claim(api, TAG, REVISION, SINCE)
    state.refs[TAG] = OTHER
    with pytest.raises(SealError, match=f"{TAG} is tagged at {OTHER}"):
        claim(api, TAG, REVISION, SINCE)
    assert claim(api, "v0.9.2", REVISION, TAG).sealed is None


@pytest.mark.parametrize("status", ["500", "502", "403", "429", "401"])
@pytest.mark.parametrize("route", ["repo", "ref", "releases"])
def test_the_claim_fails_on_any_api_error_but_404_instead_of_reading_it_as_absent(github, status,
                                                                                  route):
    state, api = github
    state.faults[("GET", route)] = [status] * 3
    with pytest.raises(SealError, match=f"HTTP {status}"):
        claim(api, TAG, REVISION, SINCE)


def test_the_claim_fails_when_the_token_cannot_see_the_repository(github):
    """A private repository answers 404 for everything to a token that cannot read it."""
    state, api = github
    state.visible = False
    with pytest.raises(SealError, match="cannot read it"):
        claim(api, TAG, REVISION, SINCE)


def test_the_claim_fails_when_github_is_unreachable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(SealError, match="GET http://127.0.0.1"):
        claim(GitHubApi(f"http://127.0.0.1:{port}", "owner/repo", "t"), TAG, REVISION, SINCE)


def test_the_claim_needs_strict_tags_and_a_full_commit(github):
    _, api = github
    for tag, revision, since in (("0.9.1", REVISION, SINCE), ("v0.9", REVISION, SINCE),
                                 (TAG, "a" * 7, SINCE), ("v0.9.1;x", REVISION, SINCE),
                                 (TAG, REVISION, "0.9.0")):
        with pytest.raises(SealError, match="claim needs"):
            claim(api, tag, revision, since)
    with pytest.raises(SealError, match="set GH_TOKEN"):
        GitHubApi.from_env({"GITHUB_REPOSITORY": "owner/repo"})
    assert GitHubApi.from_env({"GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "t"}).url == \
        "https://api.github.com"


# --- 2, 3. packaged and verified before any write ------------------------------------------------

def test_an_unresolvable_image_digest_refuses_before_any_promote_or_publish(github, build):
    state, api = github
    registry = FakeRegistry(state.journal, pushed=[IMAGE_REFERENCES["central"]])
    with pytest.raises(SealError, match="media-worker: .* does not resolve"):
        seal(api, registry, build)
    assert registry.writes == [] and state.writes() == []


def _packaging_then(damage):
    def packaged(*args, **kwargs):
        manifest = package(*args, **kwargs)
        damage(args[3], manifest)
        return manifest
    return packaged


@pytest.mark.parametrize("damage, reason", [
    (lambda destination, manifest: (destination / manifest["bootstrapper_deb"]["filename"])
     .unlink(), "missing photo-wall-bootstrapper"),
    (lambda destination, manifest: (destination / CHECKSUMS).unlink(), "missing SHA256SUMS"),
    (lambda destination, manifest: (destination / manifest["player_deb"]["filename"])
     .write_bytes(b"truncated"), "digest_mismatch"),
    (lambda destination, manifest: (destination / "notes.md").write_text("x"),
     "undeclared notes.md"),
], ids=["missing-asset", "missing-checksums", "corrupt-asset", "undeclared-asset"])
def test_a_missing_or_wrong_declared_asset_refuses_before_any_promote_or_publish(
        github, registry, build, monkeypatch, damage, reason):
    state, api = github
    monkeypatch.setattr(release_seal, "package", _packaging_then(damage))
    with pytest.raises(SealError, match=f"not the declared one: .*{reason}"):
        seal(api, registry, build)
    assert registry.writes == [] and state.writes() == []


# --- 4. promote: a version tag never moves -------------------------------------------------------

def test_another_runs_image_tag_is_taken_only_by_the_tip_of_main(github, registry, build):
    """An unpublished version's image tags belong to no release. A seal that is not main's tip
    refuses before tagging anything; main's tip moves them to its own digests."""
    state, api = github
    other = reference("media-worker", cut="-rebuilt")
    registry.manifests.add(other)
    registry.tags[f"{REPOSITORY}/media-worker:{TAG}"] = other.split("@")[1]
    state.heads["main"] = NEWER
    with pytest.raises(SealError, match=f"is not the tip of main \\({NEWER}\\)"):
        seal(api, registry, build)
    assert registry.writes == [] and state.writes() == []
    assert f"{REPOSITORY}/central:{TAG}" not in registry.tags
    state.heads["main"] = REVISION
    seal(api, registry, _again(build, "as-tip"))
    assert registry.tags == {f"{REPOSITORY}/{name}:{TAG}": digest(name) for name in IMAGES}


def test_the_tip_refuses_when_another_draft_of_the_version_is_at_a_later_commit(github, registry,
                                                                                 build):
    """The tip of main behind another run's draft (main was rewound): the newest commit is the
    draft's, so this seal refuses to take its image tags."""
    state, api = github
    state.add(TAG, NEWER, draft=True, body=release_seal.SEAL_MARKER)
    registry.manifests.add(reference("central", cut="-newer"))
    registry.tags[f"{REPOSITORY}/central:{TAG}"] = digest("central", cut="-newer")
    with pytest.raises(SealError, match=f"is at {NEWER}, a descendant of this run's {REVISION}"):
        seal(api, registry, build)
    assert registry.writes == [] and state.writes() == []


@pytest.mark.parametrize("target", ["main", NEWER], ids=["branch", "commit"])
def test_a_draft_no_seal_made_never_blocks_a_version(github, registry, build, capsys, target):
    """A draft made by hand -- targeting a branch, or a later commit, but without the seal's
    marker -- is ignored with a warning, however the compare would come out."""
    state, api = github
    hand = state.add(TAG, target, draft=True, body="by hand")
    registry.manifests.add(reference("central", cut="-newer"))
    registry.tags[f"{REPOSITORY}/central:{TAG}"] = digest("central", cut="-newer")
    release = seal(api, registry, build)
    assert release["target_commitish"] == REVISION and hand["draft"] is True
    assert f"draft {hand['id']} of {TAG} (target {target}) was not made by a seal" in \
        capsys.readouterr().out


def test_a_promotion_that_does_not_take_refuses_before_publishing(github, build):
    state, api = github
    registry = FakeRegistry(state.journal, retag_as=digest("central", cut="-rewrapped"))
    with pytest.raises(SealError, match="after promotion"):
        seal(api, registry, build)
    assert "PATCH" not in [method for method, _ in state.writes()]


# --- 5, 6, 7. stage, publish, reconcile ----------------------------------------------------------

@pytest.mark.parametrize("fault", ["after", "drop"])
def test_an_ambiguous_publish_that_landed_is_reconciled_to_published_never_deleted(
        github, registry, build, fault):
    """GitHub published the release but the client saw a 502 or a dropped connection -- where
    `gh release create` deletes the release. The seal reads it back: published, and done."""
    state, api = github
    state.faults[("PATCH", "publish")] = [fault]
    release = seal(api, registry, build)
    [published] = _for(state, TAG)
    assert published["draft"] is False and release["id"] == published["id"]
    assert state.refs[TAG] == REVISION and state.latest == published["id"]
    assert [method for method, _ in state.seen].count("PATCH") == 1  # never re-sent once live


def test_an_ambiguous_publish_that_did_not_land_is_retried_to_published(github, registry, build):
    state, api = github
    state.faults[("PATCH", "publish")] = ["before", "before"]
    seal(api, registry, build)
    assert [release["draft"] for release in _for(state, TAG)] == [False]
    assert [method for method, _ in state.seen].count("PATCH") == 3


@pytest.mark.parametrize("faults, match", [
    (["reject"], "HTTP 422"),
    (["before"] * 3, "does not show it done after 3 attempts"),
], ids=["refused", "exhausted"])
def test_a_publish_that_fails_leaves_the_complete_draft_unpublished(github, registry, build,
                                                                    faults, match):
    state, api = github
    state.faults[("PATCH", "publish")] = list(faults)
    with pytest.raises(SealError, match=match):
        seal(api, registry, build)
    [draft] = _for(state, TAG)
    assert draft["draft"] is True and TAG not in state.refs and state.latest is None
    assert {asset["name"] for asset in draft["assets"]} == set(_declared(build))


def test_the_publish_refuses_a_draft_that_lacks_a_declared_asset(github, registry, build):
    """The last read before a release becomes visible: publish() itself refuses a draft that
    does not attach exactly the declared assets, whatever staged it."""
    state, api = github
    state.faults[("POST", "upload")] = [None, "reject"]
    with pytest.raises(SealError):
        seal(api, registry, build)
    [draft] = _for(state, TAG)
    packaged = verify(build.destination, revision=REVISION)
    with pytest.raises(SealError, match="does not attach exactly the declared assets"):
        release_seal.publish(api, registry, Draft.of(api.release(draft["id"])), packaged, "t",
                             "n")
    assert draft["draft"] is True and "PATCH" not in [method for method, _ in state.seen]


@pytest.mark.parametrize("route", ["create", "upload"])
@pytest.mark.parametrize("fault", ["after", "drop", "before"])
def test_an_ambiguous_create_or_upload_is_read_back_never_duplicated(github, registry, build,
                                                                     route, fault):
    state, api = github
    state.faults[("POST", route)] = [fault]
    seal(api, registry, build)
    [published] = _for(state, TAG)
    names = [asset["name"] for asset in published["assets"]]
    assert sorted(names) == sorted(_declared(build))            # each once
    creates = [path for method, path in state.seen
               if method == "POST" and path == f"{REPO_PATH}/releases"]
    assert len(creates) == (2 if (route, fault) == ("create", "before") else 1)


def test_a_rerun_after_a_partial_seal_converges_on_the_same_draft(github, registry, build):
    """The first attempt made the draft and attached two assets before an upload failed, so
    nothing was visible: no image was tagged. The re-run (a fresh package of the same build)
    reuses that draft, attaches only what it lacks, tags the images and publishes."""
    state, api = github
    state.faults[("POST", "upload")] = [None, None, "reject"]
    with pytest.raises(SealError, match="HTTP 422"):
        seal(api, registry, build)
    [draft] = _for(state, TAG)
    attached = {asset["name"] for asset in draft["assets"]}
    assert len(attached) == 2 and registry.writes == [] and registry.tags == {}
    state.journal.clear()
    release = seal(api, registry, _again(build, "rerun"))
    assert release["id"] == draft["id"] and _for(state, TAG) == [draft]
    assert draft["draft"] is False
    uploaded = [entry.split(" ", 1)[1] for entry in state.journal if entry.startswith("upload")]
    assert sorted(uploaded) == sorted(set(_declared(build)) - attached)
    assert not [entry for entry in state.journal if entry.startswith("create")]
    assert len(registry.writes) == len(IMAGES)


def test_a_seal_that_fails_before_promoting_leaves_no_version_tag_behind(github, registry,
                                                                       build):
    """The review's probe: the seal fails on its third upload and the next push plans another
    version. v0.9.1's image tags were never written, so nothing names an unreleased build."""
    state, api = github
    state.faults[("POST", "upload")] = [None, None, "reject"]
    with pytest.raises(SealError, match="HTTP 422"):
        seal(api, registry, build)
    state.heads["main"] = NEWER
    newer = replace(_newer(build, "newer"), tag="v0.10.0")
    registry.manifests.update(newer.images.values())
    seal(api, registry, newer)
    assert set(registry.tags) == {f"{REPOSITORY}/{name}:v0.10.0" for name in IMAGES}


def test_a_rerun_whose_images_were_rebuilt_is_refused_once_main_moved_on(github, registry,
                                                                            build):
    state, api = github
    state.faults[("PATCH", "publish")] = ["reject"]
    with pytest.raises(SealError):
        seal(api, registry, build)
    before = json.dumps(state.releases, sort_keys=True)
    writes = len(state.writes())
    rebuilt = reference("media-worker", cut="-rebuilt")
    registry.manifests.add(rebuilt)
    state.heads["main"] = NEWER
    with pytest.raises(SealError, match="not the tip of main"):
        seal(api, registry, replace(_again(build, "rerun"),
                                    images={**IMAGE_REFERENCES, "media-worker": rebuilt}))
    assert json.dumps(state.releases, sort_keys=True) == before
    assert len(state.writes()) == writes and len(registry.writes) == len(IMAGES)


def test_a_draft_holding_a_broken_asset_is_left_and_a_new_one_published(github, registry, build):
    """A half-done upload blocks its name on that draft for good; nothing deletes it. The re-run
    stages a new draft and leaves the broken one exactly as it was."""
    state, api = github
    state.faults[("POST", "upload")] = [None, "broken"]
    with pytest.raises(SealError, match="left as it is"):
        seal(api, registry, build)
    [broken] = _for(state, TAG)
    snapshot = json.dumps(broken, sort_keys=True)
    release = seal(api, registry, _again(build, "rerun"))
    assert release["id"] != broken["id"] and json.dumps(broken, sort_keys=True) == snapshot
    assert sorted(release["draft"] for release in _for(state, TAG)) == [False, True]


@pytest.mark.parametrize("attached", [False, True], ids=["empty", "with-an-asset"])
@pytest.mark.parametrize("body", ["", release_seal.SEAL_MARKER], ids=["by-hand", "by-a-seal"])
def test_a_draft_of_another_revision_is_never_used_or_touched(github, registry, build,
                                                              attached, body):
    """Another run's draft of the same version (an older attempt from another commit): the seal
    stages its own, so the tag is created at this revision."""
    state, api = github
    foreign = state.add(TAG, OTHER, draft=True, body=body)
    if attached:
        state.attach(foreign, MANIFEST, b"{}")
    snapshot = json.dumps(foreign, sort_keys=True)
    release = seal(api, registry, build)
    assert release["id"] != foreign["id"] and json.dumps(foreign, sort_keys=True) == snapshot
    assert release["target_commitish"] == REVISION and state.refs[TAG] == REVISION


def test_a_new_tag_that_reads_back_late_is_waited_for_and_one_elsewhere_refused(github, registry,
                                                                              build, naps):
    state, api = github
    release = seal(api, registry, build)
    packaged = verify(build.destination, revision=REVISION)
    naps.clear()
    del state.refs[TAG]
    with pytest.raises(SealError, match=f"its tag names None, not {REVISION}"):
        release_seal.check_published(api, release, TAG, REVISION, packaged)
    assert naps == list(release_seal.READ_BACK)
    naps.clear()
    tags = iter([None, REVISION])
    api_late = type("Late", (), {"tag_commit": lambda self, tag: next(tags)})()
    release_seal.check_published(api_late, release, TAG, REVISION, packaged)
    assert naps == [1]
    state.refs[TAG] = OTHER
    with pytest.raises(SealError, match=f"its tag names {OTHER}"):
        release_seal.check_published(api, release, TAG, REVISION, packaged)


def test_a_publish_read_back_that_lags_is_waited_for_never_patched_again(github, registry,
                                                                         build, naps):
    """The PATCH landed but its answer was lost, and the next two reads still show the draft:
    the seal waits for GitHub to catch up instead of publishing a published release again."""
    state, api = github
    state.faults[("PATCH", "publish")] = ["after"]
    state.read_lag = 2
    release = seal(api, registry, build)
    assert release["draft"] is False and state.refs[TAG] == REVISION
    assert [method for method, _ in state.seen].count("PATCH") == 1
    assert naps[:2] == [1, 2]


def test_an_upload_whose_digest_reads_back_null_is_waited_for(github, registry, build, naps):
    """GitHub may show a just-uploaded asset's digest as null for a moment (its schema allows
    it): the seal re-reads until it appears, and uploads nothing twice."""
    state, api = github
    state.digest_lag = 2
    seal(api, registry, build)
    [published] = _for(state, TAG)
    assert len(published["assets"]) == len(_declared(build))
    assert [method for method, _ in state.seen].count("POST") == 1 + len(_declared(build))
    assert naps and set(naps) <= set(release_seal.READ_BACK)


def test_a_digest_that_never_reads_back_refuses_without_publishing(github, registry, build):
    state, api = github
    state.digest_lag = 99
    with pytest.raises(SealError, match="digest=None"):
        seal(api, registry, build)
    assert [release["draft"] for release in _for(state, TAG)] == [True] and registry.tags == {}


# --- the newest commit wins an unpublished version; a published one is only checked --------------

def _newer(build: Build, name: str) -> Build:
    """The next push's build: main's new tip, its own images, the same planned version."""
    return replace(_again(build, name), revision=NEWER,
                   images={image: reference(image, cut="-newer") for image in IMAGES})


def _fail_after_promote(state: State, api: GitHubApi, registry: FakeRegistry, build: Build):
    """A seal that staged its complete draft and tagged its images, then failed to publish."""
    state.faults[("PATCH", "publish")] = ["reject"]
    with pytest.raises(SealError, match="HTTP 422"):
        seal(api, registry, build)
    [draft] = _for(state, TAG)
    assert set(registry.tags) == {f"{REPOSITORY}/{name}:{TAG}" for name in IMAGES}
    return draft


def test_a_new_push_recovers_a_seal_that_failed_after_promoting(github, registry, build):
    """The failed run left the version's image tags naming its build and a draft at its commit.
    The next push, main's tip, moves the tags to its own digests, stages and publishes its own
    draft; the failed run's draft is left in place, untouched. A re-run of the failed seal then
    refuses: the version is published at another commit."""
    state, api = github
    stale = _fail_after_promote(state, api, registry, build)
    snapshot = json.dumps(stale, sort_keys=True)
    state.heads["main"] = NEWER
    newer = _newer(build, "newer")
    registry.manifests.update(newer.images.values())
    release = seal(api, registry, newer)
    assert release["target_commitish"] == NEWER and state.refs[TAG] == NEWER
    assert registry.tags == {f"{REPOSITORY}/{name}:{TAG}": digest(name, cut="-newer")
                             for name in IMAGES}
    manifest = json.loads(state.blobs[(release["id"], MANIFEST)])
    assert {name: image["digest"] for name, image in manifest["images"].items()} == {
        name: digest(name, cut="-newer") for name in IMAGES}
    assert json.dumps(stale, sort_keys=True) == snapshot and stale["draft"] is True
    writes, tags = len(state.writes()), dict(registry.tags)
    with pytest.raises(SealError, match=f"{TAG} is tagged at {NEWER}"):
        seal(api, registry, _again(build, "old-rerun"))
    assert len(state.writes()) == writes and registry.tags == tags


def test_a_rerun_of_an_older_seal_never_takes_the_version_back(github, registry, build):
    """Both runs failed before publishing, the newer after promoting its own build. The older
    run's re-run is not main's tip, and the tags are no longer its own: it refuses and writes
    nothing. The newer run's re-run publishes."""
    state, api = github
    _fail_after_promote(state, api, registry, build)
    state.heads["main"] = NEWER
    newer = _newer(build, "newer")
    registry.manifests.update(newer.images.values())
    state.faults[("PATCH", "publish")] = ["reject"]
    with pytest.raises(SealError, match="HTTP 422"):
        seal(api, registry, newer)
    writes, tags = len(state.writes()), dict(registry.tags)
    with pytest.raises(SealError, match=f"not the tip of main \\({NEWER}\\)"):
        seal(api, registry, _again(build, "old-rerun"))
    assert len(state.writes()) == writes and registry.tags == tags
    release = seal(api, registry, _again(newer, "newer-rerun"))
    assert release["target_commitish"] == NEWER and release["draft"] is False


def test_an_older_seal_rerun_before_any_newer_seal_publishes_its_own_commit(github, registry,
                                                                            build):
    """Main moved on, but no newer run has sealed yet: the tags are still the older run's own,
    so its re-run publishes the version at its commit -- a complete release of that commit. The
    next push releases the rest, as the next version."""
    state, api = github
    _fail_after_promote(state, api, registry, build)
    state.heads["main"] = NEWER
    release = seal(api, registry, _again(build, "old-rerun"))
    assert release["target_commitish"] == REVISION and state.refs[TAG] == REVISION


def test_the_publish_refuses_when_a_version_tag_names_another_build(github, registry, build,
                                                                   monkeypatch):
    """The last read before a release becomes visible: every image tag must still name this
    run's digest. One moved after promotion refuses, and the draft stays a draft."""
    state, api = github
    promoted = release_seal.promote

    def promote_then_move(*args, **kwargs):
        promoted(*args, **kwargs)
        registry.tags[f"{REPOSITORY}/central:{TAG}"] = digest("central", cut="-elsewhere")

    monkeypatch.setattr(release_seal, "promote", promote_then_move)
    with pytest.raises(SealError, match=f"central:{TAG} names .* not this run's"):
        seal(api, registry, build)
    [draft] = _for(state, TAG)
    assert draft["draft"] is True and "PATCH" not in [method for method, _ in state.seen]


def _rebuilt(registry: FakeRegistry, build: Build) -> Build:
    """"Re-run all jobs": the images rebuilt with new digests, the base tarball repackaged."""
    images = {name: reference(name, cut="-rebuilt") for name in IMAGES}
    registry.manifests.update(images.values())
    return replace(build, images=images, source_date_epoch=EPOCH + 1)


@pytest.mark.parametrize("rerun", [lambda registry, build: build, _rebuilt],
                         ids=["seal-job-alone", "every-job"])
def test_a_rerun_after_a_successful_publish_writes_nothing_and_passes(github, registry, build,
                                                                      rerun):
    """Judged by the published release and ITS manifest, never this run's build: re-running
    every job rebuilds the images with new digests, and still nothing is written."""
    state, api = github
    first = seal(api, registry, build)
    writes, tags = len(state.writes()), dict(registry.tags)
    again = seal(api, registry, rerun(registry, _again(build, "rerun")))
    assert again["id"] == first["id"]
    assert len(state.writes()) == writes and registry.tags == tags and len(registry.writes) == 2


def _drop_asset(state: State, name: str) -> None:
    [published] = _for(state, TAG)
    published["assets"] = [asset for asset in published["assets"] if asset["name"] != name]


@pytest.mark.parametrize("change, match", [
    (lambda state, registry: registry.tags.update(
        {f"{REPOSITORY}/central:{TAG}": digest("central", cut="-elsewhere")}),
     "names .* not this run's"),
    (lambda state, registry: _drop_asset(state, CHECKSUMS), "does not attach exactly"),
    (lambda state, registry: state.blobs.update(
        {(_for(state, TAG)[0]["id"], MANIFEST): b'{"schema": 1}'}), "not a release's|exactly"),
], ids=["tag-moved", "asset-missing", "manifest-changed"])
def test_a_rerun_after_a_publish_that_is_not_intact_is_refused(github, registry, build, change,
                                                               match):
    state, api = github
    seal(api, registry, build)
    change(state, registry)
    writes = len(state.writes())
    with pytest.raises(SealError, match=match):
        seal(api, registry, _again(build, "rerun"))
    assert len(state.writes()) == writes and len(registry.writes) == 2


# --- never a DELETE, never a write to a published release ----------------------------------------

@pytest.mark.parametrize("method", ["DELETE", "PUT"])
def test_the_adapter_cannot_issue_a_delete_or_a_replace(github, method):
    state, api = github
    with pytest.raises(SealError, match=f"{method} is never issued"):
        api._call(method, f"{api.url}{REPO_PATH}/releases/1")
    assert state.seen == []


def test_the_seal_writes_only_to_a_release_github_reports_as_a_draft(github):
    state, api = github
    published = api.release(_for(state, SINCE)[0]["id"])
    with pytest.raises(SealError, match="never writes to a published release"):
        Draft.of(published)
    with pytest.raises(SealError, match="never writes to a published release"):
        Draft.of({**published, "draft": None})


# --- the registry through docker buildx imagetools -----------------------------------------------

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""`docker buildx imagetools inspect --raw REF | create --tag TAG SRC` over a JSON registry."""
import json, os, sys
state_path = os.environ["FAKE_REGISTRY"]
state = json.load(open(state_path))
args = sys.argv[1:]
assert args[:2] == ["buildx", "imagetools"], args
if state.get("fail"):
    sys.stderr.write("ERROR: " + state["fail"] + "\n"); sys.exit(1)
def resolve(ref):
    if "@" in ref:
        digest = ref.split("@", 1)[1]
        return digest if ref in state["pushed"] else None
    return state["tags"].get(ref)
if args[2] == "inspect":
    assert args[3] == "--raw", args
    digest = resolve(args[4])
    if digest is None:
        sys.stderr.write(f"ERROR: {args[4]}: not found\n"); sys.exit(1)
    sys.stdout.write(state["blobs"][digest])
elif args[2] == "create":
    assert args[3:5] == ["--prefer-index=false", "--tag"], args
    digest = resolve(args[6])
    if digest is None:
        sys.stderr.write(f"ERROR: {args[6]}: not found\n"); sys.exit(1)
    state["tags"][args[5]] = digest
    json.dump(state, open(state_path, "w"))
'''


@pytest.fixture
def docker(tmp_path, monkeypatch):
    """A fake `docker` whose registry holds both images pushed by digest; returns its state."""
    path = tmp_path / "docker"
    path.write_text(FAKE_DOCKER)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    state_path = tmp_path / "registry.json"
    state = {"pushed": list(IMAGE_REFERENCES.values()), "tags": {},
             "blobs": {digest(name): manifest_blob(name).decode() for name in IMAGES}}
    state_path.write_text(json.dumps(state))
    monkeypatch.setenv("FAKE_REGISTRY", str(state_path))

    class Handle:
        buildx = Buildx(docker=str(path))

        @staticmethod
        def read() -> dict:
            return json.loads(state_path.read_text())

        @staticmethod
        def write(**changes) -> None:
            state_path.write_text(json.dumps({**Handle.read(), **changes}))

    return Handle


def test_buildx_reads_a_digest_as_the_sha256_of_the_served_manifest(docker):
    assert docker.buildx.digest(IMAGE_REFERENCES["central"]) == digest("central")
    assert docker.buildx.digest(f"{REPOSITORY}/central:{TAG}") is None
    assert docker.buildx.digest(reference("central", cut="-unknown")) is None


def test_buildx_never_reads_a_registry_error_as_absent(docker):
    docker.write(fail="denied: permission_denied: write_package")
    with pytest.raises(SealError, match="denied"):
        docker.buildx.digest(f"{REPOSITORY}/central:{TAG}")


def test_promote_through_buildx_tags_each_digest_and_moves_one_only_for_the_tip(
        docker, github, build, tmp_path):
    state, api = github
    package(build.base_bundle, build.player_deb, build.bootstrapper_deb, tmp_path / "out",
            revision=REVISION, images=IMAGE_REFERENCES, source_date_epoch=EPOCH)
    packaged = verify(tmp_path / "out", revision=REVISION)
    promoted = {f"{REPOSITORY}/{name}:{TAG}": digest(name) for name in IMAGES}
    promote(docker.buildx, api, packaged.images, TAG, REVISION)
    assert docker.read()["tags"] == promoted
    promote(docker.buildx, api, packaged.images, TAG, REVISION)   # converges: nothing to do
    docker.write(tags={**promoted, f"{REPOSITORY}/central:{TAG}": digest("media-worker")})
    state.heads["main"] = NEWER
    with pytest.raises(SealError, match="not the tip of main"):
        promote(docker.buildx, api, packaged.images, TAG, REVISION)
    state.heads["main"] = REVISION
    promote(docker.buildx, api, packaged.images, TAG, REVISION)   # the tip overwrites it
    assert docker.read()["tags"] == promoted


# --- the command line ----------------------------------------------------------------------------

def test_the_seal_command(github, registry, build, capsys, tmp_path):
    state, api = github
    summary = tmp_path / "summary.md"
    args = ["--tag", TAG, "--revision", REVISION, "--since", SINCE,
            "--base-bundle", str(build.base_bundle),
            "--player-deb", str(build.player_deb.parent / "player-artifact"),
            "--bootstrapper-deb", str(build.bootstrapper_deb),
            "--image", f"central={IMAGE_REFERENCES['central']}",
            "--image", f"media-worker={IMAGE_REFERENCES['media-worker']}",
            "--destination", str(build.destination), "--source-date-epoch", str(EPOCH)]
    artifact = build.player_deb.parent / "player-artifact"
    artifact.mkdir()
    os.link(build.player_deb, artifact / build.player_deb.name)   # a downloaded artifact dir
    environ = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_STEP_SUMMARY": str(summary)}
    assert release_seal.main(args, github=api, registry=registry, environ=environ) == 0
    assert "publish: v0.9.1 at" in capsys.readouterr().out
    assert f"Sealed {TAG}" in summary.read_text()
    writes = len(state.writes())
    rerun = [*args[:-3], str(build.destination.parent / "rerun"), *args[-2:]]
    assert release_seal.main(rerun, github=api, registry=registry, environ=environ) == 0
    assert "nothing written" in capsys.readouterr().out and len(state.writes()) == writes
    registry.tags[f"{REPOSITORY}/central:{TAG}"] = digest("media-worker")
    again = [*args[:-3], str(build.destination.parent / "again"), *args[-2:]]
    assert release_seal.main(again, github=api, registry=registry, environ=environ) == 1
    assert "::error title=release seal::" in capsys.readouterr().out


@pytest.mark.parametrize("image", ["central", "central=ghcr.io/owner/repo/central:v1",
                                   f"central={IMAGE_REFERENCES['central']}"])
def test_the_seal_command_refuses_a_malformed_or_missing_image(github, registry, build, capsys,
                                                               image):
    _, api = github
    args = ["--tag", TAG, "--revision", REVISION, "--since", SINCE,
            "--base-bundle", str(build.base_bundle), "--player-deb", str(build.player_deb),
            "--bootstrapper-deb", str(build.bootstrapper_deb), "--image", image,
            "--destination", str(build.destination), "--source-date-epoch", str(EPOCH)]
    assert release_seal.main(args, github=api, registry=registry,
                             environ={"GITHUB_REPOSITORY": "owner/repo"}) == 1
    assert "::error title=release seal::" in capsys.readouterr().out
    assert registry.writes == []


def test_a_downloaded_artifact_must_hold_exactly_one_deb(tmp_path):
    (tmp_path / "a.deb").write_bytes(b"x")
    (tmp_path / "b.deb").write_bytes(b"y")
    with pytest.raises(SealError, match="holds 2 .deb files"):
        release_seal._one_deb(tmp_path)
    assert release_seal._one_deb(tmp_path / "a.deb") == tmp_path / "a.deb"
    (tmp_path / "b.deb").unlink()
    assert release_seal._one_deb(tmp_path) == tmp_path / "a.deb"
