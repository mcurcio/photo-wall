"""Stage 1 (`appliance/netboot_init.py`) over a scripted Transport, a fake ClockSettler, fake ops
and a fake watchdog keeper; generated local bytes, no physical boot claim.

resolve -> clock -> locate -> direct base fetch -> mount -> hand-over, the fail-closed content
rows, the watchdog pets, and design §7's worked examples with their exact console lines."""

import ast
import base64
import hashlib
import stat
import sys
from pathlib import Path

import pytest

import appliance.netboot_init as netboot_module
from appliance.bootstrap import BootstrapError, LinuxOps, read_pi_serial
from appliance.netboot_init import (
    BASE_FETCH_SECONDS,
    NETBOOT_BASE_PATH,
    SERIAL_HEADER,
    NetbootError,
    NetbootOps,
    compare_trust_bundles,
    fetch_verified,
    main,
    netboot,
    parse_digest_header,
)
from contracts.clock_record import ClockRecord, ClockState
from tests import tls_fixture as tls
from tests.uplink_fakes import FakeReply, FakeTransport, central
from uplink.causes import Cause, UplinkError
from uplink.clock import DHCP_NTP_SERVERS
from uplink.fetch import MAX_FETCH_SECONDS

BODY = b"generated base squashfs bytes"
SHA256 = hashlib.sha256(BODY).hexdigest()
DIGEST_HEADER = "sha-256=" + base64.b64encode(bytes.fromhex(SHA256)).decode()
ROOT = "http://photo-wall.localdomain/"
FIRST = "http://photo-wall.localdomain/v1/locate"
LOCATED = "https://photo-wall.example/v1/locate"
BASE = "https://photo-wall.example/v1/netboot/base"
SERIAL = "10000000abcd1234"
PROVENANCE = "bundle=sha256:0123456789ab anchors=140 floor=2026-09-26"
RECORD = ClockRecord(state=ClockState.SYNCED, floor=1790380800, raised_to_floor=True,
                     tier="dhcp", source="192.0.2.1", offset=3605.2, stepped=True,
                     tried=("dhcp:192.0.2.1:ok",), writer="netboot", written_at=1790384406.1)


def cmdline(**overrides):
    return {"photowall.central": ROOT, **overrides}


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


class Events(list):
    """One ordered record of what every fake was asked to do."""


class Ops:
    """Records every call; any ticket/identity call fails the test outright,
    proving the ticketless path never reaches for them."""

    def __init__(self, path, events=None, *, networking_error=None):
        self.run_root = path / "run"
        self.run_root.mkdir(exist_ok=True)
        self.calls = events if events is not None else []
        self.mounted = []
        self.networking_error = networking_error

    def configure_networking(self):
        self.calls.append("configure_networking")
        if self.networking_error is not None:
            raise self.networking_error

    def network_info(self):
        self.calls.append("network_info")
        return {"ip": "192.0.2.42", "gateway": "192.0.2.1", "dns": "192.0.2.1", "search": "lan",
                "ntp": "192.0.2.1"}

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


class FakeKeeper:
    """A `Keeper` double: counts pets, how many happened before `hand_over`, and whether
    `hand_over` ran at all."""

    def __init__(self, events=None):
        self.pets = 0
        self.pets_before_hand_over = None
        self.handed_over = False
        self.events = events
        self.summary = "armed device=/dev/watchdog0 timeout=124s"

    def pet(self):
        self.pets += 1
        if self.events is not None:
            self.events.append("pet")

    def paced(self, blocks):
        for block in blocks:
            yield block
            self.pet()

    def hand_over(self):
        self.pets_before_hand_over = self.pets
        self.handed_over = True


class FakeClockGate:
    def __init__(self, record=RECORD, events=None):
        self.record, self.events, self.calls = record, events, 0

    def settle(self):
        self.calls += 1
        if self.events is not None:
            self.events.append("settle")
        return self.record


class OrderedTransport(FakeTransport):
    def __init__(self, script, events):
        super().__init__(script)
        self.events = events

    def send(self, url, **kwargs):
        self.events.append(f"GET {url}")
        return super().send(url, **kwargs)


def base_reply(body=BODY, digest=DIGEST_HEADER, **kwargs):
    headers = {"Content-Length": str(len(body))}
    if digest is not None:
        headers["Digest"] = digest
    return FakeReply(200, body=body, headers=headers, **kwargs)


def script(**overrides):
    """The Pi's real case (design §7 example 1): the http root 301s to the https origin."""
    answers = {FIRST: FakeReply(301, location="https://photo-wall.example:443/v1/locate"),
               LOCATED: central(), BASE: base_reply()}
    answers.update(overrides)
    return answers


@pytest.fixture(autouse=True)
def no_host_trust_bundle(tmp_path, monkeypatch):
    """The R5 note compares against a path the test owns, never the host's CA bundle."""
    monkeypatch.setattr(netboot_module, "INITRD_CA_BUNDLE", tmp_path / "initrd-ca.crt")


def run(cmd, tmp_path, *, answers=None, transport=None, ops=None, keeper=None, clock_gate=None,
        serial_reader=lambda: SERIAL, log=None):
    log = log or RecordingLog()
    netboot(cmd, tmp_path / "root", ops=ops or Ops(tmp_path),
            transport=transport or FakeTransport(answers or script()),
            clock_gate=clock_gate or FakeClockGate(), keeper=keeper or FakeKeeper(),
            trust_provenance=PROVENANCE, serial_reader=serial_reader, log=log)
    return log


def failed(cmd, tmp_path, error=UplinkError, **kwargs):
    log = RecordingLog()
    with pytest.raises(error):
        run(cmd, tmp_path, log=log, **kwargs)
    failures = [line for line in log.lines if line.startswith("FAILED")]
    assert len(failures) == 1
    return failures[0]


# --- the happy path ------------------------------------------------------------------------

def test_the_pi_real_case_locates_through_the_301_and_mounts_the_verified_base(tmp_path):
    ops, keeper = Ops(tmp_path), FakeKeeper()
    transport = FakeTransport(script())
    log = run(cmdline(), tmp_path, ops=ops, keeper=keeper, transport=transport)
    assert [sent[0] for sent in transport.sent] == [FIRST, LOCATED, BASE]
    assert transport.sent[2][1] == {"Accept-Encoding": "identity", SERIAL_HEADER: SERIAL}
    assert ops.calls == ["configure_networking", "network_info", "ram", "mount_root"]
    assert ops.mounted == [(BODY, tmp_path / "root")]
    assert not (ops.run_root / "boot.json").exists()
    assert keeper.handed_over
    # Example 1: the located origin drops :443, and the note keys on the configured http root.
    assert "phase 5/7 located https://photo-wall.example (Central api 1)" in log.lines
    assert ("note: configured root is http: the first hop is unauthenticated; "
            "set photowall.central=https://photo-wall.example/") in log.lines
    assert f"phase 5/7 locate: GET {FIRST} -> 301 peer=192.0.2.1" in log.lines


def test_the_setup_line_carries_provenance_keeper_and_missing_kernel_parameters(tmp_path,
                                                                                monkeypatch):
    monkeypatch.setattr(netboot_module, "missing_kernel_liveness",
                        lambda: ["watchdog.stop_on_reboot=0", "hung_task_panic=1"])
    log = run(cmdline(), tmp_path)
    assert log.lines[0] == (
        f"phase 0/7 setup: {PROVENANCE} keeper=armed device=/dev/watchdog0 timeout=124s "
        "note: kernel liveness missing: watchdog.stop_on_reboot=0 hung_task_panic=1")


def test_an_https_root_gets_no_http_note(tmp_path):
    answers = {LOCATED: central(), BASE: base_reply()}
    log = run(cmdline(**{"photowall.central": "https://photo-wall.example/"}), tmp_path,
              answers=answers)
    assert not any(line.startswith("note: configured root") for line in log.lines)


def test_example_8_an_http_root_that_never_redirects_prefers_https(tmp_path):
    answers = {FIRST: central(), "http://photo-wall.localdomain/v1/netboot/base": base_reply()}
    log = run(cmdline(), tmp_path, answers=answers)
    assert ("note: configured root is http: the first hop is unauthenticated; https preferred"
            in log.lines)


def test_the_clock_is_settled_before_locate_and_logged(tmp_path):
    events = Events()
    log = run(cmdline(), tmp_path, transport=OrderedTransport(script(), events),
              clock_gate=FakeClockGate(events=events))
    assert events.index("settle") < events.index(f"GET {FIRST}")
    assert events.count("settle") == 1
    assert ("phase 4/7 clock: clock=synced floor=2026-09-26 tried=dhcp:192.0.2.1:ok "
            "raised=True tier=dhcp source=192.0.2.1 offset=3605.2 stepped=True") in log.lines


def test_the_option_42_servers_show_in_debug_only(tmp_path):
    (tmp_path / "quiet").mkdir()
    quiet = run(cmdline(), tmp_path / "quiet")
    debug = run(cmdline(), tmp_path, log=RecordingLog(debug=True))
    line = "phase 3/7 option 42: ntp_servers=192.0.2.1"
    assert line in debug.lines and line not in quiet.lines
    assert DHCP_NTP_SERVERS.as_posix() == "/proc/net/ipconfig/ntp_servers"


def test_serial_is_sent_as_request_header_and_absent_serial_sends_none(tmp_path):
    transport = FakeTransport(script())
    run(cmdline(), tmp_path, transport=transport, serial_reader=lambda: None)
    assert transport.sent[2][1] == {"Accept-Encoding": "identity"}


def test_the_base_uses_the_generous_deadline(tmp_path, monkeypatch):
    seen = {}
    original = netboot_module.DirectFetch

    def recording(located, **kwargs):
        seen.update(kwargs)
        return original(located, **kwargs)

    monkeypatch.setattr(netboot_module, "DirectFetch", recording)
    run(cmdline(), tmp_path)
    assert seen["seconds"] == BASE_FETCH_SECONDS == MAX_FETCH_SECONDS


# --- the watchdog (S0-AC6 on the rewritten netboot) -------------------------------------------

def test_every_collaborator_is_required(tmp_path):
    kwargs = dict(ops=Ops(tmp_path), transport=FakeTransport(script()),
                  clock_gate=FakeClockGate(), keeper=FakeKeeper(), trust_provenance=PROVENANCE,
                  serial_reader=lambda: SERIAL, log=RecordingLog())
    for name in kwargs:
        partial = {key: value for key, value in kwargs.items() if key != name}
        with pytest.raises(TypeError):
            netboot(cmdline(), tmp_path / "root", **partial)


def test_a_pet_before_every_phase_line_one_per_block_and_hand_over_last(tmp_path):
    keeper = FakeKeeper()
    log = run(cmdline(), tmp_path, keeper=keeper,
              answers=script(**{BASE: base_reply(step=8)}))
    phase_lines = [line for line in log.lines if line.startswith("phase ") and
                   "hash compare" not in line]
    blocks = -(-len(BODY) // 8)
    assert keeper.pets == len(phase_lines) + blocks
    assert keeper.handed_over and keeper.pets_before_hand_over == keeper.pets


def test_hand_over_never_runs_on_a_failed_boot_and_the_failed_line_pets(tmp_path):
    keeper = FakeKeeper()
    wrong = "sha-256=" + base64.b64encode(bytes(32)).decode()
    line = failed(cmdline(), tmp_path, NetbootError, keeper=keeper,
                  answers=script(**{BASE: base_reply(digest=wrong)}))
    assert line == "FAILED phase=6 code=netboot_integrity"
    assert not keeper.handed_over and keeper.pets > 0


def test_the_failed_line_pets_before_the_debug_pause(tmp_path, monkeypatch):
    events = Events()
    monkeypatch.setattr(netboot_module.time, "sleep", lambda seconds: events.append("pause"))
    with pytest.raises(UplinkError):
        run(cmdline(**{"photowall.debug": "1"}), tmp_path, keeper=FakeKeeper(events),
            answers={FIRST: UplinkError(Cause.DNS, "failed", host="photo-wall.localdomain")})
    assert events[-2:] == ["pet", "pause"]


# --- fail closed: content ---------------------------------------------------------------------

@pytest.mark.parametrize(("reply", "code"), [
    (base_reply(digest="sha-256=" + base64.b64encode(bytes(32)).decode()), "netboot_integrity"),
    (base_reply(digest=None), "netboot_no_digest"),
    (base_reply(body=b"not the expected bytes at all!", digest=DIGEST_HEADER),
     "netboot_integrity"),
])
def test_a_bad_base_fails_closed_without_mounting(tmp_path, reply, code):
    ops = Ops(tmp_path)
    assert failed(cmdline(), tmp_path, NetbootError, ops=ops,
                  answers=script(**{BASE: reply})) == f"FAILED phase=6 code={code}"
    assert ops.mounted == []
    assert not (ops.run_root / "ram" / "photo-wall-base.squashfs").exists()


# --- design §7 worked examples: the exact FAILED line ----------------------------------------

def test_example_2_an_https_to_http_redirect_is_a_downgrade_and_b_is_never_contacted(tmp_path):
    transport = FakeTransport({"https://a/v1/locate": FakeReply(302, location="http://b/v1/locate")})
    line = failed(cmdline(**{"photowall.central": "https://a/"}), tmp_path, transport=transport)
    assert line == "FAILED phase=5 cause=redirect reason=downgrade host=b"
    assert [sent[0] for sent in transport.sent] == ["https://a/v1/locate"]


def test_example_3_a_loop_is_named_before_a_is_contacted_again(tmp_path):
    transport = FakeTransport({
        "https://a/v1/locate": FakeReply(301, location="https://b/v1/locate"),
        "https://b/v1/locate": FakeReply(301, location="https://a:443/v1/locate")})
    line = failed(cmdline(**{"photowall.central": "https://a/"}), tmp_path, transport=transport)
    assert line == "FAILED phase=5 cause=redirect reason=loop host=a detail=hops=a,b"
    assert len(transport.sent) == 2


def test_example_5_the_eleventh_redirect_is_the_limit(tmp_path):
    chain = [f"https://h{index}/v1/locate" for index in range(12)]
    transport = FakeTransport({url: FakeReply(301, location=chain[index + 1])
                               for index, url in enumerate(chain[:-1])})
    line = failed(cmdline(**{"photowall.central": "https://h0/"}), tmp_path, transport=transport)
    hops = ",".join(f"h{index}" for index in range(11))
    assert line == f"FAILED phase=5 cause=redirect reason=limit host=h11 detail=hops={hops}"


def test_example_6_a_sub_path_move_is_refused(tmp_path):
    transport = FakeTransport({FIRST: FakeReply(301, location="https://b/central/v1/locate")})
    assert failed(cmdline(), tmp_path, transport=transport) == \
        "FAILED phase=5 cause=redirect reason=path_changed host=b"


def test_example_10_dns_failure_for_the_configured_host(tmp_path):
    answers = {FIRST: UplinkError(Cause.DNS, "failed", host="photo-wall.localdomain",
                                  detail="EAI_NONAME")}
    assert failed(cmdline(), tmp_path, answers=answers) == \
        "FAILED phase=5 cause=dns reason=failed host=photo-wall.localdomain detail=EAI_NONAME"


def test_example_11_an_older_central_without_locate_is_not_central(tmp_path):
    answers = script(**{LOCATED: FakeReply(404, body=b'{"detail":"Not Found"}')})
    assert failed(cmdline(), tmp_path, answers=answers) == \
        "FAILED phase=5 cause=not_central reason=status host=photo-wall.example detail=status=404"


@pytest.mark.parametrize(("cmd", "line"), [
    ({}, "FAILED phase=1 cause=configuration reason=absent"),
    (None, "FAILED phase=1 cause=configuration reason=absent detail=no_cmdline"),
    ({"photowall.central": "@@PHOTOWALL_CENTRAL@@"},
     "FAILED phase=1 cause=configuration reason=invalid"),
], ids=["12-absent", "no-cmdline", "12b-placeholder"])
def test_examples_12_and_12b_no_usable_root_fails_before_any_network(tmp_path, cmd, line):
    ops, transport = Ops(tmp_path), FakeTransport({})
    assert failed(cmd, tmp_path, ops=ops, transport=transport) == line
    assert ops.calls == [] and transport.sent == []


@pytest.mark.parametrize("bad", [
    "http://boot.test/v1/netboot/base", "http://user@boot.test/", "http://user:pw@boot.test/",
    "http://boot.test/?x=1", "http://boot.test/#frag", "ftp://boot.test/", "http:///",
    "http://boot.test\\evil/", "http://boot.test/ ", "",
])
def test_an_invalid_root_never_reaches_the_network(tmp_path, bad):
    ops = Ops(tmp_path)
    assert failed(cmdline(**{"photowall.central": bad}), tmp_path, ops=ops) == \
        "FAILED phase=1 cause=configuration reason=invalid"
    assert ops.calls == []


@pytest.mark.parametrize("good", ["http://boot.test", "http://boot.test/", "https://photo-wall/"])
def test_root_forms_are_accepted(tmp_path, good):
    origin = good.rstrip("/")
    answers = {f"{origin}/v1/locate": central(),
               f"{origin}/v1/netboot/base": base_reply()}
    ops = Ops(tmp_path)
    run(cmdline(**{"photowall.central": good}), tmp_path, ops=ops, answers=answers)
    assert ops.mounted == [(BODY, tmp_path / "root")]


def test_example_13c_an_untrusted_chain_carries_clock_and_provenance(tmp_path):
    answers = script(**{LOCATED: UplinkError(Cause.TLS, "untrusted", host="photo-wall.example",
                                             detail="verify_code=20")})
    assert failed(cmdline(), tmp_path, answers=answers) == (
        "FAILED phase=5 cause=tls reason=untrusted host=photo-wall.example detail=verify_code=20 "
        f"clock=synced tried=dhcp:192.0.2.1:ok {PROVENANCE}")


def test_example_7_a_date_failure_carries_the_unsynced_clock(tmp_path):
    record = ClockRecord(state=ClockState.UNSYNCED, floor=1790380800, raised_to_floor=True,
                         tier=None, source=None, offset=None, stepped=False,
                         tried=("dhcp:none", "pool:198.51.100.1:timeout"), writer="netboot",
                         written_at=1790380810.0)
    answers = script(**{LOCATED: UplinkError(Cause.TIME, "not_yet_valid",
                                             host="photo-wall.example", detail="verify_code=9")})
    assert failed(cmdline(), tmp_path, answers=answers, clock_gate=FakeClockGate(record)) == (
        "FAILED phase=5 cause=time reason=not_yet_valid host=photo-wall.example "
        "detail=verify_code=9 clock=unsynced tried=dhcp:none,pool:198.51.100.1:timeout "
        f"{PROVENANCE}")


def test_example_14_a_redirect_on_the_base_fetch_is_refused(tmp_path):
    answers = script(**{BASE: FakeReply(307, location="https://elsewhere.example/v1/netboot/base")})
    ops = Ops(tmp_path)
    assert failed(cmdline(), tmp_path, answers=answers, ops=ops) == (
        "FAILED phase=6 cause=redirect reason=unexpected host=photo-wall.example "
        "detail=status=307;location=elsewhere.example")
    assert ops.mounted == []


def test_example_15_central_own_error_on_the_base(tmp_path):
    answers = script(**{BASE: FakeReply(503, body=b'{"error":"base_artifact_pending"}')})
    assert failed(cmdline(), tmp_path, answers=answers) == (
        "FAILED phase=6 cause=central reason=error host=photo-wall.example "
        "detail=base_artifact_pending")


def test_example_16_a_non_http_peer_is_a_protocol_failure(tmp_path):
    answers = {"http://host:22/v1/locate": UplinkError(Cause.CONNECT, "protocol", host="host",
                                                       detail="BadStatusLine")}
    assert failed(cmdline(**{"photowall.central": "http://host:22/"}), tmp_path,
                  answers=answers) == \
        "FAILED phase=5 cause=connect reason=protocol host=host detail=BadStatusLine"


def test_a_networking_command_failure_is_named(tmp_path):
    ops = Ops(tmp_path, networking_error=BootstrapError("boot_command"))
    assert failed(cmdline(), tmp_path, ops=ops) == (
        "FAILED phase=3 cause=connect reason=unreachable host=photo-wall.localdomain "
        "detail=network_setup")


# --- R5: the base's CA bundle against this initrd's (Q3 = A) ----------------------------------

def test_equal_bundles_give_no_note_and_different_ones_one(tmp_path, monkeypatch):
    initrd = tmp_path / "initrd-ca.crt"
    initrd.write_bytes(tls.CA.pem)

    class MountingOps(Ops):
        def __init__(self, path, bundle):
            super().__init__(path)
            self.bundle = bundle

        def mount_root(self, image, rootmnt):
            super().mount_root(image, rootmnt)
            (rootmnt / "etc/ssl/certs").mkdir(parents=True, exist_ok=True)
            (rootmnt / "etc/ssl/certs/ca-certificates.crt").write_bytes(self.bundle)

    (tmp_path / "same").mkdir()
    (tmp_path / "other").mkdir()
    same = run(cmdline(), tmp_path / "same", ops=MountingOps(tmp_path / "same", tls.CA.pem))
    assert not any("CA bundle differs" in line for line in same.lines)
    other = MountingOps(tmp_path / "other", tls.OTHER_CA.pem)
    log = run(cmdline(), tmp_path / "other", ops=other)
    notes = [line for line in log.lines if "CA bundle differs" in line]
    assert notes == [
        "note: CA bundle differs from the base's: "
        f"initrd=sha256:{hashlib.sha256(tls.CA.pem).hexdigest()} "
        f"base=sha256:{hashlib.sha256(tls.OTHER_CA.pem).hexdigest()}"]
    assert other.mounted  # the boot proceeds (Q3 = A)


def test_compare_trust_bundles(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"a")
    two.write_bytes(b"a")
    assert compare_trust_bundles(one, two) is None
    two.write_bytes(b"b")
    assert compare_trust_bundles(one, two) == (hashlib.sha256(b"a").hexdigest(),
                                               hashlib.sha256(b"b").hexdigest())
    assert compare_trust_bundles(one, tmp_path / "absent") == (
        hashlib.sha256(b"a").hexdigest(), "absent")


# --- setup (phase 0, in main) ---------------------------------------------------------------

@pytest.fixture
def setup(tmp_path, monkeypatch):
    """main() with no watchdog device, a recording console and a test cmdline."""
    log = RecordingLog()
    path = tmp_path / "cmdline"
    path.write_text(f"ip=dhcp photowall.central={ROOT}\n")
    monkeypatch.setattr(netboot_module, "open_watchdog", lambda: None)
    monkeypatch.setattr(netboot_module, "ConsoleLog", lambda **kwargs: log)
    monkeypatch.setattr(sys, "argv", ["netboot_init", "--cmdline", str(path),
                                      "--rootmnt", str(tmp_path / "root")])
    return log


def test_a_missing_ca_bundle_fails_setup_even_for_an_http_root(setup, tmp_path, monkeypatch):
    real = netboot_module.Trust.public
    monkeypatch.setattr(netboot_module.Trust, "public",
                        classmethod(lambda cls: real(tmp_path / "no-bundle.crt")))
    with pytest.raises(SystemExit):
        main()
    assert setup.lines == ["FAILED phase=setup cause=tls reason=trust_store detail=ENOENT"]


def test_a_bad_floor_fails_setup(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(netboot_module.Trust, "public",
                        classmethod(lambda cls: "trust"))
    (tmp_path / "floor").write_text("soon\n")
    real = netboot_module.read_floor
    monkeypatch.setattr(netboot_module, "read_floor", lambda: real(tmp_path / "floor"))
    with pytest.raises(SystemExit):
        main()
    assert setup.lines == ["FAILED phase=setup cause=configuration reason=floor detail=malformed"]


def test_an_unreadable_cmdline_fails_setup(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["netboot_init", "--cmdline", str(tmp_path)])
    with pytest.raises(SystemExit):
        main()
    assert setup.lines == [
        "FAILED phase=setup cause=configuration reason=unreadable detail=EISDIR"]


# --- unchanged helpers ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (b"10000000abcd1234\x00", "10000000abcd1234"),   # trailing NUL stripped
    (b"  10000000abcd1234\n", "10000000abcd1234"),   # surrounding whitespace
    (b"1000\x07abcd", None),                          # embedded control char -> absent
    (b"abcd\x00ef", None),                            # embedded NUL -> absent
    (b"abcd\x7f", None),                              # DEL -> absent
    (b"\x00\x00", None),                              # nothing but NULs -> absent
    (b"", None),                                      # empty -> absent
])
def test_read_pi_serial_normalizes_and_rejects_control_chars(tmp_path, raw, expected):
    path = tmp_path / "serial-number"
    path.write_bytes(raw)
    assert read_pi_serial(str(path)) == expected


def test_netboot_path_is_the_code_constant_not_from_cmdline():
    assert NETBOOT_BASE_PATH == "/v1/netboot/base"


def test_mount_root_is_reused_verbatim_not_reimplemented():
    """0009 Phase 4 says RAM-overlay-mount is done by REUSING
    `bootstrap.LinuxOps.mount_root`. Assert `NetbootOps` adds network
    bring-up + diagnostics only and does not shadow any mount machinery."""
    assert NetbootOps.mount_root is LinuxOps.mount_root
    assert NetbootOps._prepare_root is LinuxOps._prepare_root
    assert NetbootOps.ram is LinuxOps.ram
    assert NetbootOps.command is LinuxOps.command
    assert not hasattr(NetbootOps, "resolve")      # the transport's bounded lookup does it


def test_netboot_ops_mount_root_runs_the_same_mount_sequence_as_bootstrap(tmp_path):
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


def test_fetch_verified_rejects_oversized_chunk(tmp_path):
    from appliance.bootstrap import CHUNK

    with pytest.raises(NetbootError, match="netboot_chunk"):
        fetch_verified([b"x" * (CHUNK + 1)], tmp_path / "img", lambda: SHA256)
    assert not (tmp_path / "img").exists()


def test_fetch_verified_rejects_total_size_over_rootfs_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(netboot_module, "MAX_ROOTFS_BYTES", 10)
    with pytest.raises(NetbootError, match="netboot_limit"):
        fetch_verified([b"x" * 6, b"y" * 6], tmp_path / "img", lambda: SHA256)
    assert not (tmp_path / "img").exists()


def test_fetch_verified_missing_digest_callable_fails_closed(tmp_path):
    with pytest.raises(NetbootError, match="netboot_no_digest"):
        fetch_verified([BODY], tmp_path / "img", lambda: None)
    assert not (tmp_path / "img").exists()


def test_stage_1_no_longer_imports_the_provisioner_or_the_ticket_path():
    tree = ast.parse(Path(netboot_module.__file__).read_text())
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names}
    assert not {name for name in imported if name.startswith(("appliance.provision", "player"))}
    assert not hasattr(netboot_module, "BootTicket")
    assert not hasattr(netboot_module, "verify_release")
