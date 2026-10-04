"""Where each asset lives on the cache disk: one subdirectory per kind, a file named by the key.

Every name is content-keyed: an OS image by the sha256 of its base tarball
(`os-images/base-<tarball sha256>.squashfs`), a Player `.deb` by its own sha256
(`apps/app-<sha256>.deb`). A re-cut is a new name, so a late run can only write the same bytes to
the same file. A library thumbnail (`previews/asset-<id>.jpg`) is keyed by its ORIGINAL's identity
(Central's one-way asset id), not its own bytes, so its record lives only while a live preview
selects it (`central.assets.library`) and a re-production replaces its produced facts
(`AssetKind.keyed_by_content`). Temp files start with `TEMP_PREFIX`, which no final name can, so a temp is never
mistaken for an asset.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Final

from central.cache_layout import APPS_SUBDIR, OS_IMAGES_SUBDIR, PREVIEWS_SUBDIR
from central.kernel.assets import AssetKey, AssetKind

TEMP_PREFIX: Final = ".tmp-"

_DIRECTORIES: Final[dict[AssetKind, str]] = {
    AssetKind.OS_IMAGE: OS_IMAGES_SUBDIR,
    AssetKind.PLAYER_DEB: APPS_SUBDIR,
    AssetKind.PLAYER_PAYLOAD: APPS_SUBDIR,
    AssetKind.SEALED_ENVIRONMENT: APPS_SUBDIR,
    AssetKind.LIBRARY_THUMBNAIL: PREVIEWS_SUBDIR,
}
_FILE_NAMES: Final[dict[AssetKind, tuple[str, str]]] = {
    AssetKind.OS_IMAGE: ("base-", ".squashfs"),
    AssetKind.PLAYER_DEB: ("app-", ".deb"),
    AssetKind.PLAYER_PAYLOAD: ("payload-", ".tar.gz"),
    AssetKind.SEALED_ENVIRONMENT: ("environment-", ".tar"),
    AssetKind.LIBRARY_THUMBNAIL: ("", ".jpg"),  # `asset-<sha256>.jpg`
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

    def key_of(self, kinds: Iterable[AssetKind], name: str) -> AssetKey | None:
        """The key whose final file is called `name`, among `kinds` (which share one
        directory); None for a temp file or any name no key of those kinds maps to."""
        if name.startswith(TEMP_PREFIX):
            return None
        for kind in kinds:
            prefix, suffix = _FILE_NAMES[kind]
            if (name.startswith(prefix) and name.endswith(suffix)
                    and len(name) > len(prefix) + len(suffix)):
                try:
                    key = AssetKey(kind, name[len(prefix):len(name) - len(suffix)])
                except ValueError:
                    continue
                if self.path(key).name == name:
                    return key
        return None
