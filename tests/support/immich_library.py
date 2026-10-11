"""The real Immich fixture as roadmap 4's named tests see it (run ledger .claude/runs/photo-4.md,
slice FX1).

`scripts/immich_fixture.py run --keep --setup-only --publish --state-dir DIR` starts Immich v2.5.6
(tests/integration/compose.immich.yml), uploads the content set (scripts/immich_actions.py
`CONTENT`), and publishes Immich on 127.0.0.1 for this test process. `PHOTO_WALL_IMMICH_FIXTURE=DIR`
points the tests at it; without it every test that takes `immich_library` skips with "set
PHOTO_WALL_IMMICH_FIXTURE" (CI's `photo-sources` job sets it). Tests isolate by what they create:
a mutating test makes its own key or uploads its own assets (tagged with its own tag), never
changes the shared content set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scripts.immich_actions import (
    LIBRARY_SCHEMA,
    METADATA_TIMEOUT,
    UpstreamFixture,
    synthetic_image,
)

FIXTURE_VARIABLE = "PHOTO_WALL_IMMICH_FIXTURE"
# One event loop for every Immich call a test process makes: an `ImmichClient` keeps its
# connections open between calls, and a connection belongs to the loop that opened it, so a
# client used across `asyncio.run` calls meets a closed loop ("Event loop is closed").
_LOOP = asyncio.Runner()


def run(coroutine):
    """Run `coroutine` on the test process's one loop (see `_LOOP`)."""
    return _LOOP.run(coroutine)
SKIP_REASON = "set PHOTO_WALL_IMMICH_FIXTURE"


@dataclass(frozen=True)
class FixtureAsset:
    """One uploaded asset of the content set, as the fixture made it."""

    label: str
    upstream_id: str
    kind: str              # "image" or "video"
    taken: str             # ISO 8601 UTC, as uploaded
    width: int             # upright, after any edit
    height: int
    sha1: str              # of the uploaded original


@dataclass(frozen=True)
class ImmichLibrary:
    """The published fixture. `api_key` holds exactly policy.REQUIRED_PERMISSIONS."""

    base_url: str          # "http://127.0.0.1:<port>/api"
    api_key: str = field(repr=False)
    owner_ref: str
    owner_name: str
    assets: Mapping[str, FixtureAsset]
    albums: Mapping[str, str]       # album name -> album id
    people: Mapping[str, str]       # person name -> person id
    tags: Mapping[str, str]         # tag path -> tag id
    # The counts the owner should see: photos and videos (Immich's statistics), albums (their
    # own) and people (named, not hidden), the last two from the content set.
    counts: Mapping[str, int]
    admin_token: str = field(default="", repr=False)        # the owner's (admin) session
    partner_api_key: str = field(default="", repr=False)    # the partner's REQUIRED_PERMISSIONS key
    shared_albums: Mapping[str, str] = field(default_factory=dict)  # the partner's, shared
    hidden_people: Mapping[str, str] = field(default_factory=dict)  # the owner hid them

    def _admin(self) -> UpstreamFixture:
        return UpstreamFixture(self.base_url, token=self.admin_token)

    def new_key(self, permissions: Sequence[str]) -> str:
        """A fresh API key of the owner with exactly these permissions (admin session)."""
        admin = self._admin()
        try:
            key = admin.request("POST", "/api-keys", (201,), json={
                "name": "photo-wall-test-" + uuid.uuid4().hex[:12],
                "permissions": list(permissions)})
        finally:
            admin.close()
        return key["secret"]

    def other_user_key(self) -> str:
        """A key of the partner user (REQUIRED_PERMISSIONS): the other_user case."""
        return self.partner_api_key

    def upload(self, label: str, *, taken: str, size: tuple[int, int] = (96, 64),
               png: bool = False, filename: str | None = None,
               tags: Sequence[str] = ()) -> FixtureAsset:
        """Upload one synthetic image as the owner and wait until Immich has read its size.

        `label` should be unique to the test (it makes the bytes unique); `tags` are tag paths,
        made when missing."""
        data = synthetic_image(label, size, taken, png=png, camera=not png)
        name = filename or label + (".png" if png else ".jpg")
        admin = self._admin()
        try:
            upstream_id = admin.upload_bytes(name, data, "image/png" if png else "image/jpeg",
                                             taken)
            if tags:
                admin.tag(list(tags), [upstream_id])
            admin.wait_until_read(upstream_id, taken,
                                  deadline=time.monotonic() + METADATA_TIMEOUT)
        finally:
            admin.close()
        return FixtureAsset(label=label, upstream_id=upstream_id, kind="image", taken=taken,
                            width=size[0], height=size[1], sha1=hashlib.sha1(data).hexdigest())

    def delete(self, upstream_id: str) -> None:
        """Force-delete one asset (admin session)."""
        admin = self._admin()
        try:
            admin.request("DELETE", "/assets", (204,), json={"ids": [upstream_id], "force": True})
        finally:
            admin.close()

    def unreachable_url(self) -> str:
        """An /api URL on a closed local port: the unreachable case without stopping Immich."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        return f"http://127.0.0.1:{port}/api"


def _read(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def library_from_state(state: Path) -> ImmichLibrary:
    """The published fixture recorded in a `run --publish` state directory."""
    published = _read(state / "published.json")
    library = _read(state / "runtime/library.json")
    admin = _read(state / "runtime/admin.json")
    if library.get("schema") != LIBRARY_SCHEMA:
        raise ValueError("runtime/library.json has an unknown schema")
    assets = {label: FixtureAsset(label=label, upstream_id=item["upstream_id"],
                                  kind=item["kind"], taken=item["taken"], width=item["width"],
                                  height=item["height"], sha1=item["sha1"])
              for label, item in library["assets"].items()}
    return ImmichLibrary(
        base_url=published["base_url"], api_key=library["api_key"],
        owner_ref=library["owner"]["ref"], owner_name=library["owner"]["name"],
        assets=assets, albums=dict(library["albums"]), people=dict(library["people"]),
        tags=dict(library["tags"]), counts=dict(library["counts"]),
        admin_token=admin["token"], partner_api_key=library["partner_api_key"],
        shared_albums=dict(library["shared_albums"]), hidden_people=dict(library["hidden_people"]))


def load_immich_library() -> ImmichLibrary:
    """Read PHOTO_WALL_IMMICH_FIXTURE's state; the conftest fixture `immich_library` skips with
    "set PHOTO_WALL_IMMICH_FIXTURE" when the variable is unset."""
    state = os.environ.get(FIXTURE_VARIABLE)
    if not state:
        raise LookupError(SKIP_REASON)
    return library_from_state(Path(state))
