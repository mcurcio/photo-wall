"""Central app package registry -- the Player `.deb` central serves.

Independent of the (retiring) release authority: no shared tables, no
`ReleaseAuthority` dependency, no signature. Per the 0009 owner ruling (home
LAN, no threat model), the sha256 recorded here is a **corruption check
only** -- it lets a downloader detect a truncated/garbled `.deb` in transit,
never an authorship or authenticity proof. Registration is by reference: the
operator stages the `.deb` bytes under `PHOTO_WALL_APP_ROOT` out of band (the
same division of labor as the existing release artifact, whose bytes are
staged under `PHOTO_WALL_RELEASE_ROOT` before `ReleaseAuthority.register`
ever runs) and this module records/promotes the pointer to it.
"""

from __future__ import annotations

import re

from central.db import Database
from contracts.time import Clock

_SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_APP_PACKAGE_BYTES = 1024**3


class AppPackageError(ValueError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


class AppPackages:
    """Singleton "current app" pointer plus a small registry of known packages."""

    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    def register(self, version: str, sha256: str, size: int) -> None:
        """Record a package already staged under PHOTO_WALL_APP_ROOT by sha256.

        Mirrors ReleaseAuthority.register: metadata only, immutable once
        stored. Existence/size of the staged bytes is verified lazily, at
        serve time (see the streaming route), not here.
        """
        if (
            not isinstance(version, str)
            or not 0 < len(version) <= 256
            or not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)
            or type(size) is not int
            or not 0 < size <= MAX_APP_PACKAGE_BYTES
        ):
            raise AppPackageError("invalid_app_package", 422)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO app_packages(sha256,version,size,registered_at) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT(sha256) DO NOTHING",
                (sha256, version, size, self.clock.utc()),
            )
            row = conn.execute(
                "SELECT version,size FROM app_packages WHERE sha256=%s", (sha256,)
            ).fetchone()
            if (row["version"], row["size"]) != (version, size):
                raise AppPackageError("app_package_immutable")

    @staticmethod
    def _package(conn, sha256: str):
        row = conn.execute(
            "SELECT * FROM app_packages WHERE sha256=%s", (sha256,)
        ).fetchone()
        if row is None:
            raise AppPackageError("app_package_not_found", 404)
        return row

    def promote(self, sha256: str) -> None:
        """The one global "current app" pointer the operator promotes (0009 gate #3)."""
        with self.db.transaction() as conn:
            self._package(conn, sha256)
            conn.execute(
                "INSERT INTO app_package_policy VALUES(TRUE,%s) ON CONFLICT(singleton) "
                "DO UPDATE SET current_sha256=EXCLUDED.current_sha256",
                (sha256,),
            )

    def current(self) -> dict:
        """The promoted package's {version, sha256, size}; 503 if none promoted."""
        with self.db.transaction() as conn:
            policy = conn.execute(
                "SELECT current_sha256 FROM app_package_policy WHERE singleton"
            ).fetchone()
            if policy is None:
                raise AppPackageError("app_unconfigured", 503)
            row = self._package(conn, policy["current_sha256"])
            return {"version": row["version"], "sha256": row["sha256"], "size": row["size"]}

    def package(self, sha256: str) -> dict:
        """A registered package's {version, sha256, size} by sha256; 404 if unknown."""
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise AppPackageError("app_package_not_found", 404)
        with self.db.transaction() as conn:
            row = self._package(conn, sha256)
            return {"version": row["version"], "sha256": row["sha256"], "size": row["size"]}
