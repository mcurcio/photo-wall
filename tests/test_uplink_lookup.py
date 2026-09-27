"""lookup()'s bound on abandoned resolver threads: past MAX_PENDING_LOOKUPS a name lookup
times out at once, an IP literal is never affected, and a thread that finishes late frees
its slot."""

import socket
import threading
import time

import pytest

from uplink import lookup as lookup_module
from uplink.lookup import LookupTimeout, lookup

real_addresses = lookup_module._addresses


@pytest.fixture
def blocking_resolver(monkeypatch):
    release = threading.Event()
    calls = []

    def fake(host, port, *, flags=0):
        if flags:
            return real_addresses(host, port, flags=flags)
        calls.append(host)
        release.wait(10)
        return [(socket.AF_INET, "192.0.2.1")]

    monkeypatch.setattr(lookup_module, "_addresses", fake)
    try:
        yield release, calls
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while lookup_module._abandoned != 0 and time.monotonic() < deadline:
            time.sleep(0.01)


def test_a_fifth_lookup_fails_at_once_while_four_are_abandoned(blocking_resolver):
    release, calls = blocking_resolver
    for name in ("a.example", "b.example", "c.example", "d.example"):
        with pytest.raises(LookupTimeout):
            lookup(name, 80, 0.05)
    assert lookup_module._abandoned == 4
    started = time.monotonic()
    with pytest.raises(LookupTimeout):
        lookup("e.example", 80, 5.0)
    assert time.monotonic() - started < 1
    assert calls == ["a.example", "b.example", "c.example", "d.example"]


def test_an_ip_literal_still_resolves_while_four_are_abandoned(blocking_resolver):
    for name in ("a.example", "b.example", "c.example", "d.example"):
        with pytest.raises(LookupTimeout):
            lookup(name, 80, 0.05)
    assert lookup("192.0.2.7", 80, 5.0) == [(socket.AF_INET, "192.0.2.7")]


def test_a_finished_abandoned_lookup_frees_its_slot(blocking_resolver):
    release, _ = blocking_resolver
    for name in ("a.example", "b.example", "c.example", "d.example"):
        with pytest.raises(LookupTimeout):
            lookup(name, 80, 0.05)
    release.set()
    deadline = time.monotonic() + 5
    while lookup_module._abandoned != 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lookup_module._abandoned == 0
    assert lookup("f.example", 80, 5.0) == [(socket.AF_INET, "192.0.2.1")]


def test_a_lookup_that_finishes_in_time_is_never_counted(blocking_resolver):
    release, _ = blocking_resolver
    release.set()
    for index in range(10):
        assert lookup(f"n{index}.example", 80, 5.0) == [(socket.AF_INET, "192.0.2.1")]
        assert lookup_module._abandoned == 0


def test_a_lookup_error_is_still_raised_in_the_caller(monkeypatch):
    def fake(host, port, *, flags=0):
        if flags:
            return real_addresses(host, port, flags=flags)
        raise socket.gaierror(socket.EAI_NONAME, "x")

    monkeypatch.setattr(lookup_module, "_addresses", fake)
    with pytest.raises(socket.gaierror):
        lookup("bad.example", 80, 5.0)
    assert lookup_module._abandoned == 0
