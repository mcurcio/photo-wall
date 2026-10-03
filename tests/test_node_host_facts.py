"""Host facts record (console DDD §63-§64, G13, bead F1), contract and node side: the
HostFactsV2 wire rule, the sampler's `facts()` (kernel, default-route interface, operstate,
the fib_trie address) and Host Management's in-memory sender. No database; Central's ingest
and G12 are proven in tests/test_node_host_facts_ingest.py."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from appliance.node.host_linux import LinuxHostSampler
from appliance.node.host_runner import HostRunner
from contracts.node_commands import producer_document
from contracts.node_host_facts import (
    LINK_STATES,
    MAX_HOST_FACTS_BYTES,
    HostFactsV2,
    encode_host_facts,
    parse_host_facts,
    stored_fact_values,
)
from contracts.node_observation import HOST_OBSERVATION_INTERVAL_SECONDS
from contracts.node_protocol import NodeProducerV2

INTERVAL = HOST_OBSERVATION_INTERVAL_SECONDS


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
                                       "link_state": "up", "address": "192.168.1.40", "base_tag": None}
    assert stored_fact_values(json.dumps(_document(link_state="UP")).encode())["link_state"] is None
    assert tuple(stored_fact_values(encode_host_facts(_facts())).values()) == _facts().values()
    assert set(stored_fact_values(b"not json").values()) == {None}


REFUSED = {
    "a space in the kernel release": {"kernel_release": "6.6 51"},
    "a 65-character kernel release": {"kernel_release": "6" * 65},
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


def _host(tmp_path, *, routes=ROUTES, trie=FIB_TRIE, release="6.6.51+rpt-rpi-v8\n", operstate="up\n"):
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
    return LinuxHostSampler(proc, tmp_path, sys_root)


def test_a_fake_fib_trie_and_route_table_yield_the_interfaces_address(tmp_path):
    assert _host(tmp_path).facts() == {"kernel_release": "6.6.51+rpt-rpi-v8", "interface": "eth0",
                                       "link_state": "up", "address": "192.168.1.40"}


def test_two_candidate_addresses_yield_null(tmp_path):
    trie = FIB_TRIE.replace("Local:", "        |-- 192.168.1.41\n           /32 host LOCAL\nLocal:")
    assert _host(tmp_path, trie=trie).facts()["address"] is None


def test_an_address_outside_the_interfaces_own_routes_is_not_a_candidate(tmp_path):
    trie = FIB_TRIE.replace("192.168.1.40", "10.0.0.5")
    assert _host(tmp_path, trie=trie).facts()["address"] is None


def test_each_unreadable_field_is_null(tmp_path):
    facts = _host(tmp_path, trie=None, release=None, operstate=None).facts()
    assert facts == {"kernel_release": None, "interface": "eth0", "link_state": None, "address": None}
    assert _host(tmp_path / "b", routes=None).facts() == {
        "kernel_release": "6.6.51+rpt-rpi-v8", "interface": None, "link_state": None, "address": None}


def test_a_field_the_contract_refuses_is_null_not_a_broken_record(tmp_path):
    facts = _host(tmp_path, release="6.6 51\n", operstate="weird\n").facts()
    assert facts["kernel_release"] is None and facts["link_state"] is None
    assert facts["interface"] == "eth0" and facts["address"] == "192.168.1.40"


# Node: the sender (§64 state machine).

class _Store:
    failed = False

    def write(self, name, value):
        pass


def _runner(monkeypatch, clock, facts, answers):
    """A HostRunner with fake session and sampler. `facts` is the sampler's current values
    (mutable); `answers` maps a path to the status it answers, or an exception to raise."""
    runner = HostRunner.__new__(HostRunner)
    runner.monotonic = lambda: clock[0]
    runner.recovery = SimpleNamespace(telemetry=lambda: ((), None))
    runner.store = _Store()
    runner.sampler = SimpleNamespace(sample=lambda: (("uptime", 1, "seconds"),), throttling=lambda: (),
                                     supervision=lambda: (), facts=lambda: dict(facts))
    runner.delivery = SimpleNamespace(flush=lambda *args, **kwargs: 0)
    sequence = iter(range(1, 10_000))
    runner.journal = SimpleNamespace(next_sequence=lambda: next(sequence))
    sent = []

    def request(method, path, body=None):
        if path == "/v2/node/host-facts":
            sent.append((clock[0], body))
        answer = answers.get(path, 200)
        if isinstance(answer, Exception):
            raise answer
        return answer, b""

    producer = _producer()
    runner.core = SimpleNamespace(producer=producer, session_id=uuid4())
    runner.session = SimpleNamespace(claim=SimpleNamespace(session_id=runner.core.session_id),
                                     request=request)
    runner.establish_session = lambda: True
    monkeypatch.setattr("appliance.node.host_runner.boottime_ms", lambda: int(clock[0] * 1000))
    return runner, sent


VALUES = {"kernel_release": "6.6.51+rpt-rpi-v8", "interface": "eth0", "link_state": "up",
          "address": "192.168.1.40"}


def _run(runner, clock, until):
    while clock[0] < until:
        runner.tick()
        clock[0] += 2


def test_facts_are_sent_once_per_process_start_and_unchanged_facts_send_nothing(monkeypatch):
    clock, facts = [100.0], dict(VALUES)
    runner, sent = _runner(monkeypatch, clock, facts, {})
    _run(runner, clock, 200)
    assert len(sent) == 1 and sent[0][0] == 100.0
    assert parse_host_facts(sent[0][1]).values() == (*VALUES.values(), None)
    # A process start (a new runner) sends again; Central answers duplicate or keeps
    # first_received_at.
    again, resent = _runner(monkeypatch, clock, facts, {})
    _run(again, clock, 210)
    assert len(resent) == 1


@pytest.mark.parametrize(("configured", "reported"), [("2026.10.01", "2026.10.01"), ("bad tag", None),
                                                     (None, None)])
def test_the_runner_reports_its_configured_base_tag_and_nulls_one_the_contract_refuses(
        monkeypatch, configured, reported):
    # The node's own record of its base (host.json's base_tag, from the boot handoff) rides in
    # the facts record; a value the contract refuses becomes null, never a broken record.
    clock, facts = [100.0], dict(VALUES)
    runner, sent = _runner(monkeypatch, clock, facts, {})
    runner.base_tag = configured
    _run(runner, clock, 110)
    assert len(sent) == 1 and parse_host_facts(sent[0][1]).base_tag == reported


def test_a_change_sends_a_new_document_and_a_flapping_link_at_most_once_per_interval(monkeypatch):
    clock, facts = [100.0], dict(VALUES)
    runner, sent = _runner(monkeypatch, clock, facts, {})
    _run(runner, clock, 120)
    facts["address"] = "192.168.1.41"
    _run(runner, clock, 140)
    assert len(sent) == 2
    first, second = (parse_host_facts(body) for _, body in sent)
    assert second.address == "192.168.1.41" and second.sequence > first.sequence
    # The link changes on every 2 s tick: facts are read only once per observation post.
    sent.clear()
    states, ticks = ("up", "down", "dormant"), 0
    while clock[0] < 400:
        facts["link_state"] = states[ticks % 3]
        ticks += 1
        runner.tick()
        clock[0] += 2
    times = [at for at, _ in sent]
    assert ticks > 100 and len(times) >= 10
    assert all(later - earlier >= INTERVAL for earlier, later in zip(times, times[1:]))


@pytest.mark.parametrize("failure", [OSError("no answer"), 500, 503, 429], ids=str)
def test_a_failed_send_repeats_the_same_document_at_the_next_post(monkeypatch, failure):
    clock, facts = [100.0], dict(VALUES)
    answers = {"/v2/node/host-facts": failure}
    runner, sent = _runner(monkeypatch, clock, facts, answers)
    _run(runner, clock, 100 + 3 * INTERVAL + 1)
    assert len(sent) >= 3 and len({body for _, body in sent}) == 1
    times = [at for at, _ in sent]
    assert all(later - earlier >= INTERVAL for earlier, later in zip(times, times[1:]))
    answers["/v2/node/host-facts"] = 200
    _run(runner, clock, clock[0] + 3 * INTERVAL)
    assert len({body for _, body in sent}) == 1
    count = len(sent)
    _run(runner, clock, clock[0] + 3 * INTERVAL)
    assert len(sent) == count  # stored: nothing more until a value changes


def test_404_stops_sending_for_the_retry_period_even_when_values_change(monkeypatch):
    clock, facts = [100.0], dict(VALUES)
    answers = {"/v2/node/host-facts": 404}
    runner, sent = _runner(monkeypatch, clock, facts, answers)
    _run(runner, clock, 140)
    facts["link_state"] = "down"
    _run(runner, clock, 200)
    assert len(sent) == 1
    restarted, resent = _runner(monkeypatch, clock, facts, {})
    _run(restarted, clock, 210)
    assert len(resent) == 1
    # A replica without the route (rolling deploy, rollback) is not final for the boot: after
    # the retry period the current document is sent again, and stored.
    answers["/v2/node/host-facts"] = 200
    _run(runner, clock, 100 + HostRunner.FACTS_ROUTE_RETRY_SECONDS - 2 * INTERVAL)
    assert len(sent) == 1
    _run(runner, clock, 100 + HostRunner.FACTS_ROUTE_RETRY_SECONDS + 2 * INTERVAL)
    assert len(sent) == 2 and parse_host_facts(sent[1][1]).link_state == "down"
    count = len(sent)
    _run(runner, clock, clock[0] + 3 * INTERVAL)
    assert len(sent) == count


@pytest.mark.parametrize("refused", [401, 403])
def test_a_refused_session_resends_the_same_document_once_re_enrolled(monkeypatch, refused):
    """401 `node_session_unavailable` (an expired session) is temporary: the session re-enrolls
    under the same producer, and the same document is resent, not dropped for the boot."""
    clock, facts = [100.0], dict(VALUES)
    answers = {"/v2/node/host-facts": refused}
    runner, sent = _runner(monkeypatch, clock, facts, answers)
    _run(runner, clock, 102)
    answers["/v2/node/host-facts"] = 200
    _run(runner, clock, 100 + 2 * INTERVAL + 2)
    assert len(sent) == 2 and sent[0][1] == sent[1][1]
    count = len(sent)
    _run(runner, clock, clock[0] + 3 * INTERVAL)
    assert len(sent) == count  # stored


def test_another_4xx_drops_until_the_next_value_change(monkeypatch):
    clock, facts = [100.0], dict(VALUES)
    answers = {"/v2/node/host-facts": 422}
    runner, sent = _runner(monkeypatch, clock, facts, answers)
    _run(runner, clock, 160)
    assert len(sent) == 1
    answers["/v2/node/host-facts"] = 200
    facts["interface"] = "wlan0"
    _run(runner, clock, 200)
    assert len(sent) == 2 and parse_host_facts(sent[1][1]).interface == "wlan0"
