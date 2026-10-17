"""One DDC/CI port shared by power and picture: power always first, a picture command's hold
bounded (decision 0022, "Picture never costs power"; the picture split, slice S1;
appliance/display_host/ddc.py).

The port runs a real fake `ddcutil` (node.display.fake_ddcutil) through the production `run_tool`
and the real `DdcChannel`; the lock's order is checked on the real `PortLock` with threads.
"""
from __future__ import annotations

import threading
import time

import pytest
from node.display.fake_ddcutil import OTHER, OUTPUT, Display

from appliance.display_host.ddc import (
    PICTURE_HOLD_SECONDS,
    PortLock,
    Urgency,
    VcpByte,
    VcpReading,
    parse_terse,
)
from appliance.display_host.output_power import PROBE_SECONDS
from appliance.display_host.picture import DdcPictureAdapter
from appliance.display_host.picture_list import ASK_BATCH, PictureListAsker
from appliance.display_host.power_methods import DdcCiAdapter
from contracts.node_output import Power, PowerResult


@pytest.fixture
def display(tmp_path):
    return Display(tmp_path)


def until(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# The lock ---------------------------------------------------------------------------------------

def test_a_power_waiter_takes_the_port_before_an_earlier_picture_waiter():
    """Mutation probe (primary): hand the port to waiters in arrival order regardless of urgency
    -> the PICTURE waiter queued first takes it first."""
    lock = PortLock()
    assert lock.acquire(Urgency.PICTURE, 1)              # a picture command holds the port
    order: list[str] = []

    def wait(name: str, urgency: Urgency) -> None:
        if lock.acquire(urgency, 5):
            order.append(name)
            lock.release()

    picture = threading.Thread(target=wait, args=("picture", Urgency.PICTURE))
    picture.start()
    until(lambda: len(lock._waiting[Urgency.PICTURE]) == 1)
    power = threading.Thread(target=wait, args=("power", Urgency.POWER))
    power.start()
    until(lambda: len(lock._waiting[Urgency.POWER]) == 1)
    lock.release()
    picture.join(5)
    power.join(5)
    assert order == ["power", "picture"]


def test_picture_waiters_go_in_arrival_order_and_a_waiter_gives_up_at_its_timeout():
    lock = PortLock()
    assert lock.acquire(Urgency.POWER, 1)
    order: list[int] = []

    def wait(index: int) -> None:
        if lock.acquire(Urgency.PICTURE, 5):
            order.append(index)
            lock.release()

    threads = []
    for index in range(3):
        threads.append(threading.Thread(target=wait, args=(index,)))
        threads[-1].start()
        until(lambda count=index + 1: len(lock._waiting[Urgency.PICTURE]) == count)
    began = time.monotonic()
    assert not lock.acquire(Urgency.POWER, 0.2)          # held: gives up, takes nothing
    assert 0.15 < time.monotonic() - began < 1.0
    lock.release()
    for thread in threads:
        thread.join(5)
    assert order == [0, 1, 2]
    with pytest.raises(RuntimeError):
        lock.release()                                   # not held


# The terse format -------------------------------------------------------------------------------

def test_the_terse_format_answers_levels_and_bytes_and_nothing_else():
    assert parse_terse("VCP 10 C 60 100\nVCP 14 SNC x05\nVCP 16 ERR\nVCP 0b CNC x00 x01 x02 x03\n"
                       "VCP 12 C 70 50\nnoise\nvcp dc snc x0A\n") == {
        "10": VcpReading(60, 100), "14": VcpByte(5), "DC": VcpByte(10)}   # 12's current is past its maximum
    assert parse_terse("") == {}
    for value in (-1, 256, True):
        with pytest.raises(ValueError, match="^vcp_byte$"):
            VcpByte(value)


def test_the_channel_reads_continuous_codes_and_several_at_once(display):
    display.script(snc={"14": 5})
    channel = display.channel
    assert channel.bus(OUTPUT) == 13 and channel.bus(OTHER) == 14 and channel.bus("Virtual-1") is None
    assert channel.get_continuous(OUTPUT, "10", urgency=Urgency.PICTURE, timeout=5) == VcpReading(60, 100)
    assert channel.get_continuous(OUTPUT, "D6", urgency=Urgency.POWER, timeout=5) is None   # not continuous
    assert channel.read_many(OUTPUT, ("10", "12", "14", "16"), urgency=Urgency.PICTURE, timeout=5) == {
        "10": VcpReading(60, 100), "12": VcpReading(50, 100), "14": VcpByte(5)}
    assert channel.read_many("Virtual-1", ("10",), urgency=Urgency.PICTURE, timeout=5) is None   # no bus
    display.script(answer=False)                          # finished, answered nothing: known, empty
    assert channel.read_many(OUTPUT, ("10",), urgency=Urgency.PICTURE, timeout=5) == {}


# Power first on the port ------------------------------------------------------------------------

def test_a_picture_command_is_cut_at_the_hold(display):
    """A fake ddcutil that sleeps 4 s on a picture getvcp is cut at PICTURE_HOLD_SECONDS, however
    long the caller would wait. Mutation probe: remove the PICTURE cap in `_command` -> the read
    runs its 4 s."""
    display.script(hang=["10"], hang_seconds=4)
    began = time.monotonic()
    assert display.channel.read_many(OUTPUT, ("10",), urgency=Urgency.PICTURE, timeout=10) is None
    assert time.monotonic() - began < PICTURE_HOLD_SECONDS + 0.5


def picture_runs(runs) -> int:
    """How many of the channel's runs (`Display.runs`) are picture commands (all but power's D6)."""
    return sum("D6" not in command for _, command in runs)


def power_taken(runs, began: float) -> float:
    """Seconds from `began` until the first power (D6) command took the port, asserting that no
    picture command took it in between: power goes first, however many picture waiters queue."""
    after = [(at, command) for at, command in runs if at >= began]
    index = next(index for index, (_, command) in enumerate(after) if "D6" in command)
    assert picture_runs(after[:index]) == 0, after[:index + 1]
    return after[index][0] - began


@pytest.mark.parametrize("read_seconds", [1.0, PICTURE_HOLD_SECONDS + 1.0], ids=["finishes", "cut"])
def test_a_power_probe_answers_within_one_hold_while_the_list_is_asked_back_to_back(display, read_seconds):
    """With the asker running batches back to back (each holding the port about a second, or for
    the whole hold when the display is slower: each batch cut), a power probe with PROBE_SECONDS
    takes the port within PICTURE_HOLD_SECONDS + 0.5 s of its call, no picture command before it.
    Measured where the channel takes the port (`Display.runs`), not at the fake tool's finish: a
    loaded runner is slow to start a process, which is no part of the port's turn (CI: every 1 s
    batch was cut and the wait for two finished batches never ended)."""
    display.script(read_seconds=read_seconds)
    asker = PictureListAsker(display.channel)
    stop = threading.Event()

    def ask_forever() -> None:
        while not stop.is_set():
            asker.ask(OUTPUT)

    asking = [threading.Thread(target=ask_forever) for _ in range(2)]   # a picture waiter always queued
    for thread in asking:
        thread.start()
    try:
        until(lambda: picture_runs(display.runs) >= 2)                  # back to back on the port
        power = DdcCiAdapter(display.channel)
        waited = []
        for _ in range(2):
            began = time.monotonic()
            assert power.probe(OUTPUT, timeout=PROBE_SECONDS)
            waited.append(power_taken(display.runs, began))
        assert max(waited) < PICTURE_HOLD_SECONDS + 0.5, waited
    finally:
        stop.set()
        for thread in asking:
            thread.join(15)
    batches = [command for _, command in display.runs if command[0] == "getvcp" and "D6" not in command]
    assert all(len(command) - 1 <= ASK_BATCH for command in batches)


def test_one_command_at_a_time_per_port_and_ports_do_not_wait_on_each_other(display):
    display.script(write_seconds=0.5)
    power, picture_ddc = DdcCiAdapter(display.channel), DdcPictureAdapter(display.channel)
    calls = [lambda: power.set(OUTPUT, Power.OFF, timeout=10),
             lambda: picture_ddc.write(OUTPUT, "10", 46, timeout=10),
             lambda: picture_ddc.write(OTHER, "12", 40, timeout=10)]
    threads = [threading.Thread(target=call) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    same_port = display.intervals("13")
    assert len(same_port) == 4                          # D6 write + read-back, 10 write + read-back
    assert not any(a[0] < b[1] and b[0] < a[1] for index, a in enumerate(same_port) for b in same_port[index + 1:])
    other_port = display.intervals("14")
    assert any(a[0] < b[1] and b[0] < a[1] for a in same_port for b in other_port)


def test_a_call_that_waits_past_its_timeout_for_the_port_runs_nothing(display):
    display.script(write_seconds=1.2)
    writing = threading.Thread(target=lambda: display.channel.set(OUTPUT, "10", 46, urgency=Urgency.POWER,
                                                                  timeout=10))
    writing.start()
    until(lambda: display.calls(), timeout=5)
    began = time.monotonic()
    assert display.channel.get_continuous(OUTPUT, "10", urgency=Urgency.POWER, timeout=0.3) is None
    assert time.monotonic() - began < 1.0
    writing.join(5)
    assert len(display.intervals("13")) == 1


# Each caller's turn on the port; a whole power operation first (the fix pass after review) -------

class RecordingChannel:
    """A DdcChannel stand-in that records the urgency of every call and answers as the test monitor."""

    def __init__(self) -> None:
        self.urgencies: list[tuple[str, Urgency]] = []

    def bus(self, output_id):
        return 13

    def get(self, output_id, code, *, urgency, timeout):
        self.urgencies.append(("get", urgency))
        return "VCP D6 SNC x01" if code == "D6" else f"VCP {code} C 60 100"

    def set(self, output_id, code, value, *, urgency, timeout):
        self.urgencies.append(("set", urgency))
        return True

    def read_many(self, output_id, codes, *, urgency, timeout):
        self.urgencies.append(("read_many", urgency))
        return {code: VcpReading(60, 100) for code in codes if code in ("10", "12")}

    def capabilities(self, output_id, *, urgency, timeout):
        self.urgencies.append(("capabilities", urgency))
        return ""


def test_power_calls_at_powers_turn_and_picture_calls_at_pictures():
    """Every caller names its own turn: DdcCiAdapter POWER; PictureListAsker and DdcPictureAdapter
    PICTURE. Mutation probes: DdcCiAdapter at PICTURE, or either picture caller at POWER -> fails."""
    channel = RecordingChannel()
    power = DdcCiAdapter(channel)
    power.read(OUTPUT, timeout=1)
    power.set(OUTPUT, Power.ON, timeout=1)
    assert {urgency for _, urgency in channel.urgencies} == {Urgency.POWER}
    channel.urgencies.clear()
    asker = PictureListAsker(channel)
    offered = asker.ask(OUTPUT)
    asker.read_again(OUTPUT, offered)
    picture_ddc = DdcPictureAdapter(channel)
    picture_ddc.read(OUTPUT, "10", timeout=1)
    picture_ddc.write(OUTPUT, "10", 46, timeout=1)
    assert channel.urgencies and {urgency for _, urgency in channel.urgencies} == {Urgency.PICTURE}


def test_a_whole_power_operation_waits_at_most_one_picture_hold(display):
    """A power set is two commands (setvcp D6, then its read-back), each its own hold; with picture
    reads queued back to back, the read-back must not queue behind another picture hold: no picture
    command takes the port between the two, and the set takes it within one hold of its call.
    Mutation probe: POWER_GRACE_SECONDS 0 -> a picture read runs between the write and its
    read-back."""
    display.script(read_seconds=1.2)
    channel = display.channel
    stop = threading.Event()

    def read_forever() -> None:
        while not stop.is_set():
            channel.read_many(OUTPUT, ("10",), urgency=Urgency.PICTURE, timeout=10)

    reading = [threading.Thread(target=read_forever) for _ in range(3)]   # picture waiters always queued
    for thread in reading:
        thread.start()
    try:
        until(lambda: picture_runs(display.runs) >= 2)
        began = time.monotonic()
        assert DdcCiAdapter(channel).set(OUTPUT, Power.OFF, timeout=10) is PowerResult.CONFIRMED
        waited = power_taken(display.runs, began)
        power = [index for index, (at, command) in enumerate(display.runs) if at >= began and "D6" in command]
        assert [display.runs[index][1][0] for index in power[:2]] == ["setvcp", "getvcp"]
        assert power[1] == power[0] + 1, display.runs[power[0]:power[1] + 1]   # nothing between
        assert waited < PICTURE_HOLD_SECONDS + 0.5, waited
    finally:
        stop.set()
        for thread in reading:
            thread.join(15)
