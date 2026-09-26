"""Process-level end-to-end for the netboot base/squashfs path: the REAL stage 1
(`appliance.netboot_init` over the REAL `uplink` transport, locate and direct fetch)
against a REAL Central (uvicorn) + REAL Postgres, over a real socket -- once through a
gateway's 301 to Central over verified TLS (decision 0014), and plain http for the
chained app `.deb` flow (`appliance.provision.AppFetcher`, Project 2's to migrate).

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

P2: Central serves through the content catalog and the asset read path, built by
the production wiring (`build_content_services`). The fixtures seed what a
release sync and a finished fetch leave behind -- the release row, the Asset
record with its produced facts, the file at its cache path -- instead of the
retired `base_cache`/`app_packages` rows.

Runs under the DB harness (scripts/test_local.py / PHOTO_WALL_TEST_DATABASE_URL);
it skips only when no Postgres is configured, like every other DB-backed test --
never a skip-by-default false green.
"""

import asyncio
import contextlib
import dataclasses
import hashlib
import http.server
import json
import threading
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
import uvicorn

from appliance.bootstrap import read_pi_serial
from appliance.netboot_init import NetbootError, netboot
from appliance.provision import (
    Bootstrapper,
    fetch_manifest,
    fetch_package,
)
from central.app import create_app
from central.assets.layout import CacheLayout
from central.assets.reader import AssetReader, WaiterSlots
from central.assets.store import CacheStore
from central.content_catalog.catalog import device_id_for_serial
from central.content_wiring import build_content_services
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgReleaseRecords
from central.infra.outcomes import JobOutcomes
from central.infra.publisher import ProcrastinatePublisher
from central.infra.transactions import PgTransactions, pg_connection
from central.kernel.assets import AssetReady, AssetReference, OriginLocator
from central.kernel.job_types import FetchOsImage, FetchPackage
from central.kernel.jobs import asset_key
from central.kernel.ports import PublishedRelease
from contracts.clock_record import ClockRecord, ClockState
from scripts.test_netboot_e2e import (  # reuse tracer helpers
    _FixedDiscovery,
    _NoSleep,
    promote_path,
    release_seed_sql,
)
from tests import tls_fixture as tls
from uplink.causes import Cause, UplinkError
from uplink.transport import HttpTransport
from uplink.trust import Trust

ADMIN = "e2e-netboot-admin-" + "x" * 32
SQUASHFS = b"rpi-image-gen base squashfs payload, streamed over a real socket" * 64
SERIAL_BYTES = b"10000000cafef00d\x00"          # devicetree serial-number shape
SERIAL = "10000000cafef00d"
TAG = "v9.9.9"
DEB_SHA = hashlib.sha256(("deb-" + TAG).encode()).hexdigest()  # TAG's `.deb` (manifest only)
TARBALL_SHA = "b" * 64  # TAG's base tarball: the OS image's key


class _Log:
    """Captures ConsoleLog lines instead of touching /dev/console."""

    debug = False

    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(message)

    def detail(self, message):
        pass


class _Keeper:
    """A `Keeper` double (S0-AC6b): every netboot run here passes one (through
    `_netboot`), and one test asserts `hand_over` runs only after the real
    download and mount."""

    def __init__(self):
        self.pets = 0
        self.handed_over = False

    def pet(self):
        self.pets += 1

    def paced(self, blocks):
        for block in blocks:
            yield block
            self.pet()

    def hand_over(self):
        self.handed_over = True

    @property
    def summary(self):
        return "armed device=/dev/watchdog0 timeout=124s"


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


def _seed_base(registry, cache_root, *, tag=TAG, squashfs=SQUASHFS, cached=True,
               pin_serial=SERIAL):
    """What a release sync plus a finished FetchOsImage leave behind, for a pinned device.

    The release row (with its `.deb` and base-tarball locators), the os-image Asset
    with its reference and -- when `cached` -- its produced facts and the file at its
    cache path, and a device pinned to `tag` so the serial resolves deterministically."""
    db, clock = registry.db, registry.clock
    tarball = OriginLocator("https://example.test/base.tgz", TARBALL_SHA, len(squashfs))
    package = OriginLocator("https://example.test/app.deb", DEB_SHA, 4096)
    key = asset_key(FetchOsImage(tarball_sha256=TARBALL_SHA))
    assets = PgAssetRecords(clock)
    with PgTransactions(db).begin() as tx:
        PgReleaseRecords().claim(tx, PublishedRelease(tag, False, package, None, tarball, None),
                                 now=clock.utc())
        assets.reference(tx, key, AssetReference(tag, tarball, None, None))
        if cached:
            assets.record_produced(
                tx, key, AssetReady(len(squashfs), hashlib.sha256(squashfs).hexdigest()))
        pg_connection(tx).execute(
            "INSERT INTO devices(device_id,first_seen,last_seen,attached_tag) VALUES(%s,%s,%s,%s)",
            (device_id_for_serial(pin_serial), clock.utc(), clock.utc(), tag),
        )
    if cached:
        _write(cache_root, key, squashfs)


def _write(cache_root, key, data):
    path = CacheLayout(cache_root).path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _app(registry, cache_root, *, wait=None):
    """The real Central over the production content wiring; `wait` shortens the
    read-through wait (30s in production) for the miss case."""
    db, clock = registry.db, registry.clock
    content = build_content_services(db, clock, cache_root=cache_root)
    if wait is not None:
        transactions, assets = PgTransactions(db), PgAssetRecords(clock)
        publisher = ProcrastinatePublisher(db.dsn, transactions=transactions,
                                           outcomes=JobOutcomes(), assets=assets, clock=clock,
                                           feed=content.feed)
        content = dataclasses.replace(content, reader=AssetReader(
            store=CacheStore(CacheLayout(cache_root)), records=assets,
            transactions=transactions, publisher=publisher, slots=WaiterSlots(4), clock=clock,
            wait_timeout=wait))
    return create_app(db, clock, ADMIN, content=content)


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


class _ClockGate:
    """A settled clock: the SNTP gate is proven in tests/test_uplink_clock.py, not here."""

    def settle(self):
        return ClockRecord(state=ClockState.SYNCED, floor=1, raised_to_floor=False, tier=None,
                           source=None, offset=None, stepped=False, tried=(), writer="netboot",
                           written_at=time.time())


def _netboot(root: str, tmp_path: Path, ops: _Ops, *, keeper: _Keeper | None = None) -> None:
    """The real stage 1 from `root`: the real transport, trusting only the test CA."""
    transport = HttpTransport(trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA)))
    netboot({"photowall.central": root}, tmp_path / "root", ops=ops, transport=transport,
            clock_gate=_ClockGate(), keeper=keeper or _Keeper(),
            trust_provenance="bundle=sha256:test anchors=1 floor=1970-01-01",
            serial_reader=_serial_reader(tmp_path), log=_Log())


def _served_row(registry, serial=SERIAL):
    with registry.db.transaction() as conn:
        return conn.execute(
            "SELECT last_served_tag, boot_outcome, known_good_tag FROM devices "
            "WHERE device_id=%s", (device_id_for_serial(serial),)).fetchone()


def test_real_client_fetches_runtime_then_package_over_the_wire(registry, tmp_path):
    cache_root = tmp_path / "cache"
    _seed_base(registry, cache_root)

    app = _app(registry, cache_root)
    ops = _Ops(tmp_path / "run")
    with _serve(app) as origin:
        # --- Phase 1: REAL netboot base fetch over the wire ---
        _netboot(origin + "/", tmp_path, ops)
        assert ops.mounted and ops.mounted[0][0] == SQUASHFS      # runtime downloaded
        # The serial reached the SERVER, not just the client's header: the 200
        # recorded the served tag on the device row that serial derives.
        row = _served_row(registry)
        assert (row["last_served_tag"], row["boot_outcome"]) == (TAG, "pending")

        # --- Phase 2: chain the app .deb fetch via the Bootstrapper path ---
        # A promoted release whose `.deb` is produced and on disk: the compose
        # tracer's seed (scripts/test_netboot_e2e.py) and its promotion through the
        # operator route, proven here against a real schema.
        payload = b"synthetic photo-wall-player package bytes" * 32
        sha = hashlib.sha256(payload).hexdigest()
        with psycopg.connect(registry.db.dsn, autocommit=True) as conn:
            conn.execute(release_seed_sql("v9.9.8", sha, len(payload)))
        _write(cache_root, asset_key(FetchPackage(sha256=sha)), payload)
        promote = urllib.request.Request(origin + promote_path("v9.9.8"), method="POST",
                                         headers={"Authorization": "Bearer " + ADMIN})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(promote, timeout=15) as response:
            assert json.loads(response.read()) == {"status": "promoted"}

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
    cache_root = tmp_path / "cache"
    _seed_base(registry, cache_root)         # release (+ its .deb) + cached base + pin
    token = "wire-e2e-token-" + "z" * 32
    _enroll_device(registry, SERIAL, token)

    ops = _Ops(tmp_path / "run")
    app = _app(registry, cache_root)
    with _serve(app) as origin:
        # (1) real base serve over the wire -> records last_served_tag = TAG.
        _netboot(origin + "/", tmp_path, ops)
        assert ops.mounted and ops.mounted[0][0] == SQUASHFS

        # (2) REAL appliance manifest client learns the served tag over the wire.
        manifest = fetch_manifest(origin, serial=SERIAL)
        assert manifest["tag"] == TAG          # the served tag, echoed by central
        assert manifest["sha256"] == DEB_SHA

        # (3) base-health with the LEARNED tag advances the frontier.
        status, accepted = _post_base_health(
            origin,
            {"authority_epoch": 1, "sequence": 1, "running_tag": manifest["tag"], "healthy": True},
            token,
        )
        assert status == 200 and accepted == {"accepted": True}

    row = _served_row(registry)
    assert row["known_good_tag"] == TAG        # frontier advanced on real evidence
    assert row["boot_outcome"] == "healthy"


def test_base_health_with_a_guessed_tag_is_rejected_over_the_wire(registry, tmp_path):
    # The negative: a tag that is NOT the served tag never advances the frontier
    # (central validates running_tag == last_served_tag). Proves criterion (b)'s
    # guarantee is enforced server-side, not merely honored by a cooperative client.
    cache_root = tmp_path / "cache"
    _seed_base(registry, cache_root)
    token = "wire-e2e-token-" + "y" * 32
    _enroll_device(registry, SERIAL, token)
    ops = _Ops(tmp_path / "run")
    app = _app(registry, cache_root)
    with _serve(app) as origin:
        _netboot(origin + "/", tmp_path, ops)
        status, accepted = _post_base_health(
            origin,
            {"authority_epoch": 1, "sequence": 1, "running_tag": "v0.0.1", "healthy": True},
            token,
        )
        assert status == 200 and accepted == {"accepted": False}
    assert _served_row(registry)["known_good_tag"] is None  # a guessed tag never advances


def test_real_central_corruption_fails_closed_over_the_wire(registry, tmp_path):
    cache_root = tmp_path / "cache"
    _seed_base(registry, cache_root)
    app = _app(registry, cache_root)
    ops = _Ops(tmp_path / "run")
    with _serve(app) as origin:
        # Tamper the served bytes WITHOUT touching the recorded produced facts, at the
        # same length (the read path opens only a file of the recorded size): central
        # serves the tampered bytes with the recorded (now-mismatching) Digest header,
        # and the client's streamed sha256 must refuse.
        tampered = SQUASHFS[:-1] + bytes([SQUASHFS[-1] ^ 0x01])
        _write(cache_root, asset_key(FetchOsImage(tarball_sha256=TARBALL_SHA)), tampered)
        with pytest.raises(NetbootError, match="netboot_integrity"):
            _netboot(origin + "/", tmp_path, ops)
        assert ops.mounted == []


def test_real_central_uncached_tag_yields_503_over_the_wire(registry, tmp_path):
    # Real Central fails closed (503 after the read-through wait) for a known but
    # uncached tag, so the client names Central's own error rather than accepting a
    # Digest-less 200 -- and the miss published the tag's fetch for a worker to run.
    cache_root = tmp_path / "cache"
    _seed_base(registry, cache_root, cached=False)  # release + reference + pin, no bytes
    app = _app(registry, cache_root, wait=timedelta(seconds=1))
    ops = _Ops(tmp_path / "run")
    with _serve(app) as origin:
        with pytest.raises(UplinkError) as caught:
            _netboot(origin + "/", tmp_path, ops)
        assert ops.mounted == []
    assert (caught.value.cause, caught.value.reason) == (Cause.CENTRAL, "error")
    assert caught.value.central_error.startswith("base_")
    with registry.db.transaction() as conn:
        queued = conn.execute("SELECT task_name, args FROM procrastinate_jobs "
                              "WHERE status = 'todo'").fetchall()
    assert [(q["task_name"], q["args"]["tarball_sha256"]) for q in queued] == [
        ("photo_wall.os_image.fetch", TARBALL_SHA)]
    assert _served_row(registry)["last_served_tag"] is None  # a miss records nothing


class _NoDigestHandler(http.server.BaseHTTPRequestHandler):
    """A minimal stand-in whose base is a 200 body with NO Digest header; built
    on `tls_fixture.central_stub`, so it answers `/v1/locate` as Central does
    and the run reaches the base (S4b-AC3b).

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
    ops = _Ops(tmp_path / "run")
    with tls.serve_stub(tls.central_stub(_NoDigestHandler)) as stub:
        with pytest.raises(NetbootError, match="netboot_no_digest"):
            _netboot(f"http://127.0.0.1:{stub.port}/", tmp_path, ops)
    assert ops.mounted == []
    assert stub.requests == ["/v1/locate", "/v1/netboot/base"]


def test_real_netboot_locates_through_a_301_to_central_over_verified_tls(registry, tmp_path):
    # S4b-AC3: the Pi's real case -- an http root whose gateway 301s to Central's
    # https origin -- end to end: locate verifies the TLS certificate against the test
    # CA only, then the base is one direct request, digest-verified and "mounted".
    cache_root = tmp_path / "cache"
    _seed_base(registry, cache_root)
    ops, keeper = _Ops(tmp_path / "run"), _Keeper()
    with tls.serve_tls(_app(registry, cache_root), tls.CENTRAL) as port:
        with tls.redirect_stub(f"https://127.0.0.1:{port}/v1/locate") as gateway:
            _netboot(f"http://localhost:{gateway.port}/", tmp_path, ops, keeper=keeper)
    assert ops.mounted and ops.mounted[0][0] == SQUASHFS
    assert gateway.requests == ["/v1/locate"]          # the base never went through it
    # S0-AC6b: hand_over runs once, after the real download and mount.
    assert keeper.handed_over is True and keeper.pets > 0
    assert _served_row(registry)["last_served_tag"] == TAG
