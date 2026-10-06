"""DownloadPreparer against a local HTTP stub: the status bound comes from the read-through
contract, transient failures retry inside the boot window, terminal ones do not, and a killed
attempt's debris is gone before admission."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from appliance.boot import node_bootstrap as bootstrap
from appliance.kernel import boot_stage
from appliance.node import preparer
from contracts.app_environment import AppEnvironmentRefV2
from contracts.read_through import READ_THROUGH_WAIT_SECONDS
from uplink import fetch
from uplink.transport import HOP_TIMEOUT

BODY = b"exact environment archive bytes" * 64
GIB = 1024**3
REPO = Path(__file__).resolve().parents[1]


def reference(body: bytes = BODY) -> AppEnvironmentRefV2:
    return AppEnvironmentRefV2(hashlib.sha256(body).hexdigest(), len(body), "c" * 64, "photo-wall-player",
                               "2.0", "arm64", "d" * 64, "e" * 64, "/usr/lib/photo-wall-environment/entry",
                               "base-v2", "graphics-v2", "frame-v1")


class Stub:
    """Answers each request with the next scripted (hold seconds, status, headers, body)."""

    def __init__(self, script):
        self.script, self.requests, self.lock = list(script), [], threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                with stub.lock:
                    stub.requests.append(dict(self.headers))
                    hold, status, headers, body = stub.script.pop(0)
                time.sleep(hold)
                try:
                    self.send_response(status)
                    for name, value in {"Content-Length": str(len(body)), **headers}.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the client gave up on its status bound

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v2/node/boot-offers/x/artifacts/app"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub_server():
    servers = []

    def start(script):
        servers.append(Stub(script))
        return servers[-1]
    yield start
    for server in servers:
        server.close()


@pytest.fixture
def roomy(monkeypatch):
    monkeypatch.setattr(preparer, "memory_values", lambda: (8 * GIB, 8 * GIB))
    monkeypatch.setattr(preparer.shutil, "disk_usage", lambda _: SimpleNamespace(total=8 * GIB, used=0, free=8 * GIB))
    staged = []

    def stage(archive, roots, environment, **_):
        assert archive.read_bytes() == BODY
        staged.append(archive)
        return roots / environment.environment_sha256
    monkeypatch.setattr(preparer, "stage_archive", stage)
    return staged


def make(directory, url, **extra):
    return preparer.DownloadPreparer(directory, url=url, base_abi="base-v2", graphics_abi="graphics-v2",
                                     plugin_abi="frame-v1", **extra)


def central_error(code):
    return json.dumps({"error": code}).encode()


def scale_status_bound(monkeypatch, seconds):
    """The stream's status bound and the preparer's attempt pacing are the one constant."""
    monkeypatch.setattr(fetch, "STATUS_TIMEOUT", seconds)
    monkeypatch.setattr(preparer, "STATUS_TIMEOUT", seconds)


def test_status_bound_is_the_read_through_contract_not_a_literal():
    # The preparer holds uplink's bound, itself derived from Central's read-through wait.
    assert preparer.STATUS_TIMEOUT is fetch.STATUS_TIMEOUT
    assert preparer.STATUS_TIMEOUT == READ_THROUGH_WAIT_SECONDS + HOP_TIMEOUT > READ_THROUGH_WAIT_SECONDS
    # The attempt deadline starts before the request: it absorbs the status wait plus a
    # 974 MB environment at 2.3 MiB/s.
    assert preparer.ATTEMPT_SECONDS >= preparer.STATUS_TIMEOUT + 974_000_000 / (2.3 * 1024**2)
    assert not re.search(r"timeout\s*=\s*\d", (REPO / "appliance/node/preparer.py").read_text())


def test_boot_window_fits_the_prepare_unit_timeout():
    unit = (REPO / "appliance/systemd/photo-wall-node-prepare.service").read_text()
    [timeout] = re.findall(r"^TimeoutStartSec=(\d+)$", unit, re.M)
    assert bootstrap.DOWNLOAD_WINDOW_SECONDS + bootstrap.STAGING_MARGIN_SECONDS <= int(timeout)
    assert preparer.ATTEMPT_SECONDS <= bootstrap.DOWNLOAD_WINDOW_SECONDS
    assert "Restart=" not in unit  # the retries are in-process


def test_held_status_then_timeout_then_503_then_success(tmp_path, monkeypatch, stub_server, roomy):
    # Scaled 35 s -> 1.5 s. A hold under the bound is answered, one over it is a status timeout:
    # proof the bound in use is the derived constant, not a literal.
    scale_status_bound(monkeypatch, 1.5)
    server = stub_server([(2.5, 200, {}, BODY),
                          (1.0, 503, {"Retry-After": "7"}, central_error("app_artifact_timeout")),
                          (1.0, 200, {}, BODY)])
    sleeps = []
    claim = SimpleNamespace(credential="secret", session_id=uuid4())
    work = make(tmp_path / "work", server.url, claim=claim, retry_until=time.monotonic() + 60,
                sleep=sleeps.append)
    assert work.prepare(reference())
    assert len(server.requests) == 3 and len(roomy) == 1
    # After the status timeout the next attempt starts one status bound after the first began.
    assert 0 <= sleeps[0] < 0.2
    assert sleeps[1] == pytest.approx(7.0, abs=0.2)  # Retry-After honoured
    assert all(r["Accept-Encoding"] == "identity" and r["Authorization"] == "Bearer secret"
               and r["X-Node-Session"] == str(claim.session_id) for r in server.requests)
    assert not list((tmp_path / "work").glob("*.partial"))


def test_short_retry_after_is_floored(tmp_path, stub_server, roomy):
    server = stub_server([(0, 503, {"Retry-After": "1"}, b""), (0, 200, {}, BODY)])
    sleeps = []
    work = make(tmp_path / "work", server.url, retry_until=time.monotonic() + 600, sleep=sleeps.append)
    assert work.prepare(reference())
    # One attempt per status bound at least, and never under the floor.
    assert sleeps and sleeps[0] >= preparer.RETRY_AFTER_FLOOR_SECONDS


@pytest.mark.parametrize("status, headers, wait", [
    (502, {"Retry-After": "9"}, 9.0),
    (504, {}, preparer.RETRY_AFTER_FLOOR_SECONDS),
    (503, {}, preparer.RETRY_AFTER_FLOOR_SECONDS),         # not Central's: a proxy's 503
    (503, {"Retry-After": "12"}, 12.0),
    (503, {"Retry-After": "\u00b2"}, preparer.RETRY_AFTER_FLOOR_SECONDS),     # not ASCII digits
    (503, {"Retry-After": "9" * 5000}, preparer.RETRY_AFTER_FLOOR_SECONDS),  # over nine digits
    (503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, preparer.RETRY_AFTER_FLOOR_SECONDS),
])
def test_gateway_errors_are_transient_like_a_503(tmp_path, monkeypatch, stub_server, roomy,
                                                 status, headers, wait):
    # A proxy in front of Central: Retry-After honoured when given, else the floor.
    scale_status_bound(monkeypatch, 1.0)
    server = stub_server([(0, status, headers, b"<html>Bad Gateway</html>"), (0, 200, {}, BODY)])
    sleeps = []
    work = make(tmp_path / "work", server.url, retry_until=time.monotonic() + 60, sleep=sleeps.append)
    assert work.prepare(reference())
    assert len(server.requests) == 2 and len(roomy) == 1
    assert sleeps[0] == pytest.approx(wait, abs=0.2)


@pytest.mark.parametrize("script, fault", [
    ((500, b""), "preparation_download_refused:500"),
    ((501, b""), "preparation_download_refused:501"),
    ((403, central_error("node_boot_generation_stale")), "preparation_download_refused:403:node_boot_generation_stale"),
    ((404, b""), "preparation_download_refused:404"),
    ((410, central_error("gone")), "preparation_download_refused:410:gone"),
    # Central's own permanent 503s carry no Retry-After (central/fleet/routes.py).
    ((503, central_error("content_unavailable")), "preparation_download_refused:503:content_unavailable"),
    ((503, central_error("offer_artifact_mismatch")),
     "preparation_download_refused:503:offer_artifact_mismatch"),
    ((200, b"x" * len(BODY)), "preparation_digest_mismatch"),
])
def test_terminal_failures_are_not_retried(tmp_path, stub_server, roomy, script, fault):
    status, body = script
    server = stub_server([(0, status, {}, body), (0, 200, {}, BODY)])
    sleeps = []
    work = make(tmp_path / "work", server.url, retry_until=time.monotonic() + 600, sleep=sleeps.append)
    with pytest.raises(preparer.DownloadFailed) as raised:
        work.prepare(reference())
    assert str(raised.value) == fault and boot_stage.fault_token(raised.value) == fault
    assert len(server.requests) == 1 and not sleeps and not roomy
    assert not list((tmp_path / "work").iterdir())


def test_without_a_window_a_503_is_one_attempt(tmp_path, stub_server, roomy):
    # DesiredPreparation passes no window: it retries on its next poll.
    server = stub_server([(0, 503, {"Retry-After": "5"}, b""), (0, 200, {}, BODY)])
    with pytest.raises(ValueError, match="preparation_download_refused:503"):
        make(tmp_path / "work", server.url).prepare(reference())
    assert len(server.requests) == 1


def test_a_retry_that_cannot_finish_in_the_window_is_not_started(tmp_path, stub_server, roomy):
    server = stub_server([(0, 503, {"Retry-After": "60"}, b""), (0, 200, {}, BODY)])
    sleeps = []
    work = make(tmp_path / "work", server.url, retry_until=time.monotonic() + 61, sleep=sleeps.append)
    with pytest.raises(preparer.DownloadFailed, match="refused:503"):
        work.prepare(reference())
    assert len(server.requests) == 1 and not sleeps


def test_stage_stays_running_through_retries(tmp_path, stub_server, roomy):
    server = stub_server([(0, 503, {}, b""), (0, 200, {}, BODY)])
    records = tmp_path / "records"
    records.mkdir()
    seen = []

    def sleep(_):
        seen.append(json.loads((records / "prepare.json").read_text())["state"])
    work = make(tmp_path / "work", server.url, retry_until=time.monotonic() + 600, sleep=sleep)
    boot_stage.run_stage("prepare", lambda: work.prepare(reference()), directory=records)
    assert seen == ["running"]
    assert json.loads((records / "prepare.json").read_text())["state"] == "done"


def test_killed_attempt_debris_is_removed_before_admission(tmp_path, monkeypatch, roomy):
    environment = reference()
    directory = tmp_path / "work"
    (directory / "verified/.stage-abc123/rootfs").mkdir(parents=True)
    (directory / "verified/.stage-abc123/rootfs/big").write_bytes(b"x" * 4096)
    (directory / (environment.environment_sha256 + ".partial")).write_bytes(b"half")
    (directory / ("f" * 64 + ".partial")).write_bytes(b"other")
    (directory / (environment.environment_sha256 + ".tar")).write_bytes(BODY)  # complete: kept
    admitted = []

    def admit(*_, **__):
        admitted.append(sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")))
    monkeypatch.setattr(preparer, "admit_preparation", admit)
    make(directory, "http://central.invalid/x").prepare(environment)
    assert admitted == [[environment.environment_sha256 + ".tar", "verified"]]


def test_prepare_stage_clears_cold_staging_before_admission(tmp_path, monkeypatch):
    from node.boot.test_node_boot_linux import ROOT, offer
    selected = offer(app=True)
    abi = {name: getattr(selected.base, name) for name in ("base_abi", "graphics_abi", "plugin_abi")}
    monkeypatch.setattr(bootstrap, "materialize_handoff", lambda **_: (ROOT, selected, abi))
    monkeypatch.setattr(bootstrap, "memory_values", lambda: (8 * GIB, 8 * GIB))
    node_store = tmp_path / str(bootstrap.STORE).lstrip("/")
    debris = node_store / "app-roots/.cold-staging/app"
    (debris / "verified/.stage-xyz").mkdir(parents=True)
    (debris / ("a" * 64 + ".partial")).write_bytes(b"half")
    (node_store / "app-roots/.cold-staging").chmod(0o700)
    seen = []

    def admit(*_, **__):
        seen.append((node_store / "app-roots/.cold-staging").exists())
    monkeypatch.setattr(bootstrap, "admit_cold", admit)
    windows = []

    class Preparer:
        def __init__(self, *_, **kw):
            windows.append(kw["retry_until"])

        def prepare(self, _):
            raise ValueError("stop")
    monkeypatch.setattr(bootstrap, "DownloadPreparer", Preparer)
    before = time.monotonic()
    with pytest.raises(ValueError, match="stop"):
        bootstrap.prepare_roots(root=tmp_path)
    assert seen == [False]
    # The boot path retries inside the stage's window.
    assert before + bootstrap.DOWNLOAD_WINDOW_SECONDS <= windows[0] <= time.monotonic() + bootstrap.DOWNLOAD_WINDOW_SECONDS


@pytest.mark.parametrize("retry_after", ["\u00b2", "9" * 5000, "-1", ""])
def test_central_503_with_an_unreadable_retry_after_is_terminal(tmp_path, stub_server, roomy,
                                                                retry_after):
    # An unreadable Retry-After is absent: Central's 503 without one is permanent, and the
    # header never fails the stage as an unexpected error.
    server = stub_server([(0, 503, {"Retry-After": retry_after}, central_error("app_failed")),
                          (0, 200, {}, BODY)])
    sleeps = []
    work = make(tmp_path / "work", server.url, retry_until=time.monotonic() + 600, sleep=sleeps.append)
    with pytest.raises(preparer.DownloadFailed) as raised:
        work.prepare(reference())
    assert str(raised.value) == "preparation_download_refused:503:app_failed"
    assert raised.value.retry_after is None and len(server.requests) == 1 and not sleeps


def test_central_503_with_retry_after_is_transient(tmp_path, monkeypatch, stub_server, roomy):
    scale_status_bound(monkeypatch, 1.0)
    server = stub_server([(0, 503, {"Retry-After": "3"}, central_error("app_busy")),
                          (0, 200, {}, BODY)])
    sleeps = []
    work = make(tmp_path / "work", server.url, retry_until=time.monotonic() + 60, sleep=sleeps.append)
    assert work.prepare(reference())
    assert len(server.requests) == 2 and sleeps[0] == pytest.approx(5.0, abs=0.2)  # floored


def test_an_empty_200_is_short_and_retried(tmp_path, monkeypatch, stub_server, roomy):
    scale_status_bound(monkeypatch, 1.0)
    server = stub_server([(0, 200, {}, b""), (0, 200, {}, BODY)])
    sleeps = []
    work = make(tmp_path / "work", server.url, retry_until=time.monotonic() + 60, sleep=sleeps.append)
    assert work.prepare(reference())
    assert len(server.requests) == 2 and len(roomy) == 1


def test_content_length_is_compared_as_a_number(tmp_path, stub_server, roomy):
    server = stub_server([(0, 200, {"Content-Length": "00" + str(len(BODY))}, BODY)])
    assert make(tmp_path / "work", server.url).prepare(reference())


@pytest.mark.parametrize("declared, fault", [
    (str(len(BODY) + 1), "preparation_download_transfer_limit"),     # over the environment
    ("12x", "preparation_download_transfer_limit"),
])
def test_a_declared_length_over_the_environment_is_terminal(tmp_path, stub_server, roomy,
                                                            declared, fault):
    server = stub_server([(0, 200, {"Content-Length": declared}, BODY), (0, 200, {}, BODY)])
    sleeps = []
    work = make(tmp_path / "work", server.url, retry_until=time.monotonic() + 600, sleep=sleeps.append)
    with pytest.raises(preparer.DownloadFailed, match=fault):
        work.prepare(reference())
    assert len(server.requests) == 1 and not sleeps
