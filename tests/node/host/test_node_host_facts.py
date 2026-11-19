"""Host facts record (console DDD §63-§64, G13, bead F1), contract and node side: the
HostFactsV2 wire rule, the sampler's `facts()` (kernel, default-route interface, operstate,
the fib_trie address) and what Host Management states of them on its line (decision 0034: it posts
nothing). No database; Central's ingest and G12 are proven in tests/test_node_host_facts_ingest.py."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from appliance.host.host_linux import LinuxHostSampler, reset_cause
from appliance.host.host_runner import HostRunner
from contracts.node_commands import producer_document
from contracts.node_host_facts import (
    LINK_STATES,
    MAX_HOST_FACTS_BYTES,
    RESET_CAUSES,
    WALL_CLOCK_STATES,
    BootReportV2,
    BootStageV2,
    HostFactsV2,
    encode_host_facts,
    fact_values_document,
    parse_host_facts,
    stored_fact_values,
    valid_fact,
)
from contracts.node_protocol import NodeProducerV2


def _producer(owner="host_core"):
    return NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), owner, uuid4())


def _facts(**fields):
    values = {"kernel_release": "6.6.51+rpt-rpi-v8", "interface": "eth0", "link_state": "up",
              "address": "192.168.1.40", "base_tag": "2026.10.01", **fields}
    return HostFactsV2(_producer(), 3, 1234, **values)


def _document(**fields):
    return {**json.loads(encode_host_facts(_facts())), **fields}


# Contract.

def test_round_trip_with_a_plus_in_the_kernel_release_and_nulls():
    value = _facts()
    raw = encode_host_facts(value)
    assert parse_host_facts(raw) == value
    document = json.loads(raw)
    assert document["schema"] == 2 and document["kind"] == "host_facts"
    assert document["base_tag"] == "2026.10.01" and "base" not in document
    assert raw == json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    assert len(raw) <= MAX_HOST_FACTS_BYTES
    empty = _facts(kernel_release=None, interface=None, link_state=None, address=None, base_tag=None)
    assert parse_host_facts(encode_host_facts(empty)) == empty
    assert LINK_STATES == {"up", "down", "dormant", "lowerlayerdown", "notpresent", "testing", "unknown"}


def test_a_stored_row_of_an_older_shape_reads_its_missing_or_refused_facts_as_none():
    # Central's read side: one old row must not fail the fleet read (G12); ingest stays strict.
    old = _document()
    del old["base_tag"]
    raw = json.dumps(old).encode()
    with pytest.raises(ValueError):
        parse_host_facts(raw)
    assert stored_fact_values(raw) == {"kernel_release": "6.6.51+rpt-rpi-v8", "interface": "eth0",
                                       "link_state": "up", "address": "192.168.1.40", "base_tag": None,
                                       "reset_cause": None, "wall_clock": None, "boot": None}
    assert stored_fact_values(json.dumps(_document(link_state="UP")).encode())["link_state"] is None
    assert stored_fact_values(encode_host_facts(_facts())) == fact_values_document(_facts())
    assert set(stored_fact_values(b"not json").values()) == {None}


# Why this boot began: the hardware watchdog's reset (issue #125).

def test_the_reset_cause_round_trips_and_is_one_of_its_causes():
    assert RESET_CAUSES == ("watchdog", "other")
    for cause in (*RESET_CAUSES, None):
        value = _facts(reset_cause=cause)
        raw = encode_host_facts(value)
        assert parse_host_facts(raw) == value and json.loads(raw)["reset_cause"] == cause
        assert stored_fact_values(raw) == fact_values_document(value)
        assert fact_values_document(value)["reset_cause"] == cause
    with pytest.raises(ValueError, match="^invalid_host_fact_reset_cause$"):
        _facts(reset_cause="panic")
    for refused in ("panic", 1):
        with pytest.raises(ValueError):
            parse_host_facts(json.dumps(_document(reset_cause=refused)).encode())
    # The field is part of the record: a document without it is refused at ingest, and a stored row
    # without it (or with a refused cause) reads as not read.
    old = _document()
    del old["reset_cause"]
    with pytest.raises(ValueError):
        parse_host_facts(json.dumps(old).encode())
    assert stored_fact_values(json.dumps(old).encode())["reset_cause"] is None
    assert stored_fact_values(json.dumps(_document(reset_cause="panic")).encode())["reset_cause"] is None


# The wall clock as the kernel judges it (decision 0032).

def test_the_wall_clock_round_trips_and_is_one_of_its_states():
    """Mutation probe: drop `wall_clock` from FACT_RULES -> "maybe" is accepted."""
    assert WALL_CLOCK_STATES == ("synchronised", "unsynchronised")
    for state in (*WALL_CLOCK_STATES, None):
        value = _facts(wall_clock=state)
        raw = encode_host_facts(value)
        assert parse_host_facts(raw) == value and json.loads(raw)["wall_clock"] == state
        assert stored_fact_values(raw) == fact_values_document(value)
    with pytest.raises(ValueError, match="^invalid_host_fact_wall_clock$"):
        _facts(wall_clock="maybe")
    for refused in ("maybe", 1):
        with pytest.raises(ValueError):
            parse_host_facts(json.dumps(_document(wall_clock=refused)).encode())
    old = _document()
    del old["wall_clock"]
    with pytest.raises(ValueError):
        parse_host_facts(json.dumps(old).encode())
    assert stored_fact_values(json.dumps(old).encode())["wall_clock"] is None


def test_the_kernels_wall_clock_verdict_reaches_the_facts_record(monkeypatch, tmp_path):
    clock = [100.0]
    runner, stated = _runner(monkeypatch, clock, {**VALUES, "wall_clock": "unsynchronised"})
    _run(runner, clock, 110)
    assert [state.wall_clock for _, state in stated] == ["unsynchronised"]


def _watchdog(tmp_path, bootstatus, options="0x8120"):
    """A watchdog's sysfs directory: `bootstatus` and `options` as the driver prints them (None:
    absent). 0x8120 is KEEPALIVEPING | SETTIMEOUT | CARDRESET; the Pi 5's bcm2835-wdt prints 0x8180."""
    directory = tmp_path / "watchdog0"
    directory.mkdir()
    if bootstatus is not None:
        (directory / "bootstatus").write_text(bootstatus)
    if options is not None:
        (directory / "options").write_text(options)
    return directory / "bootstatus"


@pytest.mark.parametrize(("bootstatus", "options", "cause"), [
    ("32\n", "0x8120\n", "watchdog"),     # WDIOF_CARDRESET set
    ("288\n", "0x8120\n", "watchdog"),    # with another bit beside it
    ("0\n", "0x8120\n", "other"),
    ("256\n", "0x8120\n", "other"),
    ("32\n", None, "watchdog"),            # no options file: bootstatus alone
    ("0\n", "0x8180\n", None),            # the Pi 5: its driver cannot report a card reset
    ("32\n", "0x8180\n", None),
    (None, "0x8120\n", None),              # no watchdog
    ("garbage\n", "0x8120\n", None),
])
def test_the_host_reads_the_watchdogs_bootstatus(tmp_path, bootstatus, options, cause):
    assert reset_cause(_watchdog(tmp_path, bootstatus, options)) == cause


def test_a_watchdog_start_reaches_the_facts_record(monkeypatch, tmp_path):
    clock = [100.0]
    runner, stated = _runner(monkeypatch, clock, dict(VALUES))
    runner.watchdog_bootstatus = _watchdog(tmp_path, "32\n")
    _run(runner, clock, 110)
    assert [state.reset_cause for _, state in stated] == ["watchdog"]


# The boot stage report (docs/node-4gb-memory-design.md §4.3 T2).

REFUSED_STORAGE = BootStageV2("storage", "refused", "node_memory_class", 3758096384, 2147483648)
BOOT = BootReportV2((BootStageV2("handoff", "done", None, None, None), REFUSED_STORAGE),
                    ("photo-wall-node-storage.service",), 0)


def test_a_boot_report_round_trips_and_a_document_without_boot_encodes_as_before():
    value = _facts(boot=BOOT)
    raw = encode_host_facts(value)
    assert parse_host_facts(raw) == value
    assert json.loads(raw)["boot"] == {
        "stages": [{"stage": "handoff", "state": "done", "fault": None, "required_bytes": None,
                    "room_bytes": None},
                   {"stage": "storage", "state": "refused", "fault": "node_memory_class",
                    "required_bytes": 3758096384, "room_bytes": 2147483648}],
        "failed_units": ["photo-wall-node-storage.service"], "failed_units_more": 0}
    # An older node's document (no `boot` key) still validates, and boot=None omits the key.
    assert "boot" not in json.loads(encode_host_facts(_facts()))
    assert parse_host_facts(encode_host_facts(_facts())).boot is None
    assert fact_values_document(value) != fact_values_document(_facts())


def test_the_worst_case_boot_report_fits_the_facts_record():
    stages = tuple(BootStageV2(stage, "refused", "f" * 64, 2**63 - 1, 2**63 - 1)
                   for stage in ("handoff", "storage", "prepare"))
    units = tuple(sorted(f"photo-wall-{index}" + "u" * 76 + ".service" for index in range(4)))
    assert all(len(unit) == 96 for unit in units)
    producer = NodeProducerV2("s" * 256, "device-" + "a" * 64, 2**63 - 1, uuid4(), "host_core", uuid4())
    # The kernel release's worst case is its longest-encoding allowed character, 64 times.
    kernel = max((chr(code) for code in range(128) if valid_fact("kernel_release", chr(code))),
                 key=lambda character: len(json.dumps(character)))
    worst = HostFactsV2(producer, 2**63 - 1, 2**63 - 1, kernel * 64, "e" * 15, "lowerlayerdown",
                        "255.255.255.255", "t" * 128, BootReportV2(stages, units, 2**63 - 1), "watchdog")
    assert len(encode_host_facts(worst)) <= MAX_HOST_FACTS_BYTES


@pytest.mark.parametrize("stage", [
    ("boot", "done", None, None, None),  # unknown stage
    ("storage", "stopped", None, None, None),  # unknown state
    ("storage", "failed", None, None, None),  # failed without a fault
    ("storage", "failed", "f" * 65, None, None),  # fault past 64
    ("storage", "failed", "os:ENOSPC", 1, 2),  # numbers on a failure
    ("storage", "refused", "node_memory_class", None, 2),  # refusal without required
    ("storage", "refused", "node_memory_class", 1, -1),  # negative room
    ("storage", "refused", "node_memory_class", True, 1),  # bool is not a count
    ("storage", "running", "node_memory_class", None, None),  # fault on a running stage
    ("prepare", "done", None, 0, None),  # numbers on done
])
def test_a_malformed_boot_stage_is_refused(stage):
    with pytest.raises(ValueError, match="invalid_boot_stage"):
        BootStageV2(*stage)


DONE = {name: BootStageV2(name, "done", None, None, None) for name in ("handoff", "storage", "prepare")}


@pytest.mark.parametrize("report", [
    ((DONE["storage"], DONE["handoff"]), (), 0),  # out of order
    ((DONE["handoff"], DONE["handoff"]), (), 0),  # repeated
    ([DONE["handoff"]], (), 0),  # not a tuple
    ((), ("b.service", "a.service"), 0),  # unsorted
    ((), ("a.service", "a.service"), 0),  # repeated unit
    ((), tuple(f"u{index}.service" for index in range(5)), 0),  # more than four
    ((), ("u" * 97,), 0),  # a 97-character name
    ((), ("photo-wall-x@1 .service",), 0),  # a name the token rule refuses
    ((), (), -1),
    ((), None, 0),  # "not read" units with a count
    ((), (), None),  # a list with no count
    ((), [], 0),  # not a tuple
])
def test_a_malformed_boot_report_is_refused(report):
    with pytest.raises(ValueError, match="invalid_boot_report"):
        BootReportV2(*report)


@pytest.mark.parametrize("boot", [None, [], {"stages": [], "failed_units": []},
                                  {"stages": [{"stage": "storage"}], "failed_units": [],
                                   "failed_units_more": 0}])
def test_ingest_refuses_a_malformed_boot_document(boot):
    with pytest.raises(ValueError, match="invalid_boot"):
        parse_host_facts(json.dumps(_document(boot=boot)).encode())
    with pytest.raises(ValueError, match="invalid_host_facts"):
        HostFactsV2(_producer(), 3, 1234, None, None, None, None, None, boot={"stages": []})


def test_units_not_read_round_trip_as_null_never_as_none_failed():
    # Errata E-T3-2: a failed-units list that was never read is null on the wire, distinct from
    # an empty list ("nothing failed"), and stored and posted documents compare equal.
    unread = _facts(boot=BootReportV2(BOOT.stages, None, None))
    raw = encode_host_facts(unread)
    assert json.loads(raw)["boot"]["failed_units"] is None
    assert json.loads(raw)["boot"]["failed_units_more"] is None
    assert parse_host_facts(raw) == unread
    assert stored_fact_values(raw) == fact_values_document(unread)
    assert fact_values_document(unread) != fact_values_document(_facts(boot=BootReportV2(BOOT.stages, (), 0)))


def test_stored_values_equal_the_posted_document_with_and_without_boot():
    # Central's "values unchanged" compares these two (central/fleet/node_observations.py).
    for value in (_facts(), _facts(boot=BOOT)):
        assert stored_fact_values(encode_host_facts(value)) == fact_values_document(value)
    # A row stored before `boot` existed equals a post that read none, and differs from one
    # that did; an invalid stored `boot` reads as None and never fails the read.
    old = json.dumps(_document()).encode()
    assert stored_fact_values(old) == fact_values_document(_facts())
    assert stored_fact_values(old) != fact_values_document(_facts(boot=BOOT))
    assert stored_fact_values(json.dumps(_document(boot={"stages": 1})).encode())["boot"] is None
    assert fact_values_document(_facts(boot=BOOT)) != fact_values_document(
        _facts(boot=BootReportV2(BOOT.stages, (), 0)))


REFUSED = {
    "a space in the kernel release": {"kernel_release": "6.6 51"},
    "a 65-character kernel release": {"kernel_release": "6" * 65},
    "a quote in the kernel release": {"kernel_release": '6.6"51'},
    "a backslash in the kernel release": {"kernel_release": "6.6\\51"},
    "an unknown link state": {"link_state": "UP"},
    "a non-canonical address": {"address": "192.168.001.040"},
    "an IPv6 address": {"address": "::1"},
    "an interface with a control character": {"interface": "eth\x070"},
    "an interface with a bidi character": {"interface": "eth‮0"},
    "a 16-character interface": {"interface": "e" * 16},
    "an extra key": {"base": "2026.10.01"},
    "a base tag with a space": {"base_tag": "2026 10 01"},
    "a 129-character base tag": {"base_tag": "2" * 129},
    "a missing key": None,
    "another kind": {"kind": "host_observation"},
    "a sequence of 0": {"sequence": 0},
    "an App Manager producer": {"producer": producer_document(_producer("app_manager"))},
}


@pytest.mark.parametrize("change", REFUSED.values(), ids=REFUSED.keys())
def test_the_contract_refuses(change):
    document = _document(**(change or {}))
    if change is None:
        del document["address"]
    with pytest.raises(ValueError):
        parse_host_facts(json.dumps(document).encode())


def test_a_64_character_kernel_release_is_accepted_and_construction_refuses_like_parsing():
    assert _facts(kernel_release="6" * 64).kernel_release == "6" * 64
    with pytest.raises(ValueError):
        _facts(kernel_release="6.6 51")


# Node: the sampler.

ROUTE_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
# 192.168.1.0/24 on eth0 and its default route via 192.168.1.1 (host byte order, little-endian).
ROUTES = (ROUTE_HEADER
          + "eth0\t00000000\t0101A8C0\t0003\t0\t0\t202\t00000000\t0\t0\t0\n"
          + "eth0\t0001A8C0\t00000000\t0001\t0\t0\t202\t00FFFFFF\t0\t0\t0\n")
FIB_TRIE = """Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        +-- 127.0.0.0/31 1 0 0
           |-- 127.0.0.0
              /8 host LOCAL
           |-- 127.0.0.1
              /32 host LOCAL
        |-- 127.255.255.255
           /32 link BROADCAST
     +-- 192.168.1.0/24 2 0 2
        |-- 192.168.1.0
           /24 link UNICAST
        |-- 192.168.1.40
           /32 host LOCAL
        |-- 192.168.1.255
           /32 link BROADCAST
Local:
  +-- 0.0.0.0/0 3 0 5
     |-- 127.0.0.1
        /32 host LOCAL
     |-- 192.168.1.40
        /32 host LOCAL
"""


def _host(tmp_path, *, routes=ROUTES, trie=FIB_TRIE, release="6.6.51+rpt-rpi-v8\n", operstate="up\n",
          wall_clock="synchronised"):
    proc, sys_root = tmp_path / "proc", tmp_path / "sys"
    (proc / "net").mkdir(parents=True)
    (proc / "sys/kernel").mkdir(parents=True)
    if routes is not None:
        (proc / "net/route").write_text(routes)
    if trie is not None:
        (proc / "net/fib_trie").write_text(trie)
    if release is not None:
        (proc / "sys/kernel/osrelease").write_text(release)
    if operstate is not None:
        (sys_root / "class/net/eth0").mkdir(parents=True)
        (sys_root / "class/net/eth0/operstate").write_text(operstate)
    return LinuxHostSampler(proc, tmp_path, sys_root, wall_clock=lambda: wall_clock)


def test_a_fake_fib_trie_and_route_table_yield_the_interfaces_address(tmp_path):
    assert _host(tmp_path).facts() == {"kernel_release": "6.6.51+rpt-rpi-v8", "interface": "eth0",
                                       "link_state": "up", "address": "192.168.1.40",
                                       "wall_clock": "synchronised"}


def test_two_candidate_addresses_yield_null(tmp_path):
    trie = FIB_TRIE.replace("Local:", "        |-- 192.168.1.41\n           /32 host LOCAL\nLocal:")
    assert _host(tmp_path, trie=trie).facts()["address"] is None


def test_an_address_outside_the_interfaces_own_routes_is_not_a_candidate(tmp_path):
    trie = FIB_TRIE.replace("192.168.1.40", "10.0.0.5")
    assert _host(tmp_path, trie=trie).facts()["address"] is None


def test_each_unreadable_field_is_null(tmp_path):
    facts = _host(tmp_path, trie=None, release=None, operstate=None, wall_clock=None).facts()
    assert facts == {"kernel_release": None, "interface": "eth0", "link_state": None, "address": None,
                     "wall_clock": None}
    assert _host(tmp_path / "b", routes=None).facts() == {
        "kernel_release": "6.6.51+rpt-rpi-v8", "interface": None, "link_state": None, "address": None,
        "wall_clock": "synchronised"}


def test_a_field_the_contract_refuses_is_null_not_a_broken_record(tmp_path):
    facts = _host(tmp_path, release="6.6 51\n", operstate="weird\n").facts()
    assert facts["kernel_release"] is None and facts["link_state"] is None
    assert facts["interface"] == "eth0" and facts["address"] == "192.168.1.40"


# Node: what HostRunner states on its line.

def _runner(monkeypatch, clock, facts):
    """A HostRunner over a fake sampler, its line recording each facts state it is given:
    (clock, HostFactsState). `facts` is the sampler's current values (mutable)."""
    stated = []
    line = SimpleNamespace(metrics=lambda state: None,
                           facts=lambda state: stated.append((clock[0], state)))
    runner = HostRunner(line, monotonic=lambda: clock[0])
    runner.sampler = SimpleNamespace(sample=lambda: (("uptime", 1, "seconds"),), throttling=lambda: (),
                                     supervision=lambda: (), memory_rows=lambda: (), facts=lambda: dict(facts))
    runner.watchdog_bootstatus = Path("/nonexistent/watchdog0/bootstatus")   # not read: None
    return runner, stated


VALUES = {"kernel_release": "6.6.51+rpt-rpi-v8", "interface": "eth0", "link_state": "up",
          "address": "192.168.1.40"}


def _run(runner, clock, until):
    while clock[0] < until:
        runner.tick()
        clock[0] += 2


@pytest.mark.parametrize(("configured", "reported"), [("2026.10.01", "2026.10.01"), ("bad tag", None),
                                                     (None, None)])
def test_the_runner_states_its_configured_base_tag_and_nulls_one_the_contract_refuses(
        monkeypatch, configured, reported):
    # The node's own record of its base (host.json's base_tag, from the boot handoff) rides in
    # the facts; a value the contract refuses becomes null, never a broken record.
    clock = [100.0]
    runner, stated = _runner(monkeypatch, clock, dict(VALUES))
    runner.base_tag = configured
    _run(runner, clock, 110)
    assert [state.base_tag for _, state in stated] == [reported]
