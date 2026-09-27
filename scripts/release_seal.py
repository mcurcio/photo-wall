#!/usr/bin/env python3
"""The seal: the ONE writer of a release. A release is visible only once every asset it declares
is attached and every service image it names is tagged.

`.github/workflows/pipeline.yml`'s `seal` job runs it, from the repository root, once the plan's
tests passed and the `images` job pushed the service images by digest:

  python3 -m scripts.release_seal --tag T --revision R --since S \\
      --base-bundle DIR --player-deb PATH --bootstrapper-deb PATH \\
      --image central=REF --image media-worker=REF --destination DIR

Its steps, in order. Each is idempotent, so a re-run of a failed seal converges, and a re-run of
a seal that published writes nothing and passes:

  1. claim      T is R's to write: its tag is absent or at R, and S, the last tag the plan diffed
                from, is still the highest published `v*` (compare-and-swap: a stale run's plan
                refuses, so what this seal publishes is the newest release, and `make_latest` is
                true). A T already published is refused -- unless R published it and it is
                exactly this build (its assets and its image tags), which is a no-op.
  2. package    the release assets (scripts/package_release_artifacts.py); the manifest names
                this run's image digests.
  3. verify     the packaged set is exactly the release contracts/release.py declares, and every
                image digest resolves in the registry -- before anything is written.
  4. promote    `<repository>:T` names each recorded digest, copied as is (no rebuild). While T
                is unpublished its image tags belong to no release: another run's are moved to
                this run's digests only when THE NEWEST COMMIT WINS -- R is main's tip and no
                other draft of T is at a descendant of R -- and refused otherwise.
  5. stage      this tag's DRAFT at R whose every attached asset matches a declared one (name,
                size, sha256), else a new draft; attach each declared asset it lacks. Another
                run's draft of T is left in place, untouched (nothing reads drafts).
  6. publish    re-read `<repository>:T` (each must still name this run's digest) and the draft
                (exactly the declared assets), then PATCH draft=false, make_latest=true: GitHub
                creates the tag at R in that call.
  7. reconcile  after an ambiguous write (a 5xx, a timeout, a dropped connection: the server may
                have applied it), read GitHub back and carry on toward the published state.

NEVER A DELETE, NEVER A WRITE TO A PUBLISHED RELEASE. The GitHub adapter issues GET, POST and
PATCH only (WRITES); it cannot build a DELETE. Uploads and the publish take a `Draft`, which
exists only for a release GitHub has just reported as a draft. A draft this seal cannot use (a
broken or foreign asset) is left as it is -- nothing reads drafts -- and a new one is staged.

UNSIGNED BY DESIGN (home LAN, no threat model): every sha256 is a corruption check only.

Stdlib only; runs on the runner's python3.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypeVar

from scripts.package_release_artifacts import (
    IMAGE_REFERENCE,
    Asset,
    Image,
    Packaged,
    PackagingError,
    package,
    verify,
)
from scripts.release_plan import FULL_SHA, REPO, STRICT_TAG, PlanError, git

T = TypeVar("T")

PER_PAGE: Final = 100
MAX_PAGES: Final = 20            # 2000 releases: past that the claim cannot see every one
WRITE_TRIES: Final = 3           # writes per step; each is read back before the next
TAG_READ_BACK: Final = (1, 2, 4, 8)   # seconds to wait for a new tag to read back
# The branch a release is cut from (pipeline.yml's release guard: refs/heads/main): its tip is
# the newest commit, whose seal wins an unpublished version.
RELEASE_BRANCH: Final = "main"
API_TIMEOUT: Final = 60
UPLOAD_TIMEOUT: Final = 600


class SealError(Exception):
    """The seal refuses. Before the publish step, nothing is visible: no release, no version
    tag (a promoted image tag names this release's own digest)."""


class Ambiguous(Exception):
    """A write whose outcome is unknown: the server may have applied it. It is read back, never
    assumed either way."""


# --- what the seal reads and writes ------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Draft:
    """A release GitHub last reported as a DRAFT: the only kind of release the seal writes to."""
    id: int
    tag: str
    target: str
    upload_url: str
    assets: tuple[Mapping[str, object], ...]

    @classmethod
    def of(cls, release: Mapping[str, object]) -> Draft:
        if release.get("draft") is not True:
            raise SealError(f"release {release.get('id')} ({release.get('tag_name')}) is "
                            "published: the seal never writes to a published release")
        release_id, upload_url = release.get("id"), release.get("upload_url")
        if type(release_id) is not int or not isinstance(upload_url, str):
            raise SealError(f"GitHub reported a draft without an id or upload URL: {release}")
        return cls(release_id, str(release.get("tag_name")), str(release.get("target_commitish")),
                   upload_url, tuple(release.get("assets") or ()))

    def asset(self, name: str) -> Mapping[str, object] | None:
        return next((asset for asset in self.assets if asset.get("name") == name), None)


class GitHub(Protocol):
    """The releases of one repository. Reads raise SealError when they cannot answer (only an
    absence reads as None); writes raise Ambiguous when the outcome is unknown."""

    def tag_commit(self, tag: str) -> str | None:
        """The commit `refs/tags/<tag>` names; None only when the tag does not exist."""

    def branch_head(self, branch: str) -> str:
        """The commit `refs/heads/<branch>` names."""

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether `ancestor` is `descendant` or one of its ancestors."""

    def releases(self) -> list[dict]:
        """Every release, drafts included."""

    def release(self, release_id: int) -> dict | None:
        """One release; None only when it does not exist."""

    def create_draft(self, tag: str, revision: str, title: str, notes: str) -> dict:
        """A new draft for `tag` at `revision`."""

    def upload(self, draft: Draft, asset: Asset) -> dict:
        """Attach `asset` to `draft`; the attached asset as GitHub records it."""

    def publish(self, draft: Draft, title: str, notes: str) -> dict:
        """Publish `draft` as the latest release (GitHub creates its tag at its target); the
        release as GitHub records it."""


class Registry(Protocol):
    """The OCI registry holding the service images."""

    def digest(self, reference: str) -> str | None:
        """The manifest digest `reference` names; None only when it names nothing."""

    def tag(self, tag: str, source: str) -> None:
        """Point `tag` at `source`'s manifest, copied as is."""


# --- the steps ---------------------------------------------------------------------------------

def _precedence(tag: str) -> tuple[int, int, int, bool, str]:
    """A strict `v*` tag's order (a pre-release, by its label, below its release)."""
    core, _, pre = tag[1:].partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    return major, minor, patch, not pre, pre


def highest_published(releases: Iterable[Mapping[str, object]]) -> str | None:
    """The highest strict `v*` tag a published release holds (a release not plainly a draft
    counts as published)."""
    return max((str(release["tag_name"]) for release in releases
                if release.get("draft") is not True
                and STRICT_TAG.match(str(release.get("tag_name")))),
               key=_precedence, default=None)


@dataclass(frozen=True, slots=True)
class Claim:
    """What the claim found: `sealed` is the release when this very revision already published
    the version (a re-run after success), which the seal then only checks."""
    summary: str
    sealed: dict | None = None


def claim(github: GitHub, tag: str, revision: str, since: str | None) -> Claim:
    """Refuse unless this seal may write `tag` from `revision`: the tag is absent or names
    `revision` (a tag never moves), and `since` is still the highest published `v*` (compare-and-
    swap: otherwise a newer run released after this run planned, and this one is stale). A
    published `tag` is never rewritten: the claim returns it as `sealed` when `revision` published
    it, and refuses it otherwise."""
    if (not STRICT_TAG.match(tag) or not FULL_SHA.match(revision)
            or not (since is None or STRICT_TAG.match(since))):
        raise SealError(f"claim needs a strict release tag, a full commit and a strict previous "
                        f"tag or none, got {tag!r}, {revision!r} and {since!r}")
    at = github.tag_commit(tag)
    if at is not None and at != revision:
        raise SealError(f"{tag} is tagged at {at}, but this run built {revision}: refusing to "
                        "write another commit's release (this run's plan is stale, or the tag "
                        "was made outside the pipeline)")
    releases = github.releases()
    published = [release for release in releases
                 if release.get("tag_name") == tag and release.get("draft") is not True]
    if published:
        if at != revision:
            raise SealError(f"{tag} is already published, but no tag names this run's "
                            f"{revision}: a published release is never rewritten")
        return Claim(f"{tag} is already published at this revision: checking it is this build",
                     published[0])
    highest = highest_published(releases)
    if highest != since:
        raise SealError(f"this run's plan releases {tag} after {since or 'no release'}, but the "
                        f"highest published release is now {highest or 'none'}: the plan is "
                        "stale (a newer run released since it was made), and publishing it would "
                        "mark an older version latest. The next push to main releases whatever "
                        "is still unreleased.")
    return Claim(f"{tag} is {'tagged at this revision' if at else 'untagged'}, unpublished, "
                 f"and follows {since or 'no release'}, the highest published")


def verified(directory: Path, revision: str, registry: Registry) -> Packaged:
    """The packaged release, once it is exactly the declared one and every image it names
    resolves in the registry."""
    try:
        packaged = verify(directory, revision=revision)
    except PackagingError as error:
        raise SealError(f"the packaged release is not the declared one: {error}") from None
    for image in packaged.images:
        found = registry.digest(image.reference)
        if found != image.digest:
            raise SealError(f"{image.name}: {image.reference} does not resolve in the registry "
                            f"(found {found or 'nothing'}): the release would name an image "
                            "nobody can pull")
    return packaged


def require_newest(github: GitHub, tag: str, revision: str) -> None:
    """THE NEWEST COMMIT WINS an unpublished version: this run may take `tag`'s image tags from
    another run only when `revision` is the release branch's tip and no other draft of `tag` is
    at a commit `revision` precedes. So the next push recovers a seal that failed after it
    promoted, and a re-run of an older seal (no longer the tip) refuses."""
    head = github.branch_head(RELEASE_BRANCH)
    if head != revision:
        raise SealError(f"{tag}'s image tags name another run's build, and this run's {revision} "
                        f"is not the tip of {RELEASE_BRANCH} ({head}): the newest commit wins an "
                        "unpublished version, so this older run refuses. The next push to "
                        f"{RELEASE_BRANCH} releases it.")
    for release in github.releases():
        other = release.get("target_commitish")
        if (release.get("draft") is True and release.get("tag_name") == tag
                and isinstance(other, str) and other != revision
                and github.is_ancestor(revision, other)):
            raise SealError(f"draft {release.get('id')} of {tag} is at {other}, a descendant of "
                            f"this run's {revision}: the newest commit wins, so this run refuses")


def promote(registry: Registry, github: GitHub, images: Sequence[Image], tag: str,
            revision: str) -> None:
    """Tag each image `<repository>:<tag>` at its recorded digest. The version is unpublished
    (the claim holds it so), so its image tags belong to no release yet: tags another run left
    naming other digests are moved to this run's only when this run is the newest
    (`require_newest`), which is decided before any tag is written."""
    current = {image: registry.digest(f"{image.repository}:{tag}") for image in images}
    if any(digest is not None and digest != image.digest for image, digest in current.items()):
        require_newest(github, tag, revision)
    for image, digest in current.items():
        if digest != image.digest:
            registry.tag(f"{image.repository}:{tag}", image.reference)
        found = registry.digest(f"{image.repository}:{tag}")
        if found != image.digest:
            raise SealError(f"{image.repository}:{tag} names {found or 'nothing'} after "
                            f"promotion, not {image.digest}")


def _converge(observe: Callable[[], T | None], write: Callable[[], T | None], what: str) -> T:
    """Write until it is done: `observe` reads first, `write` returns the result when GitHub's
    answer shows it done, and after an ambiguous write `observe` reads GitHub back -- never
    assuming either way. At most WRITE_TRIES writes; a definite refusal (SealError) is final."""
    for attempt in range(WRITE_TRIES + 1):
        if (done := observe()) is not None:
            return done
        if attempt == WRITE_TRIES:
            break
        try:
            if (done := write()) is not None:
                return done
        except Ambiguous as error:
            print(f"::warning title=release seal::{what}: {error}; reading GitHub back")
    raise SealError(f"{what}: GitHub does not show it done after {WRITE_TRIES} attempts")


def _matches(attached: Mapping[str, object], asset: Asset) -> bool:
    return (attached.get("state") == "uploaded" and attached.get("size") == asset.size
            and attached.get("digest") == f"sha256:{asset.sha256}")


def _usable(release: Mapping[str, object], tag: str, revision: str,
            wanted: Mapping[str, Asset]) -> bool:
    """A draft of `tag` at `revision` whose every attached asset is a declared one, intact."""
    return (release.get("draft") is True and release.get("tag_name") == tag
            and release.get("target_commitish") == revision
            and all(isinstance(attached, dict) and attached.get("name") in wanted
                    and _matches(attached, wanted[str(attached["name"])])
                    for attached in release.get("assets") or ()))


def _require_exactly(release: Mapping[str, object], packaged: Packaged) -> None:
    """`release` attaches exactly the declared assets, each intact."""
    wanted = {asset.name: asset for asset in packaged.assets}
    attached = {str(asset.get("name")): asset for asset in release.get("assets") or ()}
    if set(attached) != set(wanted) or not all(_matches(attached[name], asset)
                                               for name, asset in wanted.items()):
        raise SealError(f"release {release.get('id')} does not attach exactly the declared "
                        f"assets intact: {sorted(attached)} against {sorted(wanted)}")


def stage(github: GitHub, tag: str, revision: str, packaged: Packaged, title: str,
          notes: str) -> Draft:
    """This tag's usable draft at `revision` (the oldest, if a lost create left two), or a new
    one, with every declared asset attached."""
    wanted = {asset.name: asset for asset in packaged.assets}

    def usable() -> Draft | None:
        found = sorted((release for release in github.releases()
                        if _usable(release, tag, revision, wanted)),
                       key=lambda release: release["id"])
        return Draft.of(found[0]) if found else None

    draft = _converge(usable, lambda: Draft.of(github.create_draft(tag, revision, title, notes)),
                      f"create the {tag} draft")
    for asset in packaged.assets:
        draft = _attach(github, draft, asset)
    return draft


def _read(github: GitHub, release_id: int) -> dict:
    release = github.release(release_id)
    if release is None:
        raise SealError(f"release {release_id} no longer exists")
    return release


def _attach(github: GitHub, draft: Draft, asset: Asset) -> Draft:
    observed = [draft]

    def attached() -> Draft | None:
        observed[:] = [current := Draft.of(_read(github, draft.id))]
        found = current.asset(asset.name)
        if found is None:
            return None
        if not _matches(found, asset):
            raise SealError(f"draft {draft.id} holds {asset.name} as state={found.get('state')} "
                            f"size={found.get('size')} digest={found.get('digest')}, not "
                            f"sha256:{asset.sha256} ({asset.size} bytes): it is left as it is, "
                            "and a re-run stages a new draft")
        return current

    def upload() -> Draft | None:
        return observed[0] if _matches(github.upload(observed[0], asset), asset) else None

    return _converge(attached, upload, f"attach {asset.name} to draft {draft.id}")


def require_promoted(registry: Registry, images: Sequence[Image], tag: str) -> None:
    """Each `<repository>:<tag>` names this run's digest: a release is never published, or
    passed as sealed, while its version tag names another build."""
    for image in images:
        found = registry.digest(f"{image.repository}:{tag}")
        if found != image.digest:
            raise SealError(f"{image.repository}:{tag} names {found or 'nothing'}, not this "
                            f"run's {image.digest}: refusing a release whose version tag names "
                            "another build")


def publish(github: GitHub, registry: Registry, draft: Draft, packaged: Packaged, title: str,
            notes: str) -> dict:
    """Publish `draft`, reconciling any ambiguous PATCH: the release ends published, or the seal
    fails with it still a draft. The last reads before it becomes visible are its image tags and
    its assets. A published release is only ever read."""
    observed = [draft]

    def published() -> dict | None:
        current = _read(github, draft.id)
        if current.get("draft") is not True:
            return current
        require_promoted(registry, packaged.images, draft.tag)
        _require_exactly(current, packaged)
        observed[:] = [Draft.of(current)]
        return None

    def patch() -> dict | None:
        release = github.publish(observed[0], title, notes)
        return release if release.get("draft") is False else None

    return _converge(published, patch, f"publish {draft.tag}")


def check_published(github: GitHub, release: Mapping[str, object], tag: str, revision: str,
                    packaged: Packaged, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """The published release is `tag` at `revision` with exactly the declared assets. The tag
    publishing created may take a moment to read back; a tag at another commit never passes."""
    if release.get("tag_name") != tag:
        raise SealError(f"release {release.get('id')} was published as "
                        f"{release.get('tag_name')}, not {tag}")
    at = github.tag_commit(tag)
    for delay in TAG_READ_BACK:
        if at is not None:
            break
        sleep(delay)
        at = github.tag_commit(tag)
    if at != revision:
        raise SealError(f"{tag} was published, but its tag names {at}, not {revision}")
    _require_exactly(release, packaged)


# --- the release's words -----------------------------------------------------------------------

def title(tag: str) -> str:
    return f"Photo Wall appliance {tag}"


def notes(packaged: Packaged, tag: str, revision: str, repository: str, server: str) -> str:
    """The release notes, from the packaged manifest."""
    manifest = packaged.manifest
    blob = f"{server}/{repository}/blob/{tag}"
    images = "\n".join(f"- `{image.repository}:{tag}` = `{image.digest}`"
                       for image in packaged.images)
    return f"""\
Photo Wall 0009 release assets for `{revision}`: the minimal base OS
image, the Player application and the Central service images, built for this
exact commit.

**Unsigned, by design.** Per the project's home-LAN, no-threat-model
ruling ([0009]({blob}/docs/decisions/0009-minimal-base-and-app-package.md)),
nothing here is signed. Every sha256 below (and in the attached
`SHA256SUMS`/`manifest.json`) is a **corruption check only** -- it
proves the download was not truncated or mangled in transit, never
who published it.

## Base OS image (rarely changes)

Unpack `{manifest['base_image']['filename']}` (sha256
`{manifest['base_image']['sha256']}`) and stage its `photo-wall-base/boot/`
beneath your TFTP boot-server tree -- see the
[runbook]({blob}/docs/runbook.md#player-provisioning-stage-the-netboot-bundle-and-read-its-console-0014).
Every diskless Player netboots this image and fetches the current app at
boot; it carries no application code itself.

## Player application (revs independently of the base)

Central pulls `{manifest['player_deb']['filename']}` (sha256
`{manifest['player_deb']['sha256']}`) from this GitHub release
itself; there is nothing to copy or register by hand. It discovers
the release on its next release sync (every 15 minutes), or at once
after `POST /v1/operator/app/releases/refresh`. A fleet with no
bound Players follows the newest release automatically; otherwise
promote it (`Authorization: Bearer <PHOTO_WALL_ADMIN_TOKEN>`):

       curl -X POST http://<central>/v1/operator/app/releases/{tag}/promote \\
         -H 'Authorization: Bearer <admin-token>'

Every Player fetches the newly promoted `.deb` on its next reboot;
running Players are unaffected until then.

## Service images

{images}

See the [setup and recovery runbook]({blob}/docs/runbook.md) for
the full operator procedure.
"""


# --- the seal ----------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Build:
    """What one run built and plans to release."""
    tag: str
    revision: str
    since: str | None
    base_bundle: Path
    player_deb: Path
    bootstrapper_deb: Path
    images: Mapping[str, str]            # image name -> `<repository>@<digest>`
    destination: Path
    source_date_epoch: int
    repository: str                      # owner/name, for the notes' links
    server: str = "https://github.com"


def seal(github: GitHub, registry: Registry, build: Build) -> dict:
    """Claim, package, verify, promote, stage and publish `build`; returns the published
    release. Nothing is written before the verify step passes, nor at all when this build is
    already the published release."""
    claimed = claim(github, build.tag, build.revision, build.since)
    print(f"claim: {claimed.summary}")
    try:
        package(build.base_bundle, build.player_deb, build.bootstrapper_deb, build.destination,
                revision=build.revision, images=build.images,
                source_date_epoch=build.source_date_epoch)
    except PackagingError as error:
        raise SealError(f"packaging failed: {error}") from None
    packaged = verified(build.destination, build.revision, registry)
    print(f"verify: {len(packaged.assets)} assets and {len(packaged.images)} images, as declared")
    if claimed.sealed is not None:
        check_published(github, claimed.sealed, build.tag, build.revision, packaged)
        require_promoted(registry, packaged.images, build.tag)
        print(f"seal: {build.tag} is already this build, published: nothing written")
        return claimed.sealed
    promote(registry, github, packaged.images, build.tag, build.revision)
    print(f"promote: {', '.join(f'{image.repository}:{build.tag}' for image in packaged.images)}")
    heading = title(build.tag)
    body = notes(packaged, build.tag, build.revision, build.repository, build.server)
    draft = stage(github, build.tag, build.revision, packaged, heading, body)
    print(f"stage: draft {draft.id} attaches every declared asset")
    release = publish(github, registry, draft, packaged, heading, body)
    check_published(github, release, build.tag, build.revision, packaged)
    print(f"publish: {build.tag} at {build.revision}: {release.get('html_url')}")
    return release


# --- adapters ----------------------------------------------------------------------------------

# The only writes the adapter can issue. There is no DELETE (and no PUT): a release, an asset or
# a tag is never removed or replaced.
WRITES: Final = frozenset({"POST", "PATCH"})


@dataclass(frozen=True, slots=True)
class GitHubApi:
    """GitHub's REST API for one repository. A read's HTTP 404 is an absence; any other read
    failure raises SealError, so an outage never reads as "absent". A write's 5xx, timeout or
    dropped connection raises Ambiguous; its other failures raise SealError."""
    url: str
    repository: str
    token: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> GitHubApi:
        token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN") or ""
        repository = environ.get("GITHUB_REPOSITORY") or ""
        if not token or not repository:
            raise SealError("the seal reads and writes GitHub: set GH_TOKEN (the job's token) "
                            "and GITHUB_REPOSITORY (the runner sets it)")
        return cls(environ.get("GITHUB_API_URL") or "https://api.github.com", repository, token)

    def _call(self, method: str, url: str, *, payload: object = None,
              upload: Asset | None = None) -> object:
        if method != "GET" and method not in WRITES:
            raise SealError(f"{method} is never issued: the seal removes and replaces nothing")
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": f"Bearer {self.token}",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "photo-wall-release-seal"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        handle = None
        if upload is not None:
            handle = open(upload.path, "rb")                       # noqa: SIM115 (closed below)
            data = handle
            headers["Content-Type"] = "application/octet-stream"
            headers["Content-Length"] = str(upload.size)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT if upload
                                        else API_TIMEOUT) as response:
                text = response.read()
        except urllib.error.HTTPError as error:
            if method == "GET" and error.code == 404:
                return None
            detail = error.read(1000).decode(errors="replace").strip()
            if method in WRITES and error.code >= 500:
                raise Ambiguous(f"{method} {url}: HTTP {error.code}") from None
            raise SealError(f"{method} {url}: HTTP {error.code} {error.reason} {detail}") from None
        except (OSError, http.client.HTTPException) as error:
            if method in WRITES:
                raise Ambiguous(f"{method} {url}: {error}") from None
            raise SealError(f"{method} {url}: {error}") from None
        finally:
            if handle is not None:
                handle.close()
        try:
            return json.loads(text) if text else None
        except ValueError:
            if method in WRITES:
                raise Ambiguous(f"{method} {url}: an unreadable response") from None
            raise SealError(f"{method} {url}: an unreadable response") from None

    def _api(self, path: str) -> str:
        return f"{self.url}/repos/{self.repository}{path}"

    def _get(self, path: str) -> object:
        return self._call("GET", self._api(path))

    def _require_repository(self) -> None:
        # A token that cannot see a private repository gets 404 for everything in it; that must
        # never read as "no tag" or "no release".
        if self._get("") is None:
            raise SealError(f"GitHub answers 404 for {self.repository} itself: this token "
                            "cannot read it, so no absence it reports can be trusted")

    def _ref_commit(self, ref_name: str) -> str | None:
        self._require_repository()
        # The single-ref endpoint: exactly this ref or 404 (git/matching-refs prefix-matches).
        ref = self._get(f"/git/ref/{ref_name}")
        if ref is None:
            return None
        target = ref.get("object") if isinstance(ref, dict) else None
        while isinstance(target, dict) and target.get("type") == "tag":   # annotated: peel it
            peeled = self._get(f"/git/tags/{target.get('sha')}")
            target = peeled.get("object") if isinstance(peeled, dict) else None
        if (not isinstance(target, dict) or target.get("type") != "commit"
                or not FULL_SHA.match(str(target.get("sha")))):
            raise SealError(f"refs/{ref_name} names no commit: {ref}")
        return str(target["sha"])

    def tag_commit(self, tag: str) -> str | None:
        return self._ref_commit(f"tags/{tag}")

    def branch_head(self, branch: str) -> str:
        head = self._ref_commit(f"heads/{branch}")
        if head is None:
            raise SealError(f"{self.repository} has no branch {branch}")
        return head

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        compared = self._get(f"/compare/{ancestor}...{descendant}")
        status = compared.get("status") if isinstance(compared, dict) else None
        if status not in ("ahead", "behind", "identical", "diverged"):
            raise SealError(f"comparing {ancestor}...{descendant}: {compared}")
        return status in ("ahead", "identical")

    def releases(self) -> list[dict]:
        self._require_repository()
        found: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._get(f"/releases?per_page={PER_PAGE}&page={page}")
            if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
                raise SealError(f"the release list (page {page}) is not a list of releases")
            found += batch
            if len(batch) < PER_PAGE:
                return found
        raise SealError(f"more than {PER_PAGE * MAX_PAGES} releases: the claim cannot see "
                        "every one")

    def release(self, release_id: int) -> dict | None:
        release = self._get(f"/releases/{release_id}")
        if release is not None and not isinstance(release, dict):
            raise SealError(f"release {release_id} is not a release: {release}")
        return release

    def _written(self, method: str, url: str, **body: object) -> dict:
        """A write's answer: the object GitHub wrote. Anything else leaves the outcome unknown."""
        written = self._call(method, url, **body)
        if not isinstance(written, dict):
            raise Ambiguous(f"{method} {url} answered no object")
        return written

    def create_draft(self, tag: str, revision: str, title: str, notes: str) -> dict:
        return self._written("POST", self._api("/releases"), payload={
            "tag_name": tag, "target_commitish": revision, "name": title, "body": notes,
            "draft": True, "prerelease": False})

    def upload(self, draft: Draft, asset: Asset) -> dict:
        base = draft.upload_url.split("{", 1)[0]
        return self._written("POST", f"{base}?{urllib.parse.urlencode({'name': asset.name})}",
                             upload=asset)

    def publish(self, draft: Draft, title: str, notes: str) -> dict:
        # make_latest is a string enum in the REST API ("true", "false", "legacy").
        return self._written("PATCH", self._api(f"/releases/{draft.id}"), payload={
            "draft": False, "make_latest": "true", "name": title, "body": notes})


@dataclass(frozen=True, slots=True)
class Buildx:
    """The registry through `docker buildx imagetools`, logged in by the job. A digest is the
    sha256 of the manifest bytes the registry serves (`inspect --raw`)."""
    docker: str = "docker"

    def _imagetools(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run([self.docker, "buildx", "imagetools", *args],
                                  capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SealError(f"docker buildx imagetools {' '.join(args)}: {error}") from None

    def digest(self, reference: str) -> str | None:
        result = self._imagetools("inspect", "--raw", reference)
        if result.returncode != 0:
            error = result.stderr.decode(errors="replace")
            # buildx reports a missing manifest as "<reference>: not found"; anything else (a
            # denial, a network error) must never read as absent.
            if f"{reference}: not found" in error:
                return None
            raise SealError(f"docker buildx imagetools inspect {reference}: {error.strip()}")
        if not result.stdout:
            raise SealError(f"docker buildx imagetools inspect {reference}: no manifest")
        return f"sha256:{hashlib.sha256(result.stdout).hexdigest()}"

    def tag(self, tag: str, source: str) -> None:
        # One source and no annotations: a carbon copy of its manifest, index or not, so the tag
        # names the source's own digest (promote reads it back).
        result = self._imagetools("create", "--prefer-index=false", "--tag", tag, source)
        if result.returncode != 0:
            raise SealError(f"docker buildx imagetools create --tag {tag} {source}: "
                            f"{result.stderr.decode(errors='replace').strip()}")


# --- the command line --------------------------------------------------------------------------

def _one_deb(path: Path) -> Path:
    """`path` itself, or the one `.deb` in the directory `path` (a downloaded artifact)."""
    if not path.is_dir():
        return path
    debs = sorted(path.glob("*.deb"))
    if len(debs) != 1:
        raise SealError(f"{path} holds {len(debs)} .deb files, not one")
    return debs[0]


def _image(value: str) -> tuple[str, str]:
    name, separator, reference = value.partition("=")
    if not separator or not IMAGE_REFERENCE.fullmatch(reference):
        raise SealError(f"--image takes NAME=<repository>@sha256:<digest>, got {value!r}")
    return name, reference


def main(argv: Sequence[str] | None = None, *, github: GitHub | None = None,
         registry: Registry | None = None, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--since", required=True, help="the plan's last tag; empty for none")
    parser.add_argument("--base-bundle", type=Path, required=True)
    parser.add_argument("--player-deb", type=Path, required=True)
    parser.add_argument("--bootstrapper-deb", type=Path, required=True)
    parser.add_argument("--image", action="append", default=[], help="NAME=REPOSITORY@DIGEST")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int,
                        help="the tarball's member time; default: the revision's commit time")
    args = parser.parse_args(argv)
    try:
        images = dict(_image(value) for value in args.image)
        if len(images) != len(args.image):
            raise SealError("--image names an image twice")
        epoch = args.source_date_epoch
        if epoch is None:
            epoch = int(git(REPO, "show", "-s", "--format=%ct", args.revision).strip())
        build = Build(args.tag, args.revision, args.since or None, args.base_bundle,
                      _one_deb(args.player_deb), _one_deb(args.bootstrapper_deb), images,
                      args.destination, epoch, environ.get("GITHUB_REPOSITORY", ""),
                      environ.get("GITHUB_SERVER_URL") or "https://github.com")
        release = seal(github or GitHubApi.from_env(environ), registry or Buildx(), build)
    except (SealError, PlanError) as error:
        for line in str(error).splitlines():
            print(f"::error title=release seal::{line}")
        return 1
    if path := environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as handle:
            handle.write(f"### Release\n\nSealed {build.tag} at `{build.revision}`: "
                         f"{release.get('html_url')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
