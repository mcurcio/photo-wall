"""Release-tracking store for GitHub-sourced Player packages (0010).

The discovered-release list (`app_releases`) and the operator's promoted-tag
pointer (`app_release_policy`) live here; the served bytes and the "current"
pointer stay in `app_packages` (014) and are reached through `AppPackages` --
this module never duplicates the current-pointer or serving logic, it advances
the same pointer via `AppPackages.promote`.

Two pointers exist on purpose (0010): the operator's *chosen* tag
(`promoted_tag`) may briefly precede the *servable* pointer
(`app_package_policy.current_sha256`) while a lazy mirror is in flight. Both are
written only under `SELECT ... FOR UPDATE` on the `app_release_policy` singleton,
because `Database.transaction()` runs at READ COMMITTED (`central/db.py`) where a
bare `promoted_tag == tag` compare admits an ABA race and a crash-between-writes
gap. Safety (`current` never names absent bytes) is guaranteed by
`AppPackages.promote`'s 404 on an unregistered sha256; liveness (`current`
converges to `promoted_tag`) is delivered by the `FOR UPDATE` serialization plus
`reconcile()`. sha256 is a corruption check only (0009 home-LAN ruling).
"""

from __future__ import annotations

import re

from central.app_packages import AppPackages
from central.db import Database
from contracts.time import Clock

_SHA256 = re.compile(r"[0-9a-f]{64}")
# Tag identity/ordering key: strict vX.Y.Z with an optional prerelease tail
# (matches release.yml's publish-time validation and the migration's CHECK).
_TAG = re.compile(r"v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:[.-](?P<pre>[0-9A-Za-z.-]+))?")

# mirror_states whose asset metadata a re-poll may refresh (0010: a release
# re-cut before its bytes were mirrored heals). mirrored/divergent/withdrawn
# hold or reference served bytes and are frozen; undeployable is left to an
# explicit state transition.
_REFRESHABLE = ("discovered", "mirroring", "mirror_failed")


class AppReleaseError(ValueError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def parse_semver(tag: str) -> tuple[int, int, int, str]:
    """Parse a `vX.Y.Z[-pre]` tag into ordering components; 422 if unparseable.

    The tag -- not the `.deb`'s internal version -- is the identity/ordering key
    (0010). Prerelease is the raw tail (compared lexically downstream).
    """
    if not isinstance(tag, str):
        raise AppReleaseError("invalid_tag", 422)
    m = _TAG.fullmatch(tag)
    if not m:
        raise AppReleaseError("invalid_tag", 422)
    return int(m["major"]), int(m["minor"]), int(m["patch"]), m["pre"] or ""


def _sanitize_error(reason: object) -> str | None:
    """Bound and flatten a failure reason for the `mirror_error` column."""
    if reason is None:
        return None
    text = " ".join(str(reason).split())[:500]
    return text or None


class AppReleases:
    """The discovered release list + promoted-tag pointer over `app_packages`."""

    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    # -- discovery -----------------------------------------------------------

    def upsert_discovered(
        self,
        tag: str,
        *,
        is_prerelease: bool = False,
        asset_sha256: str | None = None,
        asset_size: int | None = None,
        asset_url: str | None = None,
    ) -> str:
        """Insert or refresh a discovered release; return the resulting state.

        New row: `discovered` when a player asset (sha256 + size + url) is
        present, else `undeployable`. An existing not-yet-mirrored row
        (`discovered`/`mirroring`/`mirror_failed`) has its asset refreshed so a
        re-cut heals. A `mirrored` row re-cut to a *different* sha256 goes
        `divergent` and its served bytes are never overwritten;
        `divergent`/`withdrawn`/`undeployable` rows are otherwise frozen.
        """
        major, minor, patch, prerelease = parse_semver(tag)
        if asset_sha256 is not None and not _SHA256.fullmatch(asset_sha256):
            raise AppReleaseError("invalid_asset_sha256", 422)
        if asset_size is not None and (type(asset_size) is not int or asset_size <= 0):
            raise AppReleaseError("invalid_asset_size", 422)
        have_asset = asset_sha256 is not None and asset_size is not None and asset_url is not None
        now = self.clock.utc()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM app_releases WHERE tag=%s", (tag,)
            ).fetchone()
            if existing is None:
                state = "discovered" if have_asset else "undeployable"
                conn.execute(
                    "INSERT INTO app_releases(tag,major,minor,patch,prerelease,is_prerelease,"
                    "asset_sha256,asset_size,asset_url,mirror_state,discovered_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (tag, major, minor, patch, prerelease, is_prerelease,
                     asset_sha256, asset_size, asset_url, state, now, now),
                )
                return state
            state = existing["mirror_state"]
            if state in _REFRESHABLE:
                if have_asset:
                    conn.execute(
                        "UPDATE app_releases SET asset_sha256=%s,asset_size=%s,asset_url=%s,"
                        "is_prerelease=%s,updated_at=%s WHERE tag=%s",
                        (asset_sha256, asset_size, asset_url, is_prerelease, now, tag),
                    )
                else:
                    conn.execute(
                        "UPDATE app_releases SET is_prerelease=%s,updated_at=%s WHERE tag=%s",
                        (is_prerelease, now, tag),
                    )
                return state
            if state == "mirrored" and have_asset and asset_sha256 != existing["asset_sha256"]:
                # Re-cut of an already-mirrored tag: freeze the served bytes,
                # flag the divergence. Never overwrite asset_sha256/mirrored_sha256.
                conn.execute(
                    "UPDATE app_releases SET mirror_state='divergent',mirror_error='asset_changed',"
                    "updated_at=%s WHERE tag=%s",
                    (now, tag),
                )
                return "divergent"
            return state  # mirrored (unchanged), divergent, undeployable, withdrawn: frozen

    # -- enumeration ---------------------------------------------------------

    def list(self) -> list[dict]:
        """Every release, semver-ordered, each flagged deployable/promoted/current."""
        with self.db.transaction() as conn:
            promoted_tag, current_sha = self._pointers(conn)
            rows = conn.execute(
                "SELECT * FROM app_releases ORDER BY major DESC,minor DESC,patch DESC,"
                "(prerelease = '') DESC,prerelease DESC"
            ).fetchall()
            return [self._view(r, promoted_tag, current_sha) for r in rows]

    def get(self, tag: str) -> dict | None:
        """A single release's view (deployable/promoted/current flags), or None."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM app_releases WHERE tag=%s", (tag,)).fetchone()
            if row is None:
                return None
            promoted_tag, current_sha = self._pointers(conn)
            return self._view(row, promoted_tag, current_sha)

    # -- promotion + convergence --------------------------------------------

    def set_promoted(self, tag: str) -> bool:
        """Record the operator's chosen tag under `FOR UPDATE`; report readiness.

        Takes `SELECT ... FOR UPDATE` on the `app_release_policy` singleton
        *before* writing, so it serializes against any concurrent promote or
        `reconcile`. Returns True when the tag's bytes are already registered
        (so the caller advances `current` synchronously, typically via
        `reconcile`), False when a mirror must be enqueued first. Refuses an
        unknown (404) or undeployable (409) tag.
        """
        with self.db.transaction() as conn:
            conn.execute("SELECT promoted_tag FROM app_release_policy WHERE singleton FOR UPDATE")
            row = self._require(conn, tag)
            if not self._deployable(row):
                raise AppReleaseError("release_undeployable")
            conn.execute(
                "INSERT INTO app_release_policy VALUES(TRUE,%s) ON CONFLICT(singleton) "
                "DO UPDATE SET promoted_tag=EXCLUDED.promoted_tag",
                (tag,),
            )
            return row["mirrored_sha256"] is not None

    def reconcile(self, app_packages: AppPackages) -> dict:
        """Advance `current` to `promoted_tag`'s bytes when it lags; the backstop.

        Under the same `FOR UPDATE` lock, reads the *current* `promoted_tag`
        (never a stale caller-supplied one), and advances the current pointer via
        `AppPackages.promote` only when that tag's bytes are registered and
        `current` does not already name them. A late mirror for an abandoned
        promote therefore cannot hijack `current`, and a crash between
        `set_promoted` and the advance heals on the next call. Reuses
        `AppPackages.promote` -- the current-pointer write is not duplicated here.
        """
        with self.db.transaction() as conn:
            policy = conn.execute(
                "SELECT promoted_tag FROM app_release_policy WHERE singleton FOR UPDATE"
            ).fetchone()
            if policy is None:
                return {"advanced": False, "reason": "nothing_promoted"}
            tag = policy["promoted_tag"]
            sha = self._require(conn, tag)["mirrored_sha256"]
            if sha is None:
                return {"advanced": False, "reason": "not_mirrored", "tag": tag}
            current = conn.execute(
                "SELECT current_sha256 FROM app_package_policy WHERE singleton"
            ).fetchone()
            if current is not None and current["current_sha256"] == sha:
                return {"advanced": False, "reason": "already_current", "tag": tag, "sha256": sha}
            # promote() 404s unless sha is registered (safety), and opens its own
            # transaction; the FOR UPDATE lock above is held across it, serializing
            # every release-driven writer of the current pointer.
            app_packages.promote(sha)
            return {"advanced": True, "tag": tag, "sha256": sha}

    # -- mirror-state transitions -------------------------------------------

    def mark_mirroring(self, tag: str) -> None:
        """discovered/mirror_failed -> mirroring (download in flight)."""
        with self.db.transaction() as conn:
            row = self._require(conn, tag)
            if row["mirror_state"] not in _REFRESHABLE:
                raise AppReleaseError("release_not_mirrorable")
            conn.execute(
                "UPDATE app_releases SET mirror_state='mirroring',mirror_error=NULL,updated_at=%s "
                "WHERE tag=%s",
                (self.clock.utc(), tag),
            )

    def mark_mirrored(self, tag: str, sha256: str) -> None:
        """-> mirrored: link the registered bytes. The FK guarantees sha256 is
        already in app_packages (bytes on disk, verified), so this can never
        name absent bytes."""
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise AppReleaseError("invalid_asset_sha256", 422)
        with self.db.transaction() as conn:
            self._require(conn, tag)
            conn.execute(
                "UPDATE app_releases SET mirror_state='mirrored',mirrored_sha256=%s,"
                "mirror_error=NULL,updated_at=%s WHERE tag=%s",
                (sha256, self.clock.utc(), tag),
            )

    def mark_mirror_failed(self, tag: str, reason: object = None) -> None:
        """-> mirror_failed (unreachable / corrupt / oversize); retryable."""
        with self.db.transaction() as conn:
            self._require(conn, tag)
            conn.execute(
                "UPDATE app_releases SET mirror_state='mirror_failed',mirror_error=%s,updated_at=%s "
                "WHERE tag=%s",
                (_sanitize_error(reason), self.clock.utc(), tag),
            )

    def mark_undeployable(self, tag: str, reason: object = None) -> None:
        """-> undeployable (no player asset / bad manifest schema); not promotable."""
        with self.db.transaction() as conn:
            self._require(conn, tag)
            conn.execute(
                "UPDATE app_releases SET mirror_state='undeployable',mirror_error=%s,updated_at=%s "
                "WHERE tag=%s",
                (_sanitize_error(reason), self.clock.utc(), tag),
            )

    def mark_withdrawn(self, tag: str) -> None:
        """-> withdrawn: the release was deleted upstream. Asset fields and
        mirrored_sha256 are retained so already-mirrored bytes keep serving; this
        is the safe alternative to pruning a referenced or byte-holding row."""
        with self.db.transaction() as conn:
            self._require(conn, tag)
            conn.execute(
                "UPDATE app_releases SET mirror_state='withdrawn',updated_at=%s WHERE tag=%s",
                (self.clock.utc(), tag),
            )

    def prune(self, tag: str) -> None:
        """Delete a release row no longer upstream.

        Refuses (never FK-violates or strands) a row that holds mirrored bytes or
        is named by `promoted_tag` -- `mark_withdrawn` is the path for those
        (0010). Only a byte-less, un-promoted row may be removed.
        """
        with self.db.transaction() as conn:
            row = self._require(conn, tag)
            if row["mirrored_sha256"] is not None:
                raise AppReleaseError("release_holds_bytes")
            promoted = conn.execute(
                "SELECT promoted_tag FROM app_release_policy WHERE singleton"
            ).fetchone()
            if promoted is not None and promoted["promoted_tag"] == tag:
                raise AppReleaseError("release_promoted")
            conn.execute("DELETE FROM app_releases WHERE tag=%s", (tag,))

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _require(conn, tag: str):
        row = conn.execute("SELECT * FROM app_releases WHERE tag=%s", (tag,)).fetchone()
        if row is None:
            raise AppReleaseError("release_not_found", 404)
        return row

    @staticmethod
    def _pointers(conn) -> tuple[str | None, str | None]:
        promoted = conn.execute(
            "SELECT promoted_tag FROM app_release_policy WHERE singleton"
        ).fetchone()
        current = conn.execute(
            "SELECT current_sha256 FROM app_package_policy WHERE singleton"
        ).fetchone()
        return (
            promoted["promoted_tag"] if promoted is not None else None,
            current["current_sha256"] if current is not None else None,
        )

    @staticmethod
    def _deployable(row) -> bool:
        return row["asset_sha256"] is not None and row["mirror_state"] != "undeployable"

    @classmethod
    def _view(cls, row, promoted_tag: str | None, current_sha: str | None) -> dict:
        view = dict(row)
        view["version"] = row["tag"]
        view["deployable"] = cls._deployable(row)
        view["promoted"] = row["tag"] == promoted_tag
        view["current"] = (
            row["mirrored_sha256"] is not None and row["mirrored_sha256"] == current_sha
        )
        return view
