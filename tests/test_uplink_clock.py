"""The clock gate over fake tiers, a fake TimeQuery, ManualClock and a fake store: no sockets.
Q5 = A: the one step may go back, never below the floor."""

import errno

import pytest

from contracts.clock_record import ClockRecord, ClockState
from contracts.time import ManualClock
from uplink.causes import Cause, UplinkError
from uplink.clock import (
    DHCP_TIER_BUDGET,
    GATE_BUDGET,
    POOL_HOST,
    POOL_LOOKUP_TIMEOUT,
    ClockGate,
    TimeTier,
    dhcp_tier,
    pool_tier,
    read_floor,
)
from uplink.lookup import LookupTimeout
from uplink.sntp import NTP_PORT, TimeAnswer

FLOOR = 1790380800


class Store:
    def __init__(self) -> None:
        self.written: list[ClockRecord] = []

    def write(self, record: ClockRecord) -> None:
        self.written.append(record)

    def read(self) -> ClockRecord | None:
        return self.written[-1] if self.written else None


class Query:
    """A fake TimeQuery: records each call, advances the clock by `spend` (or to the deadline),
    and answers from a script keyed by the first server."""

    def __init__(self, clock: ManualClock, answers: dict[str, float] | None = None,
                 spend: float | None = None) -> None:
        self.clock, self.answers, self.spend = clock, answers or {}, spend
        self.calls: list[tuple[list, float, int, float]] = []

    def __call__(self, servers, *, deadline, floor):
        self.calls.append((list(servers), deadline, floor, self.clock.monotonic()))
        spend = deadline - self.clock.monotonic() if self.spend is None else self.spend
        self.clock.advance(max(spend, 0.0))
        server = servers[0][0]
        if server in self.answers:
            return TimeAnswer(server, self.answers[server], 0.01), (f"{server}:ok",)
        return None, tuple(f"{address}:timeout" for address, _ in servers)


def tier(name: str, *addresses: str) -> TimeTier:
    return TimeTier(name, lambda timeout: [(address, NTP_PORT) for address in addresses])


def gate(clock: ManualClock, query: Query, *tiers: TimeTier, steps: list | None = None,
         store: Store | None = None) -> ClockGate:
    def step(seconds: float) -> None:
        if steps is not None:
            steps.append(seconds)
        clock.step_utc(seconds)

    return ClockGate(floor=FLOOR, tiers=tiers or (tier("dhcp", "192.0.2.1"),),
                     clock=clock, step=step, store=store or Store(), writer="netboot",
                     query=query)


def test_a_clock_before_the_floor_is_raised_to_it_before_any_tier_runs():
    clock = ManualClock(wall=0)
    query = Query(clock)
    seen: list[float] = []
    record = gate(clock, query, TimeTier("dhcp", lambda timeout: seen.append(clock.utc()) or [])
                  ).settle()
    assert record.raised_to_floor and seen == [FLOOR] and clock.utc() >= FLOOR
    assert record.state is ClockState.UNSYNCED and record.tried == ("dhcp:none",)


@pytest.mark.parametrize(("offset", "state", "stepped", "moved"), [
    (3600.0, ClockState.SYNCED, True, 3600.0),
    (0.3, ClockState.SYNCED, False, 0.0),
    (-0.3, ClockState.SYNCED, False, 0.0),
    (-3600.0, ClockState.SYNCED, True, -3600.0),        # Q5 = A: back, still above the floor
    (-86400.0 * 2, ClockState.AHEAD, False, 0.0),       # would cross the floor: not moved
])
def test_the_one_step_follows_the_offset_but_never_goes_below_the_floor(offset, state, stepped,
                                                                        moved):
    clock = ManualClock(wall=FLOOR + 86400)
    steps: list[float] = []
    record = gate(clock, Query(clock, {"192.0.2.1": offset}, spend=0.1), steps=steps).settle()
    assert (record.state, record.stepped, record.offset) == (state, stepped, offset)
    assert (record.tier, record.source) == ("dhcp", "192.0.2.1")
    assert steps == ([moved] if moved else [])
    assert clock.utc() == pytest.approx(FLOOR + 86400 + moved)


def test_an_empty_dhcp_tier_spends_no_budget_and_the_pool_gets_all_of_it():
    clock = ManualClock(wall=FLOOR, mono=50.0)
    query = Query(clock)
    record = gate(clock, query, tier("dhcp"), tier("pool", "198.51.100.1")).settle()
    assert [(call[0], call[1], call[3]) for call in query.calls] == [
        ([("198.51.100.1", NTP_PORT)], 50.0 + GATE_BUDGET, 50.0)]
    assert record.tried == ("dhcp:none", "pool:198.51.100.1:timeout")


def test_a_dhcp_tier_with_servers_gets_at_most_its_share():
    clock = ManualClock(wall=FLOOR, mono=50.0)
    query = Query(clock)
    record = gate(clock, query, tier("dhcp", "192.0.2.1", "192.0.2.2"),
                  tier("pool", "198.51.100.1")).settle()
    (dhcp_servers, dhcp_deadline, _, _), (_, pool_deadline, _, pool_start) = query.calls
    assert dhcp_deadline == 50.0 + DHCP_TIER_BUDGET and pool_start == dhcp_deadline
    assert pool_deadline == 50.0 + GATE_BUDGET
    assert record.state is ClockState.UNSYNCED


def test_nothing_answering_is_unsynced_within_the_budget_and_never_raises():
    clock = ManualClock(wall=FLOOR - 5, mono=0.0)
    query = Query(clock)
    record = gate(clock, query, tier("dhcp", "192.0.2.1"), tier("pool", "198.51.100.1")).settle()
    assert record.state is ClockState.UNSYNCED and not record.stepped
    assert clock.monotonic() <= GATE_BUDGET
    for _, deadline, floor, start in query.calls:
        assert start < deadline <= GATE_BUDGET and floor == FLOOR


def test_a_failed_pool_lookup_is_named_and_not_raised():
    clock = ManualClock(wall=FLOOR)

    def broken(timeout: float):
        raise LookupTimeout(errno.ETIMEDOUT, "slow")

    record = gate(clock, Query(clock), tier("dhcp"), TimeTier("pool", broken)).settle()
    assert record.tried == ("dhcp:none", "pool:lookup:ETIMEDOUT")


def test_a_refused_step_is_recorded_not_raised():
    clock = ManualClock(wall=0)

    def refuse(seconds: float) -> None:
        raise PermissionError(errno.EPERM, "no CAP_SYS_TIME")

    record = ClockGate(floor=FLOOR, tiers=[tier("dhcp", "192.0.2.1")], clock=clock, step=refuse,
                       store=Store(), writer="netboot",
                       query=Query(clock, {"192.0.2.1": FLOOR + 10.0})).settle()
    assert not record.raised_to_floor and not record.stepped
    assert record.state is ClockState.UNSYNCED
    assert record.tried == ("floor:EPERM", "dhcp:192.0.2.1:ok", "step:EPERM")


def test_a_second_settle_returns_the_first_record_without_asking_again():
    clock = ManualClock(wall=FLOOR)
    query, store = Query(clock, {"192.0.2.1": 2.0}, spend=0.1), Store()
    clock_gate = gate(clock, query, store=store)
    first = clock_gate.settle()
    assert clock_gate.settle() is first
    assert len(query.calls) == 1 and store.written == [first]
    assert first.writer == "netboot" and first.written_at == pytest.approx(clock.utc())


def test_the_floor_is_one_decimal_integer(tmp_path):
    path = tmp_path / "clock-floor"
    path.write_text(f"{FLOOR}\n")
    assert read_floor(path) == FLOOR


@pytest.mark.parametrize("content", [None, b"", b"soon", b"-1", b"1.5", b"\xff"])
def test_a_missing_or_malformed_floor_is_a_broken_build(tmp_path, content):
    path = tmp_path / "clock-floor"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(UplinkError) as caught:
        read_floor(path)
    assert (caught.value.cause, caught.value.reason) == (Cause.CONFIGURATION, "floor")


def test_the_dhcp_tier_reads_up_to_three_usable_servers_on_port_123(tmp_path):
    path = tmp_path / "ntp_servers"
    path.write_text("192.0.2.1\n0.0.0.0\n255.255.255.255\nnot-an-ip\n192.0.2.2\n192.0.2.3\n"
                    "192.0.2.4\n")
    assert dhcp_tier(path).servers(3.0) == [("192.0.2.1", 123), ("192.0.2.2", 123),
                                            ("192.0.2.3", 123)]
    assert dhcp_tier(tmp_path / "absent").servers(3.0) == []
    (tmp_path / "empty").write_text("0.0.0.0\n0.0.0.0\n0.0.0.0\n")
    assert dhcp_tier(tmp_path / "empty").servers(3.0) == []


def test_the_pool_tier_looks_up_at_most_four_addresses_on_port_123(monkeypatch):
    calls = []

    def fake_lookup(host, port, timeout):
        calls.append((host, port, timeout))
        return [(2, f"198.51.100.{index}") for index in (1, 1, 2, 3, 4, 5)]

    monkeypatch.setattr("uplink.clock.lookup", fake_lookup)
    assert pool_tier().servers(9.0) == [(f"198.51.100.{index}", 123) for index in (1, 2, 3, 4)]
    assert pool_tier().servers(1.0) and calls == [(POOL_HOST, 123, POOL_LOOKUP_TIMEOUT),
                                                  (POOL_HOST, 123, 1.0)]
    assert POOL_HOST == "debian.pool.ntp.org"
