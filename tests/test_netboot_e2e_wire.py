"""Process-level end-to-end over a real socket for the app `.deb` flow
(`appliance.provision.Bootstrapper` over `uplink.finder.find_central` and
`uplink.fetch.DirectFetch`) against a REAL Central (uvicorn) + REAL Postgres, and the real
stage 1 (`appliance.netboot_init`) refusing a Digest-less base from a stub.

Central serves no V1 netboot base any more (decision 0019): the node boot path's base is a
node offer's artifact. The REAL dpkg install of a real `.deb`, by the packaged provisioner in
the device root, remains the docker-compose tracer's job (scripts/test_netboot_e2e.py); here the
package is a synthetic blob because this test proves the WIRE contract (fetch + streamed sha256
verify), not the install. The tracer's own helpers that need no docker are tested at the end.

Runs under the DB harness (scripts/test_local.py / PHOTO_WALL_TEST_DATABASE_URL);
it skips only when no Postgres is configured, like every other DB-backed test --
never a skip-by-default false green.
"""

import asyncio
import contextlib
import dataclasses
import functools
import hashlib
import http.server
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import psycopg
import pytest
import tls_fixture as tls
import uvicorn

from appliance.bootstrap import read_pi_serial
from appliance.netboot_init import NetbootError, netboot
from appliance.provision import Bootstrapper
from central.app import create_app
from central.assets.layout import CacheLayout
from central.assets.reader import AssetReader, WaiterSlots
from central.assets.store import CacheStore
from central.content_wiring import build_content_services
from central.infra.asset_records import PgAssetRecords
from central.infra.outcomes import JobOutcomes
from central.infra.publisher import ProcrastinatePublisher
from central.infra.transactions import PgTransactions
from central.kernel.job_types import FetchPackage
from central.kernel.jobs import asset_key
from contracts.clock_record import ClockRecord, ClockState
from scripts import test_netboot_e2e as tracer
from scripts.debian_packages import DEVICE_CONSUMERS, mmdebstrap_argv
from scripts.test_netboot_e2e import (  # reuse tracer helpers
    _NoSleep,
    device_root_image,
    gateway_stub,
    import_smoke_script,
    promote_path,
    release_seed_sql,
)
from uplink.finder import find_central
from uplink.origin import Origin
from uplink.resolver import Configured
from uplink.transport import HttpTransport
from uplink.trust import Trust

REPO = Path(__file__).resolve().parents[1]
ADMIN = "e2e-netboot-admin-" + "x" * 32
SQUASHFS = b"rpi-image-gen base squashfs payload, streamed over a real socket" * 64
SERIAL_BYTES = b"10000000cafef00d\x00"          # devicetree serial-number shape
SERIAL = "10000000cafef00d"


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

    def hand_over_modules(self, rootmnt, *, pet, release=None):
        return "modules=none (no kernel modules in this harness)"


class _InstallCapture:
    """Chain-flow install seam: records the package bytes/sha the Bootstrapper
    hands off after its streamed sha256 verify. The real dpkg install is the
    docker-compose tracer's job; here we prove the fetch+verify over the wire."""

    def __init__(self):
        self.sha = None
        self.starts = 0

    def install(self, package, manifest):
        self.sha = hashlib.sha256(package).hexdigest()

    def start_unit(self):
        self.starts += 1


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


def _transport(tmp_path: Path) -> HttpTransport:
    """The real transport, trusting only the test CA."""
    return HttpTransport(trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA)))


def _netboot(root: str, tmp_path: Path, ops: _Ops, *, keeper: _Keeper | None = None) -> None:
    """The real stage 1 from `root`: the real transport, trusting only the test CA."""
    transport = _transport(tmp_path)
    netboot({"photowall.central": root}, tmp_path / "root", ops=ops, transport=transport,
            clock_gate=_ClockGate(), keeper=keeper or _Keeper(),
            trust_provenance="bundle=sha256:test anchors=1 floor=1970-01-01",
            serial_reader=_serial_reader(tmp_path), log=_Log())


def test_the_bootstrapper_fetches_the_promoted_package_over_the_wire(registry, tmp_path):
    cache_root = tmp_path / "cache"
    app = _app(registry, cache_root)
    with _serve(app) as origin:
        # A promoted release whose `.deb` is produced and on disk: the compose tracer's seed
        # (scripts/test_netboot_e2e.py) and its promotion through the operator route, proven
        # here against a real schema.
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
        transport = _transport(tmp_path)
        bootstrapper = Bootstrapper(
            find=functools.partial(find_central, Configured(Origin.parse_root(origin)),
                                   transport=transport),
            transport=transport,
            clock=None,
            install=capture.install,
            write_handoff=lambda handoff: None,
            start_unit=capture.start_unit,
            sleep=_NoSleep(),
        )
        assert asyncio.run(bootstrapper.run(max_attempts=1)) is True
        assert capture.sha == sha                                  # package downloaded + verified
        assert capture.starts == 1


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


# --- the compose tracer's helpers that need no docker ------------------------------------


def test_packaged_os_gate_refuses_v1_or_promoted_t0_claims():
    rows = [{"schema": 2, "sequence": sequence, "boot_id": tracer.PROBE_BOOT_ID,
             "phase": "base_ready" if sequence == 1 else "retry_wait",
             "fault": None if sequence == 1 else "app_integrity",
             "installed": None, "running": None,
             "installed_reason": "executor_not_initialized",
             "running_reason": "executor_not_initialized"}
            for sequence in (1, 2)]
    claim = {"state": "unknown", "source": "serial_claim", "assurance": "t0_unverified",
             "boot_id": tracer.PROBE_BOOT_ID, "digest": None}
    status = {"commands_available": False,
              "devices": [{"serial": tracer.PROBE_SERIAL,
                           "base": {"source": "serial_claim", "assurance": "t0_unverified",
                                    "boot_id": tracer.PROBE_BOOT_ID, "phase": "retry_wait",
                                    "fault_code": "app_integrity"},
                           "installed": claim, "running": claim,
                           "accepted_fallback": None,
                           "update_now": {"available": False}}]}
    central = SimpleNamespace(os_rows=lambda: rows, accepted_count=lambda: 0,
                              fleet=lambda: status)
    assert tracer.os_claim_evidence(central, sequence=2, phase="retry_wait",
                                    fault="app_integrity")["rows"] == rows
    bad_rows = deepcopy(rows)
    bad_rows[1]["schema"] = 1
    with pytest.raises(tracer.TracerError, match="os_check_in_app_claim_invalid"):
        tracer.os_claim_evidence(SimpleNamespace(os_rows=lambda: bad_rows,
                                                 accepted_count=lambda: 0,
                                                 fleet=lambda: status),
                                 sequence=2, phase="retry_wait", fault="app_integrity")
    with pytest.raises(tracer.TracerError, match="os_check_in_created_acceptance"):
        tracer.os_claim_evidence(SimpleNamespace(os_rows=lambda: rows,
                                                 accepted_count=lambda: 1,
                                                 fleet=lambda: status),
                                 sequence=2, phase="retry_wait", fault="app_integrity")


@pytest.mark.parametrize("diagnostic", ["packaged probe failed\n", ""])
def test_failed_packaged_probe_keeps_its_diagnostic_log(tmp_path, monkeypatch, diagnostic):
    log = tmp_path / "os-agent-before.log"

    def failed_command(argv, *, timeout, log):
        assert argv[:3] == ["docker", "exec", "device"]
        assert timeout == 60
        if diagnostic:
            log.write_text(diagnostic)
        raise tracer.TracerError("command_failed:docker")

    monkeypatch.setattr(tracer, "run", failed_command)
    device = tracer.DeviceRoot("image", tmp_path, "device")
    with pytest.raises(tracer.TracerError, match="command_failed:docker"):
        device.packaged_probe("report", "http://127.0.0.1:8000", log=log)
    assert log.read_text() == (diagnostic or "command_failed:docker\n")


def test_the_tracer_gateway_301s_every_path_to_central_by_another_name():
    """The tracer's command-line root: every request, same path, to `localhost` (a different
    host NAME from the 127.0.0.1 root), and nothing followed by the stub itself."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
    with gateway_stub(8123) as gateway:
        for target in ("/v1/locate", "/v1/app/manifest"):
            with pytest.raises(urllib.error.HTTPError) as answer:
                opener.open(f"http://127.0.0.1:{gateway.port}{target}", timeout=10)
            assert answer.value.code == 301
            assert answer.value.headers["Location"] == f"http://localhost:8123{target}"
    assert gateway.requests == ["/v1/locate", "/v1/app/manifest"]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class _Commands:
    """subprocess.run as device_root_image calls it: mmdebstrap writes a tar to its stdout,
    docker import reads it from stdin."""

    TAR = b"pretend device root tar"

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        if "mmdebstrap" in argv:
            self.calls.append(("build", argv))
            kwargs["stdout"].write(self.TAR)
        else:
            self.calls.append(("import", argv, kwargs["stdin"].read()))
        return subprocess.CompletedProcess(argv, 0, None if "mmdebstrap" in argv else "", "")


def test_the_device_root_is_built_at_the_pin_once_then_reimported(monkeypatch, tmp_path):
    """The one declaration builds the root: exactly mmdebstrap_argv over the device set (the
    base OS's, the bootstrapper's and the Player's packages), as root, to a tar that is kept; a
    second run only re-imports it."""
    commands = _Commands()
    monkeypatch.setattr(tracer.subprocess, "run", commands)
    cache = tmp_path / "cache"
    for _ in range(2):
        assert device_root_image(arch="arm64", tag="device-root:test", cache=cache) == (
            "device-root:test")
    expected = list(mmdebstrap_argv(arch="arm64", target="-", consumers=DEVICE_CONSUMERS))
    assert DEVICE_CONSUMERS == ("base-os", "bootstrapper", "player")
    [build, *imports] = commands.calls
    assert build[0] == "build" and build[1][len(build[1]) - len(expected):] == expected
    assert build[1][:len(build[1]) - len(expected)] in ([], ["sudo", "-n"])
    assert imports == [("import", ["docker", "import", "-", "device-root:test"], _Commands.TAR)] * 2
    [kept] = cache.iterdir()
    assert kept.name.startswith("device-root-arm64-") and kept.read_bytes() == _Commands.TAR


MMDEBSTRAP_STDERR = (
    "I: automatically chosen mode: root\n"
    "W: GPG error: https://snapshot.debian.org/archive/debian/20260904T000000Z trixie InRelease:"
    " NO_PUBKEY 762F67A0B2C39DE4\n"
    "E: The repository 'https://snapshot.debian.org/archive/debian/20260904T000000Z trixie "
    "InRelease' is not signed.\n"
    "E: apt-get update failed\n"
    "E: mmdebstrap failed to run\n")


def test_a_failed_device_root_build_leaves_no_tar_and_shows_its_whole_stderr(
        monkeypatch, tmp_path, capsys):
    """The PR #28 tracer showed only `E: mmdebstrap failed to run`: the cause is in the lines
    before it. A failed build echoes all of them into the CI log and keeps them in `log` (the
    workflow's artifact), besides leaving no tar."""
    def mmdebstrap_fails(argv, **kwargs):
        kwargs["stdout"].write(b"half a tar")
        return subprocess.CompletedProcess(argv, 1, None, MMDEBSTRAP_STDERR)

    monkeypatch.setattr(tracer.subprocess, "run", mmdebstrap_fails)
    cache, log = tmp_path / "cache", tmp_path / "logs" / "mmdebstrap.log"
    with pytest.raises(tracer.TracerError, match="mmdebstrap failed to run"):
        device_root_image(arch="arm64", tag="device-root:test", cache=cache, log=log)
    assert list(cache.iterdir()) == []
    assert log.read_text() == MMDEBSTRAP_STDERR
    assert MMDEBSTRAP_STDERR.rstrip("\n") in capsys.readouterr().err


def test_a_timed_out_device_root_build_keeps_what_it_printed(monkeypatch, tmp_path, capsys):
    def mmdebstrap_hangs(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1, output=None, stderr=b"I: downloading...\n")

    monkeypatch.setattr(tracer.subprocess, "run", mmdebstrap_hangs)
    log = tmp_path / "mmdebstrap.log"
    with pytest.raises(tracer.TracerError, match="command_failed"):
        device_root_image(arch="arm64", tag="device-root:test", cache=tmp_path / "c", log=log)
    assert log.read_text() == "I: downloading...\n"
    assert "I: downloading..." in capsys.readouterr().err


@pytest.mark.parametrize("workflow", ["base-image.yml", "netboot-e2e.yml"])
def test_every_step_of_the_boot_chain_workflows_fails_where_it_fails(workflow):
    """An explicit `shell: bash` is `bash -eo pipefail`; the default is `bash -e`, under which
    `image=$(failing | tail -n1)` succeeds (the PR #28 tracer's green-while-failed step). No step
    may override it."""
    text = (REPO / ".github/workflows" / workflow).read_text()
    assert "\ndefaults:\n  run:\n    shell: bash\n" in text
    shells = [line.split(":", 1)[1].strip() for line in text.splitlines()
              if line.strip().startswith("shell:")]
    assert shells == ["bash"]


def test_the_import_smoke_imports_every_closure_module_from_the_private_dir(tmp_path):
    """`import_smoke_script` names the private install dir and closure.json (not
    dist-packages), and a real run of it -- against a FAKE staged closure, since the dev venv
    carries no gi/OpenGL (`bindings=False`) -- imports every module closure.json lists and
    reports the Player module's file from under the private dir."""
    script = import_smoke_script(PurePosixPath("/usr/lib/photo-wall-player"))
    compile(script, "<smoke>", "exec")
    assert "/usr/lib/photo-wall-player" in script
    assert "closure.json" in script

    player_dir = tmp_path / "player"
    player_dir.mkdir()
    (player_dir / "__init__.py").write_text("")
    (player_dir / "service.py").write_text("")
    (tmp_path / "closure.json").write_text(json.dumps({
        "modules": ["player", "player.service"], "files": ["player/__init__.py",
        "player/service.py"], "forbidden": [], "digest": "test",
    }))

    fake_script = import_smoke_script(PurePosixPath(str(tmp_path)), bindings=False)
    result = subprocess.run([sys.executable, "-I", "-B", "-c", fake_script],
                            capture_output=True, text=True, timeout=30, check=True)
    printed = json.loads(result.stdout.strip().splitlines()[-1])
    assert printed["modules"] == 2
    assert printed["player_service_file"].startswith(str(tmp_path))


def test_the_device_root_gets_the_check_tools(monkeypatch, tmp_path):
    """`start` stages scripts/device_root_checks.py and scripts/debian_packages.py, read-only
    under the work dir (mounted at /e2e), before `docker run` -- so `root_checks()` can run them
    from inside the container. Proven without a real docker/dpkg: `run` and `_exec` are
    monkeypatched (the package-list check needs `_exec` to return the empty listing)."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return ""

    monkeypatch.setattr(tracer, "run", fake_run)
    monkeypatch.setattr(tracer.DeviceRoot, "_exec", lambda self, *args, **kwargs: "")
    device = tracer.DeviceRoot("device-root:test", tmp_path, "c")
    device.start(Path(__file__))       # any existing file stands in for the .deb bytes
    assert calls and calls[0][:2] == ["docker", "run"]
    for name in ("device_root_checks.py", "debian_packages.py"):
        staged = tmp_path / "tools" / "scripts" / name
        assert staged.read_bytes() == (tracer.ROOT / "scripts" / name).read_bytes()
