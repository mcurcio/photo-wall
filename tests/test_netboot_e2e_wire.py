"""Process-level end-to-end for the netboot base/squashfs path: the REAL client
(`appliance.netboot_init` + the REAL `appliance.provision.AppFetcher`) against a
REAL Central (uvicorn) + REAL Postgres, over a real socket.

This closes the gap the unit + TestClient tests leave: there, the client used a
mocked fetcher and the server used an in-memory TestClient, so the two halves
never met over the wire. Here the fetch is a genuine HTTP round trip to a live
uvicorn server on an ephemeral 127.0.0.1 port.

NOT a kernel/QEMU boot (owner's hardware bench, out of scope): the ram/mount ops
are injected exactly as the unit test does, since we are not pivoting a real
root -- but everything up to and including the download + digest verification is
real. It also chains the app `.deb` fetch via the existing Bootstrapper tracer
path in the same run, so one flow proves connect -> download runtime (squashfs)
-> download package (.deb). The REAL apt-install of a real `.deb` remains the
docker-compose tracer's job (scripts/test_netboot_e2e.py); here the package is a
synthetic blob because this test proves the WIRE contract (fetch + streamed
sha256 verify), not dependency resolution.

Runs under the DB harness (scripts/test_local.py / PHOTO_WALL_TEST_DATABASE_URL);
it skips only when no Postgres is configured, like every other DB-backed test --
never a skip-by-default false green.
"""

import asyncio
import contextlib
import hashlib
import http.server
import json
import threading
import time
import urllib.request

import pytest
import uvicorn

from appliance.bootstrap import read_pi_serial
from appliance.netboot_init import NetbootError, netboot
from appliance.provision import (
    Bootstrapper,
    ProvisionError,
    fetch_manifest,
    fetch_package,
)
from central.app import create_app
from central.app_packages import AppPackages
from central.app_release_queue import QueueReceipt
from central.app_releases import AppReleases
from central.netboot_base import SERIAL_HEADER, base_file_path, device_id_for_serial
from scripts.test_netboot_e2e import _FixedDiscovery, _NoSleep  # reuse tracer helpers

ADMIN = "e2e-netboot-admin-" + "x" * 32
SQUASHFS = b"rpi-image-gen base squashfs payload, streamed over a real socket" * 64
SERIAL_BYTES = b"10000000cafef00d\x00"          # devicetree serial-number shape
SERIAL = "10000000cafef00d"
TAG = "v9.9.9"


class _FakeQueue:
    """No procrastinate schema in the wire harness; the happy path never enqueues
    (cached + pinned) and the uncached case only records the enqueue."""

    def __init__(self):
        self.base_fetches = []

    def enqueue_base_fetch_in(self, conn, tag):
        self.base_fetches.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_mirror_in(self, conn, tag):  # pragma: no cover
        return QueueReceipt(coalesced=False)

    def enqueue_poll_in(self, conn):  # pragma: no cover
        return QueueReceipt(coalesced=False)


class _Log:
    """Captures ConsoleLog lines instead of touching /dev/console."""

    debug = False

    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(message)

    def detail(self, message):
        pass


class _Ops:
    """Injected ram/mount ops (as the unit test does) -- no real root pivot; the
    HTTP fetch that feeds `mount_root` is real."""

    def __init__(self, run_root):
        self.run_root = run_root
        self.calls = []
        self.mounted = []

    def configure_networking(self):
        self.calls.append("configure_networking")

    def network_info(self):
        return {"ip": "127.0.0.1"}

    def resolve(self, host):
        self.calls.append(("resolve", host))
        return ["127.0.0.1"]

    def ram(self):
        path = self.run_root / "ram"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def mount_root(self, image, rootmnt):
        self.mounted.append((image.read_bytes(), rootmnt))


class _InstallCapture:
    """Chain-flow install seam: records the package bytes/sha the Bootstrapper
    hands off after its streamed sha256 verify. The real apt-install is the
    docker-compose tracer's job; here we prove the fetch+verify over the wire."""

    def __init__(self):
        self.sha = None
        self.starts = 0

    def install(self, package, manifest):
        self.sha = hashlib.sha256(package).hexdigest()

    def start_unit(self):
        self.starts += 1


def _seed_base(registry, base_root, *, tag=TAG, squashfs=SQUASHFS, pin_serial=SERIAL):
    """Cache one version's base for a pinned device: the release row, a cached
    base_cache row (Digest = sha of the bytes), the per-version file, and a pin so
    the serial resolves deterministically to it."""
    base_root.mkdir(parents=True, exist_ok=True)
    base_file_path(base_root, tag).write_bytes(squashfs)
    sha = hashlib.sha256(squashfs).hexdigest()
    db, clock = registry.db, registry.clock
    AppReleases(db, clock).upsert_discovered(
        tag,
        asset_sha256="a" * 64,
        asset_size=10,
        asset_url="https://example.test/app.deb",
        base_tarball_sha256="b" * 64,
        base_tarball_size=len(squashfs),
        base_tarball_url="https://example.test/base.tgz",
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO base_cache(tag,state,squashfs_sha256,size,updated_at) "
            "VALUES(%s,'cached',%s,%s,%s)",
            (tag, sha, len(squashfs), clock.utc()),
        )
        conn.execute(
            "INSERT INTO devices(device_id,first_seen,last_seen,attached_tag) VALUES(%s,%s,%s,%s)",
            (device_id_for_serial(pin_serial), clock.utc(), clock.utc(), tag),
        )
    return sha


def _seed_release_only(registry, *, tag=TAG, pin_serial=SERIAL):
    """A pinned device resolving to a discovered-but-uncached tag: serving 503s."""
    db, clock = registry.db, registry.clock
    AppReleases(db, clock).upsert_discovered(
        tag,
        asset_sha256="a" * 64,
        asset_size=10,
        asset_url="https://example.test/app.deb",
        base_tarball_sha256="b" * 64,
        base_tarball_size=len(SQUASHFS),
        base_tarball_url="https://example.test/base.tgz",
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO devices(device_id,first_seen,last_seen,attached_tag) VALUES(%s,%s,%s,%s)",
            (device_id_for_serial(pin_serial), clock.utc(), clock.utc(), tag),
        )


@contextlib.contextmanager
def _serve(app):
    """Run `app` under a real uvicorn server on an ephemeral port; yield origin."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started or not server.servers:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start in time")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)


def _serial_reader(tmp_path):
    path = tmp_path / "serial-number"
    path.write_bytes(SERIAL_BYTES)
    return lambda: read_pi_serial(str(path))


def _post_json(origin, path, body, token):
    request = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode(),
        method="POST" if path.endswith("/app") else "PUT",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        return response.status


def test_real_client_fetches_runtime_then_package_over_the_wire(registry, tmp_path, monkeypatch):
    base_root = tmp_path / "base"
    app_root = tmp_path / "app"
    app_root.mkdir()
    _seed_base(registry, base_root)

    # Instrument the selection seam so we can assert the serial actually reached
    # the SERVER (not just that the client sent a header) -- central.app calls
    # the module-global name, so patching it here is seen by the live route.
    import central.app as appmod

    seen = {}
    real_select = appmod.select_base_for_serial

    def spy(conn, serial, **kwargs):
        seen["serial"] = serial
        return real_select(conn, serial, **kwargs)

    monkeypatch.setattr(appmod, "select_base_for_serial", spy)

    app = create_app(
        registry.db, registry.clock, ADMIN,
        app_root=app_root, base_root=base_root, release_queue=_FakeQueue(),
    )
    ops = _Ops(tmp_path / "run")
    with _serve(app) as origin:
        # --- Phase 1: REAL netboot base fetch over the wire ---
        netboot(
            {"photowall.central": origin + "/"},
            tmp_path / "root",
            ops=ops,
            serial_reader=_serial_reader(tmp_path),
            log=_Log(),
        )
        # URL composition resolved to the live route (the request produced the
        # bytes AND ran the server-side route), not a string-equality assert.
        assert ("resolve", "127.0.0.1") in ops.calls
        assert ops.mounted and ops.mounted[0][0] == SQUASHFS      # runtime downloaded
        assert seen["serial"] == SERIAL                            # serial on the wire, server-side

        # --- Phase 2: chain the app .deb fetch via the Bootstrapper path ---
        payload = b"synthetic photo-wall-player package bytes" * 32
        sha = hashlib.sha256(payload).hexdigest()
        (app_root / f"app-{sha}.deb").write_bytes(payload)
        assert _post_json(origin, "/v1/operator/app",
                          {"version": "9.9.9", "sha256": sha, "size": len(payload)}, ADMIN) == 201
        assert _post_json(origin, "/v1/operator/app/current", {"sha256": sha}, ADMIN) == 200

        capture = _InstallCapture()
        bootstrapper = Bootstrapper(
            discovery=_FixedDiscovery(origin),
            fetch_manifest=lambda o: fetch_manifest(o),
            fetch_package=lambda o, m: fetch_package(o, m),
            install=capture.install,
            write_origin=lambda o: None,
            start_unit=capture.start_unit,
            sleep=_NoSleep(),
        )
        assert asyncio.run(bootstrapper.run(max_attempts=1)) is True
        assert capture.sha == sha                                  # package downloaded + verified
        assert capture.starts == 1


def _mirror_deb(registry, tag):
    """Register + mirror a `.deb` for `tag` so /v1/netboot/manifest resolves it."""
    deb_sha = hashlib.sha256(("deb-" + tag).encode()).hexdigest()
    AppPackages(registry.db, registry.clock).register(version=tag, sha256=deb_sha, size=4096)
    AppReleases(registry.db, registry.clock).mark_mirrored(tag, deb_sha)
    return deb_sha


def _enroll_device(registry, serial, token, *, epoch=1):
    device_id = device_id_for_serial(serial)
    player_id = "p-" + hashlib.sha256(device_id.encode()).hexdigest()[:32]
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO players(id,public_key,token_hash,authority_epoch,registered_at,"
            "last_seen,device_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (player_id, "pk-" + device_id, token_hash, epoch,
             registry.clock.utc(), registry.clock.utc(), device_id),
        )


def _get_json(origin, path, *, serial=None):
    headers = {SERIAL_HEADER: serial} if serial else {}
    request = urllib.request.Request(origin + path, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        return json.loads(response.read())


def _post_base_health(origin, body, token):
    request = urllib.request.Request(
        origin + "/v1/player/base-health",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        return response.status, json.loads(response.read())


def test_base_health_advances_frontier_with_the_served_tag_over_the_wire(registry, tmp_path):
    # (criterion b + c) The FULL per-device arc over a real socket:
    #   1. a real base serve records `last_served_tag = TAG` on the 200;
    #   2. the REAL appliance client (provision.fetch_manifest, serial header)
    #      fetches /v1/netboot/manifest and LEARNS the served tag from the
    #      response -- proof the reported tag is the served tag, not a guess;
    #   3. base-health on that learned tag validates (`running_tag ==
    #      last_served_tag`) and advances known-good, moving latest-verified.
    # Un-over-mocked: real uvicorn + Postgres, real fetch client, real HTTP.
    base_root = tmp_path / "base"
    _seed_base(registry, base_root)          # release + cached base + pin(SERIAL->TAG)
    _mirror_deb(registry, TAG)               # so the per-device manifest resolves
    token = "wire-e2e-token-" + "z" * 32
    _enroll_device(registry, SERIAL, token)

    ops = _Ops(tmp_path / "run")
    app = create_app(registry.db, registry.clock, ADMIN,
                     base_root=base_root, release_queue=_FakeQueue())
    with _serve(app) as origin:
        # (1) real base serve over the wire -> records last_served_tag = TAG.
        netboot({"photowall.central": origin + "/"}, tmp_path / "root", ops=ops,
                serial_reader=_serial_reader(tmp_path), log=_Log())
        assert ops.mounted and ops.mounted[0][0] == SQUASHFS

        # (2) REAL appliance manifest client learns the served tag over the wire.
        manifest = fetch_manifest(origin, serial=SERIAL)
        assert manifest["tag"] == TAG          # the served tag, echoed by central
        assert manifest["sha256"] == hashlib.sha256(("deb-" + TAG).encode()).hexdigest()

        # (3) base-health with the LEARNED tag advances the frontier.
        status, accepted = _post_base_health(
            origin,
            {"authority_epoch": 1, "sequence": 1, "running_tag": manifest["tag"], "healthy": True},
            token,
        )
        assert status == 200 and accepted == {"accepted": True}

    with registry.db.transaction() as conn:
        row = conn.execute("SELECT known_good_tag, boot_outcome FROM devices WHERE device_id=%s",
                           (device_id_for_serial(SERIAL),)).fetchone()
    assert row["known_good_tag"] == TAG        # frontier advanced on real evidence
    assert row["boot_outcome"] == "healthy"


def test_base_health_with_a_guessed_tag_is_rejected_over_the_wire(registry, tmp_path):
    # The negative: a tag that is NOT the served tag never advances the frontier
    # (central validates running_tag == last_served_tag). Proves criterion (b)'s
    # guarantee is enforced server-side, not merely honored by a cooperative client.
    base_root = tmp_path / "base"
    _seed_base(registry, base_root)
    _mirror_deb(registry, TAG)
    token = "wire-e2e-token-" + "y" * 32
    _enroll_device(registry, SERIAL, token)
    ops = _Ops(tmp_path / "run")
    app = create_app(registry.db, registry.clock, ADMIN,
                     base_root=base_root, release_queue=_FakeQueue())
    with _serve(app) as origin:
        netboot({"photowall.central": origin + "/"}, tmp_path / "root", ops=ops,
                serial_reader=_serial_reader(tmp_path), log=_Log())
        status, accepted = _post_base_health(
            origin,
            {"authority_epoch": 1, "sequence": 1, "running_tag": "v0.0.1", "healthy": True},
            token,
        )
        assert status == 200 and accepted == {"accepted": False}
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT known_good_tag FROM devices WHERE device_id=%s",
                           (device_id_for_serial(SERIAL),)).fetchone()
    assert row["known_good_tag"] is None       # a guessed tag never advances known-good


def test_real_central_corruption_fails_closed_over_the_wire(registry, tmp_path):
    base_root = tmp_path / "base"
    _seed_base(registry, base_root)
    app = create_app(
        registry.db, registry.clock, ADMIN, base_root=base_root, release_queue=_FakeQueue()
    )
    ops = _Ops(tmp_path / "run")
    with _serve(app) as origin:
        # Tamper the served bytes WITHOUT touching the recorded base_cache sha:
        # central serves the tampered bytes with the stored (now-mismatching)
        # Digest header, and the client's streamed sha256 must refuse.
        base_file_path(base_root, TAG).write_bytes(SQUASHFS + b"tampered")
        with pytest.raises(NetbootError, match="netboot_integrity"):
            netboot(
                {"photowall.central": origin + "/"},
                tmp_path / "root",
                ops=ops,
                serial_reader=_serial_reader(tmp_path),
                log=_Log(),
            )
        assert ops.mounted == []


def test_real_central_uncached_tag_yields_503_over_the_wire(registry, tmp_path):
    # Real Central fails closed (503) for a discovered-but-uncached tag, so the
    # client sees a transport-level ProvisionError rather than a Digest-less 200.
    base_root = tmp_path / "base"
    base_root.mkdir()
    _seed_release_only(registry)  # release + pin, but no cached bytes
    app = create_app(
        registry.db, registry.clock, ADMIN, base_root=base_root, release_queue=_FakeQueue()
    )
    ops = _Ops(tmp_path / "run")
    with _serve(app) as origin:
        with pytest.raises(ProvisionError):
            netboot(
                {"photowall.central": origin + "/"},
                tmp_path / "root",
                ops=ops,
                serial_reader=_serial_reader(tmp_path),
                log=_Log(),
            )
        assert ops.mounted == []


class _NoDigestHandler(http.server.BaseHTTPRequestHandler):
    """A minimal stand-in that returns a 200 body with NO Digest header.

    The REAL Central structurally cannot emit this -- it 503s when it has no
    stored digest -- so the missing-Digest fail-closed branch is exercised
    against a minimal server over a real socket: proof the client refuses a
    Digest-less 200 that a buggy or hostile server might send."""

    def do_GET(self):  # noqa: N802 (stdlib handler contract)
        self.send_response(200)
        self.send_header("Content-Length", str(len(SQUASHFS)))
        self.end_headers()
        self.wfile.write(SQUASHFS)

    def log_message(self, *_args):
        pass


def test_missing_digest_header_fails_closed_over_the_wire(tmp_path):
    server = http.server.HTTPServer(("127.0.0.1", 0), _NoDigestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        ops = _Ops(tmp_path / "run")
        with pytest.raises(NetbootError, match="netboot_no_digest"):
            netboot(
                {"photowall.central": origin + "/"},
                tmp_path / "root",
                ops=ops,
                serial_reader=_serial_reader(tmp_path),
                log=_Log(),
            )
        assert ops.mounted == []
    finally:
        server.shutdown()
        thread.join(timeout=10)
