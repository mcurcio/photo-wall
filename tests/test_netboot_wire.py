"""The real stage 1 (`appliance.netboot_init.netboot`) over a real socket and the real
`HttpTransport`: a stand-in Central (tls_fixture.central_stub, which answers /v1/locate as Central
does) gives a node boot offer and its base; stage 1 mounts exactly those bytes, and refuses a base
served without its Digest or a Central without the offer route, mounting nothing.

The stand-in, not real Central, serves here: real Central's node offers are proven by
tests/test_node_boot.py, and real Central structurally cannot emit a Digest-less base. Every
other row of stage 1 is tests/test_netboot_init.py's, over a scripted transport.
"""

import base64
import hashlib
import http.server
import json
import time
from pathlib import Path

import pytest
import tls_fixture as tls
from test_netboot_init import BOOT_ID, NONCE, SERIAL, node_offer

import appliance.netboot_init as netboot_module
from appliance.netboot_init import NODE_BOOT_OFFERS_PATH, NetbootError, netboot
from contracts.clock_record import ClockRecord, ClockState
from contracts.node_boot import encode_node_boot_offer
from uplink.transport import HttpTransport
from uplink.trust import Trust

REPO = Path(__file__).resolve().parents[1]
SQUASHFS = b"rpi-image-gen base squashfs payload, streamed over a real socket" * 64
OFFER = node_offer(sha256=hashlib.sha256(SQUASHFS).hexdigest(), size=len(SQUASHFS))
BASE_PATH = f"{NODE_BOOT_OFFERS_PATH}/{OFFER.offer_id}/artifacts/base"
DIGEST = "sha-256=" + base64.b64encode(hashlib.sha256(SQUASHFS).digest()).decode()


class _Log:
    debug = False

    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(message)

    def detail(self, message):
        pass


class _Keeper:
    summary = "armed device=/dev/watchdog0 timeout=124s"

    def __init__(self):
        self.handed_over = False

    def pet(self):
        pass

    def paced(self, blocks):
        yield from blocks

    def hand_over(self):
        self.handed_over = True


class _Ops:
    """Injected ram/mount ops -- no real root pivot; the HTTP exchange that feeds them is real."""

    def __init__(self, run_root):
        self.run_root = run_root
        self.run_root.mkdir(parents=True)
        self.mounted = []

    def configure_networking(self):
        pass

    def network_info(self):
        return {"ip": "127.0.0.1"}

    def ram(self):
        path = self.run_root / "ram"
        path.mkdir(exist_ok=True)
        return path

    def mount_root(self, image, rootmnt):
        self.mounted.append(image.read_bytes())

    def hand_over_modules(self, rootmnt, *, pet, release=None):
        return "modules=none (no kernel modules in this harness)"


class _ClockGate:
    def settle(self):
        return ClockRecord(state=ClockState.SYNCED, floor=1, raised_to_floor=False, tier=None,
                           source=None, offset=None, stepped=False, tried=(), writer="netboot",
                           written_at=time.time())


def _central(*, digest=DIGEST, offers=True):
    """A stand-in Central's handler: the offer route (or the canonical unknown-route 404) and
    the offer's base, with `digest` as its Digest header when given."""

    class Central(http.server.BaseHTTPRequestHandler):
        def _reply(self, status, body, headers=()):
            self.send_response(status)
            for name, value in (("Content-Length", str(len(body))), *headers):
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 (stdlib handler contract)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if offers and self.path == NODE_BOOT_OFFERS_PATH:
                self._reply(200, encode_node_boot_offer(OFFER),
                            (("Content-Type", "application/json"),))
            else:
                self._reply(404, json.dumps({"detail": "Not Found"}).encode(),
                            (("Content-Type", "application/json"),))

        def do_GET(self):  # noqa: N802
            if self.path != BASE_PATH:
                return self._reply(404, b"")
            self._reply(200, SQUASHFS, (("Digest", digest),) if digest else ())

    return tls.central_stub(Central)


@pytest.fixture(autouse=True)
def fixed_nonce(monkeypatch):
    monkeypatch.setattr(netboot_module, "node_nonce", lambda *_: NONCE)
    monkeypatch.setattr(netboot_module, "INITRD_CA_BUNDLE", Path("/nonexistent/initrd-ca.crt"))


def _netboot(port, tmp_path, ops, keeper):
    transport = HttpTransport(trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA)))
    netboot({"photowall.central": f"http://127.0.0.1:{port}/"}, tmp_path / "root", ops=ops,
            transport=transport, clock_gate=_ClockGate(), keeper=keeper,
            trust_provenance="bundle=sha256:test anchors=1 floor=1970-01-01",
            serial_reader=lambda: SERIAL, log=_Log(), boot_id_reader=lambda: BOOT_ID)


@pytest.fixture(autouse=True)
def own_resolver(tmp_path, monkeypatch):
    source = tmp_path / "stage1-resolv.conf"
    source.write_text("nameserver 127.0.0.1\n")
    original = netboot_module.hand_over_resolver
    monkeypatch.setattr(netboot_module, "hand_over_resolver",
                        lambda rootmnt: original(rootmnt, source=source))


def test_stage_1_mounts_the_offers_base_over_the_wire(tmp_path):
    ops, keeper = _Ops(tmp_path / "run"), _Keeper()
    with tls.serve_stub(_central()) as stub:
        _netboot(stub.port, tmp_path, ops, keeper)
    assert ops.mounted == [SQUASHFS] and keeper.handed_over
    assert stub.requests == ["/v1/locate", NODE_BOOT_OFFERS_PATH, BASE_PATH]
    handoff = json.loads((tmp_path / "root/etc/photo-wall/node-boot.json").read_bytes())
    assert handoff["offer"]["offer_id"] == str(OFFER.offer_id)


def test_missing_digest_header_fails_closed_over_the_wire(tmp_path):
    """A buggy or hostile server's Digest-less 200 is refused: nothing is mounted."""
    ops, keeper = _Ops(tmp_path / "run"), _Keeper()
    with tls.serve_stub(_central(digest=None)) as stub:
        with pytest.raises(NetbootError, match="netboot_offer_header_mismatch"):
            _netboot(stub.port, tmp_path, ops, keeper)
    assert ops.mounted == [] and not keeper.handed_over
    assert stub.requests == ["/v1/locate", NODE_BOOT_OFFERS_PATH, BASE_PATH]


def test_a_central_without_the_offer_route_boots_nothing(tmp_path):
    """There is no other boot path: an older Central's unknown-route answer is a failure."""
    ops, keeper = _Ops(tmp_path / "run"), _Keeper()
    with tls.serve_stub(_central(offers=False)) as stub:
        with pytest.raises(NetbootError, match="node_boot_route_unsupported"):
            _netboot(stub.port, tmp_path, ops, keeper)
    assert ops.mounted == [] and stub.requests == ["/v1/locate", NODE_BOOT_OFFERS_PATH]


def test_every_step_of_the_base_image_workflow_fails_where_it_fails():
    """An explicit `shell: bash` is `bash -eo pipefail`; the default is `bash -e`, under which
    `image=$(failing | tail -n1)` succeeds (the PR #28 tracer's green-while-failed step). No step
    may override it."""
    text = (REPO / ".github/workflows/base-image.yml").read_text()
    assert "\ndefaults:\n  run:\n    shell: bash\n" in text
    shells = [line.split(":", 1)[1].strip() for line in text.splitlines()
              if line.strip().startswith("shell:")]
    assert shells == ["bash"]
