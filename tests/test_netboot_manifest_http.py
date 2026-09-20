"""HTTP composition of the 0012 per-device `.deb` manifest route (central + Postgres).

The per-device `.deb` (bead 5, F4) rides the exact tag whose base bytes the
device was ACTUALLY served this boot -- `devices.last_served_tag` -- never a
fresh latest-verified re-resolve. So base and `.deb` cannot diverge even if the
frontier moved between the base serve and the `.deb` fetch, and a recovery boot
(served its known-good) resolves its `.deb` to that same known-good tag.

The route is additive and serial-keyed, symmetric to /v1/netboot/base. 0010's
global GET /v1/app/manifest route and its promoted_tag/current_sha256/reconcile
machinery are UNTOUCHED -- criterion (c) below is the regression guard on that.
"""

import hashlib

from fastapi.testclient import TestClient

from central.app import create_app
from central.app_packages import AppPackages
from central.app_release_queue import QueueReceipt
from central.app_releases import AppReleases
from central.netboot_base import SERIAL_HEADER, device_id_for_serial

ADMIN = "netboot-manifest-http-operator-" + "x" * 32
SERIAL = "10000000abcd1234"


class _FakeQueue:
    """Faithful to the AppReleaseTaskQueue contract: every enqueue_* returns a
    QueueReceipt (never None/Mock). Records mirror enqueues for assertions."""

    def __init__(self):
        self.mirrors = []

    def enqueue_mirror_in(self, conn, tag):
        self.mirrors.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_base_fetch_in(self, conn, tag):  # pragma: no cover
        return QueueReceipt(coalesced=False)

    def enqueue_poll_in(self, conn):  # pragma: no cover
        return QueueReceipt(coalesced=False)


def _deb_sha(tag):
    return hashlib.sha256(("deb-bytes-for-" + tag).encode()).hexdigest()


def _seed_mirrored(registry, tag, *, size=4096):
    """A release whose `.deb` is mirrored: the release row, its bytes registered
    in app_packages, and mirrored_sha256 linked. Returns the `.deb` sha256."""
    sha = _deb_sha(tag)
    releases = AppReleases(registry.db, registry.clock)
    releases.upsert_discovered(
        tag,
        asset_sha256=sha,
        asset_size=size,
        asset_url="https://example.test/app-" + tag + ".deb",
    )
    AppPackages(registry.db, registry.clock).register(version=tag, sha256=sha, size=size)
    releases.mark_mirrored(tag, sha)
    return sha


def _seed_discovered_unmirrored(registry, tag, *, size=4096):
    """A deployable release whose `.deb` is NOT yet mirrored (mirrored_sha256 NULL)."""
    AppReleases(registry.db, registry.clock).upsert_discovered(
        tag,
        asset_sha256=_deb_sha(tag),
        asset_size=size,
        asset_url="https://example.test/app-" + tag + ".deb",
    )


def _device(registry, serial, **cols):
    """Insert a devices row keyed on the serial's device_id with the given columns."""
    keys = ["device_id", "first_seen", "last_seen", *cols.keys()]
    values = [device_id_for_serial(serial), registry.clock.utc(), registry.clock.utc(), *cols.values()]
    placeholders = ",".join(["%s"] * len(values))
    with registry.db.transaction() as conn:
        conn.execute(
            f"INSERT INTO devices({','.join(keys)}) VALUES({placeholders})", tuple(values)
        )


def _app(registry, queue=None):
    return create_app(
        registry.db,
        registry.clock,
        ADMIN,
        app_root=None,
        release_queue=queue or _FakeQueue(),
    )


# -- (a) base tag == .deb tag even when latest-verified has moved on -----------


def test_deb_rides_last_served_tag_not_a_moved_frontier(registry):
    # The device was SERVED base tag T (last_served_tag=T). Since then another
    # device reported healthy on T2, so latest-verified is now T2. The per-device
    # `.deb` must resolve to T's mirrored `.deb`, NOT T2's -- base tag == .deb tag
    # == T. (Mutation probe 20 / F4: re-resolving live off latest-verified would
    # return T2's sha and fail this assertion.)
    served_sha = _seed_mirrored(registry, "v1.2.3")
    frontier_sha = _seed_mirrored(registry, "v2.0.0")
    assert served_sha != frontier_sha
    _device(registry, SERIAL, last_served_tag="v1.2.3", boot_outcome="pending")
    # A different device makes latest-verified = v2.0.0.
    _device(registry, "20000000ffff0000", known_good_tag="v2.0.0", known_good_at=registry.clock.utc())

    with TestClient(_app(registry)) as client:
        response = client.get("/v1/netboot/manifest", headers={SERIAL_HEADER: SERIAL})
    assert response.status_code == 200
    body = response.json()
    # base tag == .deb tag == T: the served sha is T's, never the frontier's T2.
    assert body["sha256"] == served_sha
    assert body["sha256"] != frontier_sha
    assert body["version"] == "v1.2.3"


# -- (b) recovery boot: .deb follows the served known-good, not the failed tag --


def test_recovery_boot_deb_follows_known_good_not_failed_tag(registry):
    # On a recovery boot Central served the device's known-good K; last_served_tag
    # was recorded as K, while failed_tag stays at the failing desired tag T2. The
    # `.deb` must resolve to K's mirrored `.deb`, not the failed/desired tag's --
    # base and `.deb` both K, so the recovery boot can confirm itself healthy.
    known_good_sha = _seed_mirrored(registry, "v1.0.0")
    failed_sha = _seed_mirrored(registry, "v2.0.0")
    _device(
        registry,
        SERIAL,
        known_good_tag="v1.0.0",
        known_good_at=registry.clock.utc(),
        last_served_tag="v1.0.0",  # recovery serve recorded the known-good tag
        boot_outcome="pending",
        failed_tag="v2.0.0",  # the fenced failing target (sticky)
    )
    with TestClient(_app(registry)) as client:
        response = client.get("/v1/netboot/manifest", headers={SERIAL_HEADER: SERIAL})
    assert response.status_code == 200
    body = response.json()
    assert body["sha256"] == known_good_sha
    assert body["sha256"] != failed_sha


# -- (c) 0010 global GET /v1/app/manifest is UNCHANGED (regression guard) -------


def test_global_manifest_still_resolves_via_promoted_pointer_untouched(registry):
    # Regression guard on the untouched 0010 path: /v1/app/manifest resolves via
    # the promoted global `current()` pointer, independent of ANY device's
    # last_served_tag. Repurposing it to read the per-device tag (touching 0010)
    # would return the per-device sha and fail this test.
    global_sha = _seed_mirrored(registry, "v3.0.0")
    AppPackages(registry.db, registry.clock).promote(global_sha)  # 0010 current pointer
    per_device_sha = _seed_mirrored(registry, "v1.2.3")
    _device(registry, SERIAL, last_served_tag="v1.2.3", boot_outcome="pending")

    with TestClient(_app(registry)) as client:
        # Global route ignores the serial entirely and returns the promoted sha,
        # both with and without the header present.
        plain = client.get("/v1/app/manifest")
        with_serial = client.get("/v1/app/manifest", headers={SERIAL_HEADER: SERIAL})
    assert plain.status_code == 200 and with_serial.status_code == 200
    assert plain.json()["sha256"] == global_sha
    assert with_serial.json()["sha256"] == global_sha
    assert plain.json()["sha256"] != per_device_sha


# -- (d) never-served device: no carried tag => no per-device answer (503) ------


def test_never_served_device_is_503_unresolved(registry):
    # A device row exists but has never been served a base on a 200
    # (last_served_tag IS NULL): there is no carried tag, so no per-device answer.
    # Fail closed -- must NOT fall back to a global pointer that could diverge.
    _seed_mirrored(registry, "v1.2.3")
    _device(registry, SERIAL)  # no last_served_tag
    with TestClient(_app(registry)) as client:
        response = client.get("/v1/netboot/manifest", headers={SERIAL_HEADER: SERIAL})
    assert response.status_code == 503
    assert response.json() == {"error": "app_manifest_unresolved"}


def test_unknown_device_and_absent_serial_are_503_unresolved(registry):
    _seed_mirrored(registry, "v1.2.3")
    with TestClient(_app(registry)) as client:
        # No device row was ever created for this serial.
        unknown = client.get("/v1/netboot/manifest", headers={SERIAL_HEADER: SERIAL})
        # No serial header at all.
        absent = client.get("/v1/netboot/manifest")
    assert unknown.status_code == 503 and unknown.json() == {"error": "app_manifest_unresolved"}
    assert absent.status_code == 503 and absent.json() == {"error": "app_manifest_unresolved"}


# -- carried tag not yet mirrored: 503 + lazy mirror enqueue -------------------


def test_carried_tag_unmirrored_is_503_and_enqueues_mirror(registry):
    # The device was served base tag T (so last_served_tag=T) but T's `.deb` is
    # not yet mirrored: 503 + enqueue the mirror (lazy backstop), never a wrong
    # tag. The Pi retries on reboot once the mirror lands.
    _seed_discovered_unmirrored(registry, "v1.2.3")
    _device(registry, SERIAL, last_served_tag="v1.2.3", boot_outcome="pending")
    queue = _FakeQueue()
    with TestClient(_app(registry, queue)) as client:
        response = client.get("/v1/netboot/manifest", headers={SERIAL_HEADER: SERIAL})
    assert response.status_code == 503
    assert response.json() == {"error": "app_manifest_uncached"}
    assert queue.mirrors == ["v1.2.3"]  # coalesced by the tag lock in the real queue
