"""The single cache-root resolver (0013 -- one cache the app owns, k8s places).

The app owns the layout: it reads ONE optional env, ``PHOTO_WALL_CACHE_ROOT``
(baked default ``/var/cache/photo-wall``), and derives the three domain
subdirectories -- ``media/``, ``apps/``, ``os-images/`` -- as INTERNAL CONSTANTS.
It never reads a per-domain path env. Relocating one domain onto another medium
is a Kubernetes ``subPath`` mount at the fixed in-container subdir path; the app
never sees it.

Home is ``central`` deliberately: ``media`` already depends on ``central``
(``media/worker.py`` imports ``central.netboot_base`` et al.), while ``contracts``
and ``player`` are import-linter-forbidden from importing either -- so this is
the one module BOTH the central serve side and the media worker can import
without adding a new import-linter edge. It depends on nothing but the stdlib,
so it introduces no cycle.
"""

from __future__ import annotations

import os
from pathlib import Path

# The baked default: the one place the FHS home for the cache is named.
DEFAULT_CACHE_ROOT = "/var/cache/photo-wall"

# In-container domain subdirectory names (0013 frozen contract). Derived from the
# cache root as constants -- NEVER separate env inputs.
MEDIA_SUBDIR = "media"
APPS_SUBDIR = "apps"
OS_IMAGES_SUBDIR = "os-images"


def cache_root(env: dict | None = None) -> Path:
    """The one cache root: ``PHOTO_WALL_CACHE_ROOT`` or the baked default.

    Optional-with-default, so this always returns a path -- release sourcing and
    base serving are unconditional (always-on), never gated on env presence.
    """
    env = os.environ if env is None else env
    value = env.get("PHOTO_WALL_CACHE_ROOT")
    return Path(value) if value else Path(DEFAULT_CACHE_ROOT)


def media_root(env: dict | None = None) -> Path:
    """The ``media/`` domain directory (cache of Immich variants)."""
    return cache_root(env) / MEDIA_SUBDIR


def apps_root(env: dict | None = None) -> Path:
    """The ``apps/`` domain directory (cache of the Player ``.deb``)."""
    return cache_root(env) / APPS_SUBDIR


def os_images_root(env: dict | None = None) -> Path:
    """The ``os-images/`` domain directory (cache of the OS squashfs)."""
    return cache_root(env) / OS_IMAGES_SUBDIR
