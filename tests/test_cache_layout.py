"""0013 probe-3: the retired per-domain env roots are IGNORED, not merely absent.

The unified-cache-root cutover (0013) retired ``PHOTO_WALL_MEDIA_ROOT`` /
``PHOTO_WALL_APP_ROOT`` / ``PHOTO_WALL_BASE_ROOT`` in favour of the single
``PHOTO_WALL_CACHE_ROOT`` the app derives its layout from. A grep guarantee proves
the names are no longer *read*; these tests prove the stronger positive property --
that a STALE legacy env set to a bogus path has NO effect on any resolved root.
Every derivation must key on ``PHOTO_WALL_CACHE_ROOT`` alone.
"""

from __future__ import annotations

from pathlib import Path

from central import cache_layout, netboot_base

# A path the resolvers must NEVER touch. If any resolver still read a legacy env,
# a derived root would land under here and the assertions below would fail.
_BOGUS_LEGACY = "/nonexistent/legacy"

_LEGACY_ENV = {
    "PHOTO_WALL_MEDIA_ROOT": f"{_BOGUS_LEGACY}/media",
    "PHOTO_WALL_APP_ROOT": f"{_BOGUS_LEGACY}/apps",
    "PHOTO_WALL_BASE_ROOT": f"{_BOGUS_LEGACY}/os-images",
}


def _env(cache_root: Path) -> dict:
    """A full environment: the one live cache root plus all three retired legacy
    envs pointed at a DIFFERENT bogus path."""
    return {"PHOTO_WALL_CACHE_ROOT": str(cache_root), **_LEGACY_ENV}


def test_cache_layout_ignores_retired_legacy_envs(tmp_path):
    cache_root = tmp_path / "cache"
    env = _env(cache_root)

    # Every domain root derives from PHOTO_WALL_CACHE_ROOT, never the legacy envs.
    assert cache_layout.cache_root(env) == cache_root
    assert cache_layout.media_root(env) == cache_root / "media"
    assert cache_layout.apps_root(env) == cache_root / "apps"
    assert cache_layout.os_images_root(env) == cache_root / "os-images"

    # None of the resolved roots derive from the bogus legacy path.
    for resolved in (
        cache_layout.media_root(env),
        cache_layout.apps_root(env),
        cache_layout.os_images_root(env),
    ):
        assert _BOGUS_LEGACY not in str(resolved)


def test_resolve_base_root_ignores_retired_base_root_env(tmp_path):
    cache_root = tmp_path / "cache"
    env = _env(cache_root)

    # BASE_ROOT is now <cache>/os-images, NOT the legacy PHOTO_WALL_BASE_ROOT.
    resolved = netboot_base.resolve_base_root(env)
    assert resolved == cache_root / "os-images"
    assert _BOGUS_LEGACY not in str(resolved)
