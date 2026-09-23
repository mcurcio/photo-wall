"""Where each asset lives on the cache disk: one subdirectory per kind, a file named by the key.

The names are today's (`os-images/base-<tag>.squashfs`, `apps/app-<sha256>.deb`), so files already
on disk keep serving across the rollout. Temp files start with `TEMP_PREFIX`, which no final name
can, so a temp is never mistaken for an asset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from central.cache_layout import APPS_SUBDIR, OS_IMAGES_SUBDIR
from central.kernel.assets import AssetKey, AssetKind

TEMP_PREFIX: Final = ".tmp-"

_DIRECTORIES: Final[dict[AssetKind, str]] = {
    AssetKind.OS_IMAGE: OS_IMAGES_SUBDIR,
    AssetKind.PLAYER_DEB: APPS_SUBDIR,
}
_FILE_NAMES: Final[dict[AssetKind, tuple[str, str]]] = {
    AssetKind.OS_IMAGE: ("base-", ".squashfs"),
    AssetKind.PLAYER_DEB: ("app-", ".deb"),
}


class CacheLayout:
    """Maps asset keys to paths under one cache root (`cache_layout.cache_root()` in production)."""

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def directory(self, kind: AssetKind) -> Path:
        return self._root / _DIRECTORIES[kind]

    def path(self, key: AssetKey) -> Path:
        prefix, suffix = _FILE_NAMES[key.kind]
        return self.directory(key.kind) / f"{prefix}{key.identity}{suffix}"
