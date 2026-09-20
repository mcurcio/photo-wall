"""Generated local bytes and injected ops/fetcher; no physical boot claim.

Covers 0009 Phase 4 slice p4-boot-chain part 1: the new, ADDITIVE, ticketless
netboot init (`appliance/netboot_init.py`), now on the central-discovery model
(cmdline carries only Central's root; the Pi self-identifies by serial; the
corruption digest arrives in the HTTP `Digest` header). `appliance/bootstrap.py`'s
ticketed `boot()` path is untouched -- see tests/test_bootstrap.py, still green.
"""

import base64
import hashlib
import stat

import pytest

from appliance.bootstrap import LinuxOps
from appliance.netboot_init import (
    NETBOOT_BASE_PATH,
    SERIAL_HEADER,
    NetbootError,
    NetbootOps,
    fetch_verified,
    netboot,
    parse_cmdline,
    parse_digest_header,
)
from appliance.provision import AppFetcher

BODY = b"generated base squashfs bytes"
SHA256 = hashlib.sha256(BODY).hexdigest()
DIGEST_HEADER = "sha-256=" + base64.b64encode(bytes.fromhex(SHA256)).decode()
CENTRAL = "http://boot.test/"
ORIGIN = "http://boot.test"
SERIAL = "10000000abcd1234"


def cmdline(**overrides):
    base = {"photowall.central": CENTRAL}
    base.update(overrides)
    return base


class RecordingLog:
    """Captures per-phase console lines instead of writing to /dev/console."""

    def __init__(self, debug=False):
        self.debug = debug
        self.lines = []

    def info(self, message):
        self.lines.append(message)

    def detail(self, message):
        if self.debug:
            self.lines.append(message)


class Ops:
    """Records every call; any ticket/watchdog/identity call fails the test
    outright, proving the ticketless path never reaches for them."""

    def __init__(self, path):
        self.run_root = path / "run"
        self.run_root.mkdir()
        self.calls = []
        self.mounted = []

    def configure_networking(self):
        self.calls.append("configure_networking")

    def network_info(self):
        self.calls.append("network_info")
        return {"ip": "10.0.20.42", "gateway": "10.0.20.1", "dns": "10.0.20.1", "search": "lan"}

    def resolve(self, host):
        self.calls.append("resolve")
        return ["10.0.20.5"]

    def ram(self):
        self.calls.append("ram")
        path = self.run_root / "ram"
        path.mkdir()
        return path

    def mount_root(self, image, rootmnt):
        self.calls.append("mount_root")
        self.mounted.append((image.read_bytes(), rootmnt))

    def device_id(self):
        pytest.fail("ticketless netboot must never read equipment identity")

    def boot_id(self):
        pytest.fail("ticketless netboot must never read a boot id")

    def time_ready(self, server):
        pytest.fail("ticketless netboot must never contact a time server")

    def arm_trial_watchdog(self):
        pytest.fail("ticketless netboot must never arm the trial watchdog")


class Fetcher:
    """Fake AppFetcher: asserts the code-constant path is requested, records the
    request headers, and surfaces a `Digest` response header via on_response."""

    body = BODY
    digest_header = DIGEST_HEADER

    def __init__(self, origin):
        self.origin = origin
        self.sent_headers = None

    def chunks(self, path, maximum, *, headers=None, on_response=None):
        assert self.origin == ORIGIN
        assert path == NETBOOT_BASE_PATH
        assert maximum > 0
        self.sent_headers = headers
        if on_response is not None:
            response = {"Content-Length": str(len(self.body))}
            if self.digest_header is not None:
                response["Digest"] = self.digest_header
            on_response(response)
        if self.body:
            yield self.body

    def ticket(self, *_):
        pytest.fail("ticketless netboot must never request a boot ticket")


def capturing_factory(fetcher_cls=Fetcher):
    """A fetcher_factory that records the instance it built, so a test can read
    the request headers the initrd sent."""
    created = {}

    def factory(origin):
        created["fetcher"] = fetcher_cls(origin)
        return created["fetcher"]

    factory.created = created
    return factory


def run(cmd, rootmnt, *, ops, fetcher_factory=Fetcher, serial_reader=lambda: SERIAL, log=None):
    return netboot(cmd, rootmnt, ops=ops, fetcher_factory=fetcher_factory,
                   serial_reader=serial_reader, log=log or RecordingLog())


def test_happy_path_fetches_verifies_mounts_writes_no_ticketed_context(tmp_path):
    ops = Ops(tmp_path)
    rootmnt = tmp_path / "root"
    run(cmdline(), rootmnt, ops=ops)

    assert ops.calls == ["configure_networking", "network_info", "resolve", "ram", "mount_root"]
    assert ops.mounted == [(BODY, rootmnt)]
    assert not (ops.run_root / "boot.json").exists()


def test_serial_is_sent_as_request_header(tmp_path):
    ops = Ops(tmp_path)
    factory = capturing_factory()
    run(cmdline(), tmp_path / "root", ops=ops, fetcher_factory=factory)
    assert factory.created["fetcher"].sent_headers == {SERIAL_HEADER: SERIAL}


def test_netboot_path_is_the_code_constant_not_from_cmdline(tmp_path):
    # The Fetcher asserts `path == NETBOOT_BASE_PATH` internally; a happy run
    # proves the initrd appends the constant to Central's root.
    ops = Ops(tmp_path)
    run(cmdline(), tmp_path / "root", ops=ops)
    assert NETBOOT_BASE_PATH == "/v1/netboot/base"


def test_missing_serial_still_boots_without_the_header(tmp_path):
    ops = Ops(tmp_path)
    factory = capturing_factory()
    run(cmdline(), tmp_path / "root", ops=ops, fetcher_factory=factory, serial_reader=lambda: None)
    assert factory.created["fetcher"].sent_headers == {}
    assert ops.mounted == [(BODY, tmp_path / "root")]


def test_digest_header_drives_verification_and_passes(tmp_path):
    ops = Ops(tmp_path)
    run(cmdline(), tmp_path / "root", ops=ops)
    assert ops.mounted == [(BODY, tmp_path / "root")]


def test_wrong_digest_header_fails_closed(tmp_path):
    class WrongDigest(Fetcher):
        digest_header = "sha-256=" + base64.b64encode(bytes(32)).decode()

    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_integrity"):
        run(cmdline(), tmp_path / "root", ops=ops, fetcher_factory=WrongDigest)
    assert ops.mounted == []
    assert not (ops.run_root / "ram" / "photo-wall-base.squashfs").exists()


def test_missing_digest_header_fails_closed(tmp_path):
    class NoDigest(Fetcher):
        digest_header = None

    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_no_digest"):
        run(cmdline(), tmp_path / "root", ops=ops, fetcher_factory=NoDigest)
    assert ops.mounted == []
    assert not (ops.run_root / "ram" / "photo-wall-base.squashfs").exists()


def test_corruption_fails_closed_without_mounting(tmp_path):
    class Corrupt(Fetcher):
        # Advertises the (correct-for-BODY) digest but streams other bytes.
        body = b"not the expected bytes at all"
        digest_header = DIGEST_HEADER

    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_integrity"):
        run(cmdline(), tmp_path / "root", ops=ops, fetcher_factory=Corrupt)
    assert ops.mounted == []
    assert not (ops.run_root / "ram" / "photo-wall-base.squashfs").exists()


def test_missing_central_fails_closed_before_any_network_or_mount(tmp_path):
    class NeverCalled(Fetcher):
        def chunks(self, *_, **__):
            pytest.fail("must not fetch with no central")

    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_configuration"):
        run(cmdline(**{"photowall.central": None}), tmp_path / "root",
            ops=ops, fetcher_factory=NeverCalled)
    assert ops.calls == []
    assert ops.mounted == []


def test_missing_central_key_entirely_fails_closed(tmp_path):
    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_configuration"):
        run({}, tmp_path / "root", ops=ops)
    assert ops.calls == []


@pytest.mark.parametrize("bad", [
    "http://boot.test/v1/netboot/base",   # a path, not a root
    "http://user@boot.test/",             # userinfo
    "http://user:pw@boot.test/",          # userinfo
    "http://boot.test/?x=1",              # query
    "http://boot.test/#frag",             # fragment
    "ftp://boot.test/",                   # scheme
    "http:///",                           # no hostname
    "http://boot.test\\evil/",            # backslash
    "http://boot.test/ ",                 # control/space char
])
def test_central_root_validation_rejects(tmp_path, bad):
    ops = Ops(tmp_path)
    with pytest.raises(NetbootError, match="netboot_configuration"):
        run(cmdline(**{"photowall.central": bad}), tmp_path / "root", ops=ops)
    assert ops.calls == []


@pytest.mark.parametrize("good", ["http://boot.test", "http://boot.test/", "https://photo-wall/"])
def test_central_root_validation_accepts_root_forms(tmp_path, good):
    class Any(Fetcher):
        def chunks(self, path, maximum, *, headers=None, on_response=None):
            assert path == NETBOOT_BASE_PATH
            if on_response is not None:
                on_response({"Digest": DIGEST_HEADER, "Content-Length": str(len(BODY))})
            yield BODY

    ops = Ops(tmp_path)
    run(cmdline(**{"photowall.central": good}), tmp_path / "root", ops=ops, fetcher_factory=Any)
    assert ops.mounted == [(BODY, tmp_path / "root")]


def test_mount_root_is_reused_verbatim_not_reimplemented():
    """0009 Phase 4 says RAM-overlay-mount is done by REUSING
    `bootstrap.LinuxOps.mount_root`. Assert `NetbootOps` adds network
    bring-up + diagnostics only and does not shadow any mount machinery."""
    assert NetbootOps.mount_root is LinuxOps.mount_root
    assert NetbootOps._prepare_root is LinuxOps._prepare_root
    assert NetbootOps.ram is LinuxOps.ram
    assert NetbootOps.command is LinuxOps.command


def test_netboot_ops_mount_root_runs_the_same_mount_sequence_as_bootstrap(tmp_path):
    """Exercises the literal inherited `mount_root` (not a stand-in) through
    `NetbootOps`, the same way tests/test_bootstrap.py exercises it through
    `LinuxOps` -- same squashfs/tmpfs/overlay sequence, no reimplementation."""
    ops = NetbootOps(tmp_path / "run")
    calls = []

    def command(*argv, **kwargs):
        calls.append(argv)
        return b""

    ops.command = command
    rootmnt = tmp_path / "root"
    ops.mount_root(tmp_path / "rootfs", rootmnt)
    assert stat.S_IMODE(rootmnt.stat().st_mode) == 0o755
    assert [call[2] for call in calls if call[0] == "mount"] == ["squashfs", "tmpfs", "overlay"]


def test_parse_cmdline_reads_photowall_keys_ignores_bare_flags():
    parsed = parse_cmdline(
        "console=ttyAMA0 quiet photowall.central=" + CENTRAL + " photowall.debug=1"
    )
    assert parsed["photowall.central"] == CENTRAL
    assert parsed["photowall.debug"] == "1"
    assert "quiet" not in parsed


@pytest.mark.parametrize("header,expected", [
    (DIGEST_HEADER, SHA256),
    ("SHA-256=" + base64.b64encode(bytes.fromhex(SHA256)).decode(), SHA256),
    ("md5=abc, sha-256=" + base64.b64encode(bytes.fromhex(SHA256)).decode(), SHA256),
    (None, None),
    ("", None),
    ("sha-256=not-base64!!", None),
    ("sha-256=" + base64.b64encode(bytes(16)).decode(), None),  # wrong length
    ("md5=" + base64.b64encode(bytes(16)).decode(), None),      # no sha-256
])
def test_parse_digest_header(header, expected):
    assert parse_digest_header(header) == expected


def test_fetch_verified_reuses_appfetcher_streaming_not_a_bespoke_client(tmp_path):
    """`AppFetcher` (appliance/provision.py) is imported and used directly by
    `netboot()`, not reimplemented; this exercises `fetch_verified` with its
    real `.chunks()` generator shape against a fake opener, and proves the
    request header is sent and the response `Digest` header is surfaced."""
    import io

    class Response:
        status = 200

        def __init__(self, body):
            self.body = io.BytesIO(body)
            self.headers = {"Content-Length": str(len(body)), "Digest": DIGEST_HEADER}

        def read(self, size):
            return self.body.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.body.close()

    seen = {}

    class Opener:
        def open(self, request, **kwargs):
            assert request.full_url == ORIGIN + NETBOOT_BASE_PATH
            # urllib capitalizes header keys ("X-photowall-serial"); the real
            # server (Starlette) looks up case-insensitively, so match that.
            seen["serial"] = next(
                (value for key, value in request.header_items()
                 if key.lower() == SERIAL_HEADER.lower()),
                None,
            )
            return Response(BODY)

    fetcher = AppFetcher(ORIGIN, opener=Opener())
    destination = tmp_path / "img.squashfs"
    captured = {}

    def on_response(headers):
        captured["digest"] = parse_digest_header(headers.get("Digest"))

    chunks = fetcher.chunks(NETBOOT_BASE_PATH, 10_000,
                            headers={SERIAL_HEADER: SERIAL}, on_response=on_response)
    fetch_verified(chunks, destination, lambda: captured.get("digest"))
    assert destination.read_bytes() == BODY
    assert seen["serial"] == SERIAL


def test_fetch_verified_rejects_oversized_chunk(tmp_path):
    """A single block larger than the per-block ceiling (`bootstrap.CHUNK`)
    is rejected outright -- the same per-block bound `copy_verified` uses."""
    from appliance.bootstrap import CHUNK

    with pytest.raises(NetbootError, match="netboot_chunk"):
        fetch_verified([b"x" * (CHUNK + 1)], tmp_path / "img", lambda: SHA256)
    assert not (tmp_path / "img").exists()


def test_fetch_verified_rejects_total_size_over_rootfs_bound(tmp_path, monkeypatch):
    """The running total across many well-formed chunks is still bounded
    (`contracts.release.MAX_ROOTFS_BYTES`), independent of any per-chunk
    check -- lower the bound for the test instead of streaming a real
    gigabyte."""
    import appliance.netboot_init as module

    monkeypatch.setattr(module, "MAX_ROOTFS_BYTES", 10)
    with pytest.raises(NetbootError, match="netboot_limit"):
        fetch_verified([b"x" * 6, b"y" * 6], tmp_path / "img", lambda: SHA256)
    assert not (tmp_path / "img").exists()


def test_fetch_verified_missing_digest_callable_fails_closed(tmp_path):
    with pytest.raises(NetbootError, match="netboot_no_digest"):
        fetch_verified([BODY], tmp_path / "img", lambda: None)
    assert not (tmp_path / "img").exists()


def test_bootstrap_ticketed_boot_path_is_untouched_by_this_module():
    """Guards against accidental coupling: this module must not import
    contracts.release.BootTicket/Release-signing machinery or
    appliance.updates (the ticket/signature layer belongs only to the old
    path)."""
    import appliance.netboot_init as module

    assert not hasattr(module, "BootTicket")
    assert not hasattr(module, "verify_release")
