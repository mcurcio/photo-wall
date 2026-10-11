"""The real Immich fixture as roadmap 4's named tests see it (run ledger .claude/runs/photo-4.md,
slice FX1 implements; F0 froze this surface).

`scripts/immich_fixture.py run --keep --setup-only --publish --state-dir DIR` starts Immich v2.5.6
(tests/integration/compose.immich.yml), uploads the content set the ledger names, and publishes
Immich on 127.0.0.1 for this test process. `PHOTO_WALL_IMMICH_FIXTURE=DIR` points the tests at it;
without it every test that takes `immich_library` skips with "set PHOTO_WALL_IMMICH_FIXTURE" (CI's
`photo-sources` job sets it). Tests isolate by what they create: a mutating test makes its own key
or uploads its own assets (tagged with its own tag), never changes the shared content set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


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
    api_key: str
    owner_ref: str
    owner_name: str
    assets: Mapping[str, FixtureAsset]
    albums: Mapping[str, str]       # album name -> album id
    people: Mapping[str, str]       # person name -> person id
    tags: Mapping[str, str]         # tag path -> tag id
    counts: Mapping[str, int]       # Immich's own statistics: photos, videos, albums, people

    def new_key(self, permissions: Sequence[str]) -> str:
        """A fresh API key of the owner with exactly these permissions (admin session)."""
        raise NotImplementedError("slice FX1")

    def other_user_key(self) -> str:
        """A key of the partner user (REQUIRED_PERMISSIONS): the other_user case."""
        raise NotImplementedError("slice FX1")

    def upload(self, label: str, *, taken: str, size: tuple[int, int] = (96, 64),
               png: bool = False, filename: str | None = None,
               tags: Sequence[str] = ()) -> FixtureAsset:
        """Upload one synthetic image as the owner and wait until Immich has read its size."""
        raise NotImplementedError("slice FX1")

    def delete(self, upstream_id: str) -> None:
        """Force-delete one asset (admin session)."""
        raise NotImplementedError("slice FX1")

    def unreachable_url(self) -> str:
        """An /api URL on a closed local port: the unreachable case without stopping Immich."""
        raise NotImplementedError("slice FX1")


def load_immich_library() -> ImmichLibrary:
    """Read PHOTO_WALL_IMMICH_FIXTURE's state; the conftest fixture `immich_library` skips with
    "set PHOTO_WALL_IMMICH_FIXTURE" when the variable is unset."""
    raise NotImplementedError("slice FX1")
