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

Server-side rollback / sticky recovery (bead 2) lives on the read side of the
netboot seam: `select_base_for_serial` detects a 200-served target that never
reported base-healthy, fences it in `failed_tag`, and serves the device's
known-good instead -- sticking there until the desired target changes. The
poll-tail `sweep_failed_boots` fails a device left `pending` past
`PENDING_HEALTH_TIMEOUT` so a powered-off device does not hold the frontier.

Boot re-hydrate + stray-temp sweep (bead 3) and need-driven GC (bead 4) close
the cache lifecycle: `boot_rehydrate_targets`/`empty_state_bootstrap_tag`/
`sweep_stray_temps` self-heal the on-disk cache at worker boot, and
`gc_base_cache` evicts bytes whose tag has left the keep-set (latest-verified U
non-retired pins U non-retired known-good U in-flight `caching`). Both key off
the release TAG and the uniform `retired_at IS NULL` device filter.
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
# The one naming convention for a fetch's in-progress temp files (both the
# streamed tarball and the extracted-squashfs mkstemp). The final immutable file
# is `base-<tag>.squashfs` (no leading dot), so it never matches this prefix --
# which is exactly what lets the boot-time stray-temp sweep (bead 3) delete an
# orphaned temp from a crash-before-rename without ever touching a served file.
_TEMP_PREFIX = ".base-"
_TEMP_SUFFIX = ".tmp"
# Recorded on a `base_cache` row when GC (bead 4) unlinks its bytes: the tag left
# the keep-set. Observability only; the row (and its integrity sha) survive.
_GC_EVICTION_REASON = "not_in_keep_set"
# The only shape a client serial may take before it can select an image or be
# logged: a Pi serial is 16 hex digits, but keep a small safe superset so a
# future per-serial scheme has room, and reject everything else (control chars,
# path separators, whitespace) at the seam -- the serial is unauthenticated,
# client-controlled input.
_SAFE_SERIAL = re.compile(r"[A-Za-z0-9:_.-]{1,128}")
# A 200-served target that has not reported base-healthy within this window is
# swept `failed` (bead 2 poll-tail), so a powered-off / stuck device does not hold
# the latest-verified frontier or a cache entry indefinitely. Placeholder cadence
# (0012 gate); nothing structural depends on the exact value.
PENDING_HEALTH_TIMEOUT = 15 * 60


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


# -- base-root boot status (bead 9 observability, errata E4) ------------------


def record_base_boot_status(conn, *, ok: bool, code: str | None, clock) -> None:
    """Upsert the single-row BASE_ROOT boot-assertion outcome (0012 bead 9, E4).

    Written by the worker's boot path on EVERY base boot -- ``ok=True`` when
    ``assert_base_root_writable`` passed, ``ok=False`` + the ``BaseRootError``
    code when it raised. The fail-loud assertion otherwise only logs and the
    worker swallows the exception (E4); this row is what makes a misconfigured
    base volume operator-visible at ``GET /v1/operator/netboot`` rather than a
    silent later 503."""
    conn.execute(
        "INSERT INTO base_boot_status(singleton, ok, code, checked_at) VALUES(TRUE,%s,%s,%s) "
        "ON CONFLICT(singleton) DO UPDATE SET ok=EXCLUDED.ok, code=EXCLUDED.code, "
        "checked_at=EXCLUDED.checked_at",
        (ok, code, clock.utc()),
    )


def read_base_boot_status(conn) -> dict | None:
    """The last recorded BASE_ROOT boot-assertion outcome, or None if never run.

    None means no worker with ``PHOTO_WALL_BASE_ROOT`` configured has booted yet
    (base serving off, or a base-less worker) -- distinct from ``ok=False`` (a
    configured volume that failed its assertion)."""
    row = conn.execute(
        "SELECT ok, code, checked_at FROM base_boot_status WHERE singleton"
    ).fetchone()
    if row is None:
        return None
    return {"ok": row["ok"], "code": row["code"], "checked_at": row["checked_at"]}


def operator_base_status(conn, base_root: Path | None) -> dict:
    """The read-only operator observability view for the base-mirror (0012 bead 9).

    Answers the three questions the design's bead-9 page names: "why did this
    device get this image / why won't it advance / why were bytes evicted."
      * ``boot_status`` -- the BASE_ROOT boot assertion outcome (E4), so a failed
        base volume is visible, not only logged.
      * ``frontier`` -- the live ``latest_verified`` (the unpinned target every
        device follows), so an operator can see WHY an unpinned device resolves a
        given image.
      * ``devices`` -- each device's pin, known-good, last-served tag + boot
        outcome, and sticky ``failed_tag`` (why it won't advance / rolled back).
      * ``cache`` -- each ``base_cache`` row's state and ``eviction_reason`` (why
        bytes were evicted; the row and its integrity sha survive an eviction).
    All read-only; no state is changed. The operator UI over this is deferred
    (0012 out-of-scope: "backend fields ship")."""
    devices = [
        {
            "device_id": row["device_id"],
            "serial": row["serial"],
            "attached_tag": row["attached_tag"],
            "known_good_tag": row["known_good_tag"],
            "last_served_tag": row["last_served_tag"],
            "boot_outcome": row["boot_outcome"],
            "failed_tag": row["failed_tag"],
            "last_served_at": row["last_served_at"],
            "retired_at": row["retired_at"],
        }
        for row in conn.execute(
            "SELECT device_id, serial, attached_tag, known_good_tag, last_served_tag, "
            "boot_outcome, failed_tag, last_served_at, retired_at FROM devices "
            "ORDER BY device_id"
        ).fetchall()
    ]
    cache = [
        {
            "tag": row["tag"],
            "state": row["state"],
            "squashfs_sha256": row["squashfs_sha256"],
            "size": row["size"],
            "error": row["error"],
            "eviction_reason": row["eviction_reason"],
            "updated_at": row["updated_at"],
        }
        for row in conn.execute(
            "SELECT tag, state, squashfs_sha256, size, error, eviction_reason, updated_at "
            "FROM base_cache ORDER BY tag"
        ).fetchall()
    ]
    return {
        "base_root_configured": base_root is not None,
        "boot_status": read_base_boot_status(conn),
        "frontier": latest_verified(conn),
        "devices": devices,
        "cache": cache,
    }


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
    row (E1). Precedence: the device's pin (`attached_tag`), else latest-verified,
    else -- only when the frontier is empty -- latest-discovered. On top of that
    (bead 2) the recovery-aware arm keyed off `failed_tag` detects a failed boot,
    releases a stale stick, or serves known-good (`_resolve_recovery_aware`). On
    the cached (200) branch it records `last_served_tag`/`boot_outcome='pending'`/
    `last_served_at`; on a miss it records NOTHING (the fetch is enqueued by the
    caller and the Pi retries)."""
    serial = sanitize_serial(serial)
    device_id = device_id_for_serial(serial)
    now = clock.utc()

    if device_id is None:
        # Invalid/absent serial: no device row, serve the unpinned target
        # best-effort, record nothing (there is no row to record on -- so no
        # detection/recovery either, which key off the row's boot state).
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

    served = _resolve_recovery_aware(conn, device, device_id=device_id)
    return _decision(conn, served, device_id=device_id, now=now)


def _resolve_recovery_aware(conn, device, *, device_id: str) -> str | None:
    """The served tag by recovery-aware precedence, under the caller's FOR UPDATE.

    Bead 2 -- splits the served tag from the fenced `failed_tag` (r8). A pin always
    wins and clears any stick (the operator's explicit choice supersedes it);
    otherwise the desired target is latest-verified (else, empty state only,
    latest-discovered). Three sticky-recovery arms key off `failed_tag`, never off
    `last_served_tag`:

      * DETECT -- a diskless device that 200-booted `desired` (`last_served_tag ==
        desired`) and never posted it healthy (`boot_outcome == 'pending'`) is back
        asking, so that boot failed: fence it (`failed_tag = desired`,
        `boot_outcome = 'failed'`). A 503 miss wrote no `pending` record, so a
        fetch-retry is never mis-detected as a failed boot (bead-1 invariant).
      * RELEASE -- the desired target moved off the fence (a newer latest-verified,
        or the pin cleared it above) => clear `failed_tag`, one fresh attempt.
      * RECOVER -- still fenced (`failed_tag == desired`) with a known-good => serve
        known-good, recorded truthfully by `_decision` as `last_served_tag`; the
        stick (`failed_tag`) is LEFT INTACT so the failing tag is not re-served and
        the detector stays quiet next boot (`last_served_tag` != `desired`).
    """
    if device["attached_tag"] is not None:               # pin overrides
        if device["failed_tag"] is not None:
            conn.execute(
                "UPDATE devices SET failed_tag=NULL WHERE device_id=%s", (device_id,)
            )
        return device["attached_tag"]

    desired = latest_verified(conn) or latest_discovered(conn)
    failed = device["failed_tag"]

    if (
        device["last_served_tag"] is not None
        and device["last_served_tag"] == desired
        and device["boot_outcome"] == "pending"
    ):                                                    # DETECT
        conn.execute(
            "UPDATE devices SET failed_tag=%s, boot_outcome='failed' WHERE device_id=%s",
            (desired, device_id),
        )
        failed = desired

    if failed is None or desired is None:
        return desired
    if desired != failed:                                # RELEASE
        conn.execute("UPDATE devices SET failed_tag=NULL WHERE device_id=%s", (device_id,))
        return desired
    if device["known_good_tag"] is not None:             # RECOVER (stick holds)
        return device["known_good_tag"]
    return desired  # fenced but no known-good: boot-loops until an operator pins it


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


# -- operator pin / attachment surface (bead 7) ------------------------------


def set_device_pin(conn, device_id: str, tag: str) -> bool:
    """Set `devices.attached_tag = tag` (the operator pin); return whether a row
    matched.

    The pin is the precedence WINNER at the serve seam (`_resolve_recovery_aware`
    serves a pinned device its pin and releases any sticky `failed_tag` on the
    next netboot -- the operator's explicit choice supersedes the stick), so this
    write only records the tag; the failed-tag release composes at serve time.
    Validates `tag` exists in `app_releases` (the `attached_tag` FK target) FIRST,
    raising `AppReleaseError('release_not_found', 404)` rather than surfacing a raw
    FK violation as a 500 -- so a bad tag is a clean rejection with NO mutation.
    Returns False when no `devices` row matches `device_id` (a row is auto-created
    only at the netboot seam), so the caller 404s an unknown device."""
    if conn.execute("SELECT 1 FROM app_releases WHERE tag=%s", (tag,)).fetchone() is None:
        raise AppReleaseError("release_not_found", 404)
    return (
        conn.execute(
            "UPDATE devices SET attached_tag=%s WHERE device_id=%s", (tag, device_id)
        ).rowcount
        == 1
    )


def clear_device_pin(conn, device_id: str) -> bool:
    """Clear `devices.attached_tag` (NULL => unpinned); return whether a row matched.

    The device then falls back to the unpinned precedence at the serve seam --
    latest-verified, else (empty state only) latest-discovered. Returns False when
    no `devices` row matches `device_id`, so the caller 404s an unknown device."""
    return (
        conn.execute(
            "UPDATE devices SET attached_tag=NULL WHERE device_id=%s", (device_id,)
        ).rowcount
        == 1
    )


# -- per-device .deb carried tag (bead 5) ------------------------------------


def served_tag_for_serial(conn, serial: str | None) -> str | None:
    """The tag whose base bytes this device was ACTUALLY served this boot, or None.

    Read-only lookup of `devices.last_served_tag` for the serial's device row
    (F4): the per-device `.deb` rides the exact tag its base was served this
    boot -- never a fresh `latest_verified`/`latest_discovered` re-resolve -- so
    base and `.deb` cannot diverge even if the frontier moved between the base
    serve and the `.deb` fetch. On a recovery boot `last_served_tag` is the
    known-good tag, so the recovery boot's base and `.deb` agree on it too.

    Returns None when the serial is absent/unsafe, no device row exists, or the
    device has never been served a base on a 200 (`last_served_tag IS NULL`) --
    there is no carried tag, so there is no per-device `.deb` answer. The caller
    fails closed (503); it must NOT fall back to 0010's global `current()`, which
    could name a different tag and reintroduce the divergence this closes.
    """
    device_id = device_id_for_serial(sanitize_serial(serial))
    if device_id is None:
        return None
    row = conn.execute(
        "SELECT last_served_tag FROM devices WHERE device_id=%s", (device_id,)
    ).fetchone()
    return row["last_served_tag"] if row is not None else None


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


# -- poll-tail pending sweep (bead 2) ----------------------------------------


def sweep_failed_boots(conn, *, clock, timeout: float = PENDING_HEALTH_TIMEOUT) -> int:
    """Fail any non-retired device left `pending` past `timeout`; return the count.

    Runs at the poll tail. A device 200-served a target it never posts base-healthy
    would otherwise hold the latest-verified frontier and a cache entry forever
    (e.g. it was powered off before reporting). It sets `boot_outcome = 'failed'`
    and -- ONLY WHEN `failed_tag IS NULL` (E2: never overwrite a live stick, and
    never with the recovery boot's known-good tag) -- fences the tag the device
    ACTUALLY ATTEMPTED: its pin, else `last_served_tag` (E2b). This mirrors the live
    DETECT arm, which only ever fences the served tag. Using a recomputed frontier
    would be wrong precisely when the frontier has drifted past what this device
    served (another device pushed latest-verified higher): it would fence a tag the
    device never attempted, so its next boot would `RECOVER` and silently skip a
    legitimate already-verified upgrade. If the device never served anything and has
    no pin (`COALESCE` is NULL) `failed_tag` stays NULL -- no bogus fence, just the
    `failed` outcome. A recovery boot already carries a non-NULL `failed_tag`, so the
    guard leaves its stick untouched while still recording the timeout as `failed`.
    """
    return conn.execute(
        "UPDATE devices SET boot_outcome='failed', "
        "failed_tag = CASE WHEN failed_tag IS NULL "
        "THEN COALESCE(attached_tag, last_served_tag) ELSE failed_tag END "
        "WHERE boot_outcome='pending' AND retired_at IS NULL "
        "AND last_served_at IS NOT NULL AND last_served_at < %s",
        (clock.utc() - timeout,),
    ).rowcount


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

        descriptor, temp_name = tempfile.mkstemp(dir=base_root, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
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
    tarball = base_root / f"{_TEMP_PREFIX}{tag}.tar.{secrets.token_hex(8)}{_TEMP_SUFFIX}"
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


# -- keep-set (shared by boot re-hydrate + GC) -------------------------------


def _nonretired_device_tags(conn) -> set[str]:
    """Every tag any NON-RETIRED device pins or ran known-good.

    The uniform device terms of BOTH the GC keep-set (bead 4) and the boot
    re-hydrate target set (bead 3): a retired device contributes nothing to
    either (it never boots again), so its versions become evictable and stop
    holding the frontier -- the single `retired_at IS NULL` filter the design
    requires on every device term. NULL columns are ignored."""
    rows = conn.execute(
        "SELECT attached_tag, known_good_tag FROM devices WHERE retired_at IS NULL"
    ).fetchall()
    tags: set[str] = set()
    for row in rows:
        for tag in (row["attached_tag"], row["known_good_tag"]):
            if tag is not None:
                tags.add(tag)
    return tags


# -- boot re-hydrate + stray-temp sweep (bead 3) -----------------------------


def empty_state_bootstrap_tag(conn) -> str | None:
    """The latest-discovered tag to fetch on an EMPTY cluster, or None.

    Empty state = no non-retired device has ever reported base-healthy (the live
    frontier `latest_verified` is empty). Only then does a fresh cluster fetch
    latest-discovered so it can serve an (unverified) bootstrap image -- the one
    unverified serve the design ever makes. Once any device is healthy the
    frontier is non-empty and this returns None (bootstrap never overrides a
    verified fleet)."""
    if latest_verified(conn) is not None:
        return None
    return latest_discovered(conn)


def boot_rehydrate_targets(conn, base_root: Path) -> list[str]:
    """Tags to re-enqueue at boot: a `cached` cache row whose file is ABSENT.

    The persistent-volume-wiped / cold-start self-heal (the fix for the root
    503): the cache row says the bytes should exist but they do not, so re-fetch
    them before the common serve path 503s. Restricted to the NEEDED set --
    latest-verified U every non-retired device's pin/known-good (its resolved
    targets and rollback target), else the empty-state bootstrap tag -- so a
    no-longer-needed stale row is left for GC rather than re-fetched. The
    absent-file check is load-bearing: a present file is NOT re-enqueued (an
    idempotent, coalesced fetch is cheap, but a needless one is still avoided).
    """
    needed = _nonretired_device_tags(conn)
    verified = latest_verified(conn)
    if verified is not None:
        needed.add(verified)
    else:
        bootstrap = latest_discovered(conn)
        if bootstrap is not None:
            needed.add(bootstrap)
    missing: list[str] = []
    for tag in sorted(needed):
        row = _cache_row(conn, tag)
        if (
            row is not None
            and row["state"] == "cached"
            and not base_file_path(base_root, tag).exists()
        ):
            missing.append(tag)
    return missing


def sweep_stray_temps(base_root: Path) -> int:
    """Delete orphaned fetch temps from BASE_ROOT at boot; return the count.

    A fetch that crashed before its atomic rename leaves a uniquely-named
    `_TEMP_PREFIX...(_TEMP_SUFFIX)` temp (the streamed tarball or the
    extracted-squashfs mkstemp). The final served file is `base-<tag>.squashfs`
    (no leading dot), which by construction never matches this prefix, so the
    sweep can never remove a live served file -- only crash orphans."""
    removed = 0
    for entry in Path(base_root).iterdir():
        if (
            entry.name.startswith(_TEMP_PREFIX)
            and entry.name.endswith(_TEMP_SUFFIX)
            and entry.is_file()
        ):
            entry.unlink(missing_ok=True)
            removed += 1
    return removed


# -- garbage collection (bead 4) ---------------------------------------------


def gc_base_cache(conn, base_root: Path, *, clock) -> list[str]:
    """Evict cache bytes whose tag has left the keep-set; return evicted tags.

    keep = {latest-verified} U {non-retired pins} U {non-retired known-good} U
    {rows currently `caching`}. For each `cached` row NOT in keep: unlink
    `base-<tag>.squashfs` and set `state='evicted'` with an `eviction_reason`
    (the row and its integrity sha survive -- a later need re-fetches). An
    in-use tag is NEVER evicted. Safety notes carried from the design:
      * uniform `retired_at IS NULL` on every device term, so a decommissioned
        device pins no bytes forever;
      * an in-flight `caching` tag is kept so GC never races the fetch landing
        its bytes;
      * open-fd-safe: a serve already streaming an unlinked file completes;
      * decision + eventual under READ COMMITTED -- a tag needed AFTER the
        keep-set read may still be unlinked, then re-fetched on next need (a
        transient 503 + reboot-retry, never a wrong or torn serve).
    """
    keep = _nonretired_device_tags(conn)
    verified = latest_verified(conn)
    if verified is not None:
        keep.add(verified)
    keep.update(
        row["tag"]
        for row in conn.execute("SELECT tag FROM base_cache WHERE state='caching'").fetchall()
    )
    cached = conn.execute("SELECT tag FROM base_cache WHERE state='cached'").fetchall()
    evicted: list[str] = []
    for row in cached:
        tag = row["tag"]
        if tag in keep:
            continue
        base_file_path(base_root, tag).unlink(missing_ok=True)
        conn.execute(
            "UPDATE base_cache SET state='evicted', eviction_reason=%s, updated_at=%s "
            "WHERE tag=%s",
            (_GC_EVICTION_REASON, clock.utc(), tag),
        )
        evicted.append(tag)
    return evicted


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
