"""Central's per-device netboot base-image serving + fetch (0012 auto-mirror).

The Pi's initrd (`appliance/netboot_init.py`) fetches its RAM-root squashfs from
`<central>/v1/netboot/base`, self-identifying by hardware serial in the
`X-PhotoWall-Serial` header. This module is the per-device selection + serving
seam (0012 bead 1, the tracer): a serial maps to a canonical `devices` row, that
row resolves ONE release tag by precedence (pin, else latest-verified, else --
empty state only -- latest-discovered), and the base squashfs for that tag is
served from a per-version immutable file `base-<tag>.squashfs` under BASE_ROOT.

Two write seams on the `devices` row, split on purpose (0012):
  * `select_base_for_serial` (unauthenticated netboot) upserts the row and, on a
    200 (bytes actually cached), records the tag it served (`last_served_tag`,
    `boot_outcome='pending'`). It records NOTHING on a 503 cache miss, so a
    self-healing miss is never mistaken for a failed boot.
  * `record_base_health` (authenticated base-health) advances `known_good_tag`
    only for a genuinely healthy check-in whose `running_tag` equals the tag
    Central recorded as last-served -- the sole writer of the frontier input.

Both run their read-modify-write under `SELECT ... FOR UPDATE` on the `devices`
row (E1); the base-health write is additionally a single conditional UPDATE
guarded on `last_served_tag = running_tag`, so a concurrent recovery serve that
moved the served tag invalidates a stale health write.

The cache is keyed by the release TAG, never by content sha (decision 2b): each
version owns its own row and file, and the squashfs sha256 is an integrity /
`Digest` attribute only. sha256 is a corruption check (0009 home-LAN); nothing is
signed.

Rollback / sticky recovery (`failed_tag`), the poll-tail pending sweep, GC, and
boot re-hydrate are LATER beads (2/3/4); this module builds only the tracer arc.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from central.app_releases import AppReleaseError, parse_semver
from central.artifact_io import HardenedOpenError, open_regular
from contracts.equipment import equipment_device_id
from contracts.release import MAX_ROOTFS_BYTES

if TYPE_CHECKING:  # avoid a hard import cycle at module load; only for type hints
    from contracts.models import BaseHealth

BASE_IMAGE_NAME = "photo-wall-base.squashfs"
SHA256SUMS_NAME = "SHA256SUMS"
# The two allowlisted members inside a release's base tarball. The tarball is
# built with arcname="photo-wall-base" (scripts/package_release_artifacts.py),
# so every member is prefixed. We read ONLY these two exact names -- never
# extractall -- and take the FIRST occurrence of each, so a hostile archive
# (traversal, symlink, device, duplicate name) writes nothing.
TARBALL_SQUASHFS_MEMBER = f"photo-wall-base/{BASE_IMAGE_NAME}"
TARBALL_SUMS_MEMBER = f"photo-wall-base/{SHA256SUMS_NAME}"
# The request header the Pi's initrd sends its hardware serial in. Duplicated on
# the client side (appliance/netboot_init.py SERIAL_HEADER): the two sides are
# independent (central must not import appliance, and vice versa), so this wire
# constant is restated rather than shared, like the route strings.
SERIAL_HEADER = "X-PhotoWall-Serial"
# The `device_id` derivation kind, shared with appliance/bootstrap.py and
# player/service.py via contracts.equipment so one Pi resolves to one device_id.
DEVICE_KIND = "pi"
_MAX_SUMS_BYTES = 4 * 1024 * 1024
MAX_SQUASHFS_BYTES = MAX_ROOTFS_BYTES  # 1 GiB; the same bound the initrd fetch enforces
# The only shape a client serial may take before it can select an image or be
# logged: a Pi serial is 16 hex digits, but keep a small safe superset so a
# future per-serial scheme has room, and reject everything else (control chars,
# path separators, whitespace) at the seam -- the serial is unauthenticated,
# client-controlled input.
_SAFE_SERIAL = re.compile(r"[A-Za-z0-9:_.-]{1,128}")


class NetbootBaseError(Exception):
    """A fixed diagnostic code for a base-root / base-fetch fault."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class BaseRootError(NetbootBaseError):
    """BASE_ROOT is unconfigured, missing, or not writable -- a FAIL-LOUD boot
    condition, never a silent later 503."""


class BaseFetchError(NetbootBaseError):
    """A base fetch could not produce a verified per-version squashfs file."""


@dataclass(frozen=True, slots=True)
class BaseServeDecision:
    """The netboot seam's verdict for one request.

    `served_tag` is the tag whose bytes to serve (None => nothing resolvable, a
    503). `cached` distinguishes the 200 path (bytes present, `squashfs_sha256`
    is the `Digest`) from the 503 miss (enqueue a fetch; NO last-served record
    was written). `fetch_tag` is the tag to enqueue on a miss.
    """

    served_tag: str | None
    cached: bool
    squashfs_sha256: str | None

    @property
    def fetch_tag(self) -> str | None:
        return self.served_tag if (self.served_tag is not None and not self.cached) else None


# -- serial / path helpers ---------------------------------------------------


def sanitize_serial(serial: str | None) -> str | None:
    """The serial if it matches the safe charset, else None. Bounds what the
    route logs AND guards the selection seam, so the guarantee holds regardless
    of caller."""
    if serial is not None and _SAFE_SERIAL.fullmatch(serial):
        return serial
    return None


def device_id_for_serial(serial: str | None) -> str | None:
    """Map a (sanitized) serial to its canonical `device-<64hex>` id, or None.

    Uses the one shared derivation (`contracts.equipment.equipment_device_id`,
    `kind="pi"`) the appliance and the flashed-image fallback use, so a given Pi
    resolves to the SAME `device_id` at the netboot seam as it does at
    enrollment (which is how `players.device_id` later joins the row)."""
    if serial is None:
        return None
    return equipment_device_id(DEVICE_KIND, serial.encode())


def base_file_path(base_root: Path, tag: str) -> Path:
    """The per-version immutable squashfs file for `tag` under BASE_ROOT."""
    return Path(base_root) / f"base-{tag}.squashfs"


def resolve_base_root(env: dict | None = None) -> Path | None:
    """The dedicated base-image directory from PHOTO_WALL_BASE_ROOT, or None.

    Shared by `create_app` (serve, RO) and the worker (write, RW) so the two
    never drift on the path. Returning None (unset) is distinct from "set but
    unwritable" -- the writability assertion below is the worker's FAIL-LOUD."""
    env = os.environ if env is None else env
    value = env.get("PHOTO_WALL_BASE_ROOT")
    return Path(value) if value else None


def assert_base_root_writable(base_root: Path | None) -> Path:
    """FAIL LOUD if BASE_ROOT is unconfigured/missing/unwritable (0012 boot
    assertion) -- raise here at startup rather than 503 silently at serve time.

    Returns the validated path so a caller can `base_root = assert_...(root)`."""
    if base_root is None:
        raise BaseRootError("base_root_unconfigured")
    path = Path(base_root)
    if not path.is_dir():
        raise BaseRootError("base_root_missing")
    if not os.access(path, os.W_OK | os.X_OK):
        raise BaseRootError("base_root_unwritable")
    return path


# -- selection (netboot seam) ------------------------------------------------


def _rank(tag: str) -> tuple | None:
    """A total order over release tags for `max`, or None for a non-semver tag
    (excluded). Full releases outrank prereleases of the same X.Y.Z; prereleases
    compare lexically (the same ordering `app_releases` indexes)."""
    try:
        major, minor, patch, prerelease = parse_semver(tag)
    except AppReleaseError:
        return None  # non-semver: never a frontier target
    return (major, minor, patch, prerelease == "", prerelease)


def _max_semver(tags) -> str | None:
    best_tag, best_key = None, None
    for tag in tags:
        key = _rank(tag)
        if key is not None and (best_key is None or key > best_key):
            best_tag, best_key = tag, key
    return best_tag


def latest_verified(conn) -> str | None:
    """max(semver) over NON-RETIRED devices' `known_good_tag` -- the live
    frontier, never a stored column. Non-semver tags are excluded."""
    rows = conn.execute(
        "SELECT DISTINCT known_good_tag FROM devices "
        "WHERE retired_at IS NULL AND known_good_tag IS NOT NULL"
    ).fetchall()
    return _max_semver(row["known_good_tag"] for row in rows)


def latest_discovered(conn) -> str | None:
    """The highest semver release that carries base facts and is not a
    prerelease -- the EMPTY-STATE bootstrap target only (the one unverified base
    the design ever serves). Non-semver tags are excluded."""
    rows = conn.execute(
        "SELECT tag FROM app_releases "
        "WHERE base_tarball_sha256 IS NOT NULL AND base_tarball_url IS NOT NULL "
        "AND is_prerelease = FALSE"
    ).fetchall()
    return _max_semver(row["tag"] for row in rows)


def _cache_row(conn, tag: str):
    return conn.execute(
        "SELECT state, squashfs_sha256 FROM base_cache WHERE tag=%s", (tag,)
    ).fetchone()


def select_base_for_serial(conn, serial: str | None, *, clock) -> BaseServeDecision:
    """Resolve + (on a 200 only) record the base a Pi should be served.

    Runs the read-modify-write under `SELECT ... FOR UPDATE` on the `devices`
    row (E1). Precedence (bead 1; recovery/`failed_tag` is bead 2): the device's
    pin (`attached_tag`), else latest-verified, else -- only when the frontier is
    empty -- latest-discovered. On the cached (200) branch it records
    `last_served_tag`/`boot_outcome='pending'`/`last_served_at`; on a miss it
    records NOTHING (the fetch is enqueued by the caller and the Pi retries)."""
    serial = sanitize_serial(serial)
    device_id = device_id_for_serial(serial)
    now = clock.utc()

    if device_id is None:
        # Invalid/absent serial: no device row, serve the unpinned target
        # best-effort, record nothing (there is no row to record on).
        desired = latest_verified(conn) or latest_discovered(conn)
        return _decision(conn, desired, device_id=None, now=now)

    conn.execute(
        "INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES(%s,%s,%s,%s) "
        "ON CONFLICT(device_id) DO UPDATE SET serial=EXCLUDED.serial, last_seen=EXCLUDED.last_seen",
        (device_id, serial, now, now),
    )
    device = conn.execute(
        "SELECT * FROM devices WHERE device_id=%s FOR UPDATE", (device_id,)
    ).fetchone()

    if device["attached_tag"] is not None:
        desired = device["attached_tag"]                 # pin overrides
    else:
        desired = latest_verified(conn) or latest_discovered(conn)
    return _decision(conn, desired, device_id=device_id, now=now)


def _decision(conn, served: str | None, *, device_id: str | None, now: float) -> BaseServeDecision:
    if served is None:
        return BaseServeDecision(None, False, None)
    row = _cache_row(conn, served)
    if row is None or row["state"] != "cached" or row["squashfs_sha256"] is None:
        return BaseServeDecision(served, False, None)  # miss: 503 + enqueue, record nothing
    if device_id is not None:
        # 200 branch ONLY: record the tag whose bytes we are about to serve. A
        # fresh 200 is the only thing that resets boot_outcome (guarded state
        # machine); bead 1 never writes 'failed'.
        conn.execute(
            "UPDATE devices SET last_served_tag=%s, boot_outcome='pending', last_served_at=%s "
            "WHERE device_id=%s",
            (served, now, device_id),
        )
    return BaseServeDecision(served, True, row["squashfs_sha256"])


# -- base-health seam --------------------------------------------------------


def record_base_health(conn, device_id: str, report: "BaseHealth", *, clock) -> bool:
    """Advance `known_good_tag` for a validated base-health check-in; return
    whether it was applied.

    Read-modify-write under `SELECT ... FOR UPDATE` on the device row, then a
    single conditional `UPDATE ... WHERE last_served_tag = running_tag` (E1): a
    concurrent recovery serve that moved `last_served_tag` between the read and
    the write invalidates this write, so a device is never recorded healthy on a
    tag it was just rolled off. Requires: the device row exists; the report is
    monotonic per authority epoch (known-good only advances); `healthy` is true;
    and `running_tag` equals the tag Central recorded as last-served (not a live
    re-resolve -- that would drop a genuine report after a frontier move, S1).
    On success also clears `failed_tag` if it equals `running_tag`."""
    device = conn.execute(
        "SELECT device_id FROM devices WHERE device_id=%s FOR UPDATE", (device_id,)
    ).fetchone()
    if device is None:
        return False

    prior = conn.execute(
        "SELECT sequence FROM device_base_health WHERE device_id=%s AND authority_epoch=%s",
        (device_id, report.authority_epoch),
    ).fetchone()
    if prior is not None and report.sequence <= prior["sequence"]:
        return False  # monotonicity: a reordered/older check-in never regresses known-good
    conn.execute(
        "INSERT INTO device_base_health(device_id, authority_epoch, sequence) VALUES(%s,%s,%s) "
        "ON CONFLICT(device_id, authority_epoch) DO UPDATE SET sequence=EXCLUDED.sequence",
        (device_id, report.authority_epoch, report.sequence),
    )
    if not report.healthy:
        return False  # a false report never advances known-good, but did advance the sequence

    updated = conn.execute(
        "UPDATE devices SET known_good_tag=%s, known_good_at=%s, boot_outcome='healthy', "
        "failed_tag=CASE WHEN failed_tag=%s THEN NULL ELSE failed_tag END "
        "WHERE device_id=%s AND last_served_tag=%s",
        (report.running_tag, clock.utc(), report.running_tag, device_id, report.running_tag),
    ).rowcount
    return updated == 1


# -- base fetch (worker task body) -------------------------------------------


def _first_member(tar: tarfile.TarFile, name: str) -> tarfile.TarInfo | None:
    """The FIRST member with exactly `name`, ignoring later duplicates."""
    for member in tar.getmembers():
        if member.name == name:
            return member
    return None


def _read_squashfs_digest(sums_bytes: bytes) -> str | None:
    """The squashfs sha256 (hex) from a base bundle's `SHA256SUMS`, or None.

    PRIVATE to the fetch: the serve side reads the `Digest` from the DB, so this
    parser has exactly one consumer. `SHA256SUMS` lines are `<hex>  <name>`;
    names may be `./`-prefixed (build_netboot_bundle.sh emits relative paths)."""
    for line in sums_bytes.decode("utf-8", "replace").splitlines():
        digest, _, name = line.partition("  ")
        if name.strip().lstrip("./") == BASE_IMAGE_NAME and _is_sha256_hex(digest):
            return digest
    return None


def _extract_squashfs(tar_path: Path, base_root: Path) -> tuple[str, str, int]:
    """Allowlist-extract the squashfs + verify it against the bundle's
    `SHA256SUMS`. Returns (temp path, squashfs sha256, size). Raises
    `BaseFetchError` on any hostile/inconsistent archive, leaving no file."""
    with tarfile.open(tar_path, "r:gz") as tar:
        squashfs = _first_member(tar, TARBALL_SQUASHFS_MEMBER)
        sums = _first_member(tar, TARBALL_SUMS_MEMBER)
        if squashfs is None or sums is None:
            raise BaseFetchError("base_member_missing")
        if not squashfs.isfile() or not sums.isfile():
            raise BaseFetchError("base_member_not_file")  # symlink/hardlink/device/fifo refused
        if squashfs.size > MAX_SQUASHFS_BYTES:
            raise BaseFetchError("base_too_large")
        if sums.size > _MAX_SUMS_BYTES:
            raise BaseFetchError("base_sums_too_large")

        sums_handle = tar.extractfile(sums)
        sums_bytes = sums_handle.read(_MAX_SUMS_BYTES + 1) if sums_handle else b""
        if len(sums_bytes) > _MAX_SUMS_BYTES:
            raise BaseFetchError("base_sums_too_large")
        expected = _read_squashfs_digest(sums_bytes)
        if expected is None:
            raise BaseFetchError("base_sums_no_squashfs")

        descriptor, temp_name = tempfile.mkstemp(dir=base_root, prefix=".base-", suffix=".tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            source = tar.extractfile(squashfs)
            with os.fdopen(descriptor, "wb") as output:
                while chunk := (source.read(1024 * 1024) if source else b""):
                    size += len(chunk)
                    if size > MAX_SQUASHFS_BYTES:
                        raise BaseFetchError("base_too_large")
                    output.write(chunk)
                    digest.update(chunk)
        except BaseException:
            os.unlink(temp_name)
            raise
    computed = digest.hexdigest()
    if computed != expected:
        os.unlink(temp_name)
        raise BaseFetchError("base_digest_mismatch")
    return temp_name, computed, size


def _set_cache_state(
    db, clock, tag: str, state: str, *, squashfs_sha256=None, size=None, error=None
) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO base_cache(tag, state, squashfs_sha256, size, error, updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(tag) DO UPDATE SET state=EXCLUDED.state, "
            "squashfs_sha256=COALESCE(EXCLUDED.squashfs_sha256, base_cache.squashfs_sha256), "
            "size=COALESCE(EXCLUDED.size, base_cache.size), error=EXCLUDED.error, "
            "updated_at=EXCLUDED.updated_at",
            (tag, state, squashfs_sha256, size, error, clock.utc()),
        )


async def fetch_base(source, db, clock, tag: str, base_root: Path) -> str:
    """Download, verify, allowlist-extract, and atomically install one version's
    base squashfs; return its sha256. The per-version worker task body.

    Reuses `GithubReleaseSource.download` (bounded, streamed, sha-verified, fails
    closed) for the tarball, then extracts ONLY the two prefixed members, writes
    the squashfs via `mkstemp` -> atomic rename to `base-<tag>.squashfs`, and
    records `base_cache.squashfs_sha256` + `state='cached'`. The enqueue side
    coalesces duplicate fetches under a `base:<tag>` queueing lock; this body is
    also idempotent (write-once-per-tag file; a re-fetch reproduces the bytes)."""
    base_root = assert_base_root_writable(base_root)
    with db.transaction() as conn:
        release = conn.execute(
            "SELECT base_tarball_url, base_tarball_sha256, base_tarball_size "
            "FROM app_releases WHERE tag=%s",
            (tag,),
        ).fetchone()
    if release is None or not release["base_tarball_url"] or not release["base_tarball_sha256"]:
        raise BaseFetchError("base_facts_missing")

    _set_cache_state(db, clock, tag, "caching")
    tarball = base_root / f".base-{tag}.tar.{secrets.token_hex(8)}.tmp"
    squashfs_temp: str | None = None
    try:
        await source.download(
            release["base_tarball_url"],
            tarball,
            sha256=release["base_tarball_sha256"],
            max_bytes=source.max_deb_bytes,
        )
        squashfs_temp, sha256, size = _extract_squashfs(tarball, base_root)
        os.replace(squashfs_temp, base_file_path(base_root, tag))
        squashfs_temp = None
        _set_cache_state(db, clock, tag, "cached", squashfs_sha256=sha256, size=size)
        return sha256
    except NetbootBaseError as error:
        _set_cache_state(db, clock, tag, "failed", error=error.code)
        raise
    except Exception as error:
        _set_cache_state(db, clock, tag, "failed", error=type(error).__name__)
        raise
    finally:
        tarball.unlink(missing_ok=True)
        if squashfs_temp is not None:
            Path(squashfs_temp).unlink(missing_ok=True)


# -- legacy single-base helpers (retained; superseded by per-version serving) --


def base_digest(base_root: Path) -> str | None:
    """The stored sha256 (hex) of `BASE_IMAGE_NAME` from a single-base bundle's
    build-time `SHA256SUMS`, or None. Retained from the pre-0012 single-base
    model; per-version serving reads the `Digest` from `base_cache` instead."""
    try:
        descriptor, _ = open_regular(base_root / SHA256SUMS_NAME, max_size=_MAX_SUMS_BYTES)
    except HardenedOpenError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(_MAX_SUMS_BYTES + 1)
    except OSError:
        return None
    return _read_squashfs_digest(raw)


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
