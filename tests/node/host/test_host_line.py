"""What HostCore puts on its host line (appliance/host/bus.py `HostLine`; decision 0034, S4a): its facts
when they change, and its metrics when a band, a flag, a count, the set of metrics or the fault
changes, never more often than every HOST_METRICS_EVERY seconds; a gauge moving inside its band is
never put. HostRunner puts them every tick and makes no HTTP request (decision 0034, S4c)."""
from __future__ import annotations

import http.client
import socket
from types import SimpleNamespace

import pytest

from appliance.host.bus import HostLine
from appliance.host.host_runner import HostRunner
from contracts.node_host_facts import HostFactsState, decode_host_facts_state
from contracts.node_observation import HostMetricsState, decode_host_metrics_state, valid_metrics
from contracts.node_state import HOST_FACTS_KEY, HOST_METRICS_EVERY, HOST_METRICS_KEY


def _metrics(temperature: float, uptime: float, *, throttled: int = 0) -> HostMetricsState:
    return HostMetricsState(valid_metrics([("soc_temperature", temperature, "celsius"),
                                           ("uptime", uptime, "seconds"),
                                           ("throttled_now", throttled, "boolean", "firmware")]))


def _facts(address: str | None) -> HostFactsState:
    return HostFactsState(kernel_release="6.6.51+rpt-rpi-v8", interface="eth0", link_state="up",
                          address=address, base_tag="v2.1.0", reset_cause=None, wall_clock="synchronised",
                          boot=None)


class _Line:
    def __init__(self) -> None:
        self.clock = [100.0]
        self.puts: list[tuple[float, str, bytes]] = []
        self.line = HostLine(lambda key, value: self.puts.append((self.clock[0], key, value)),
                             monotonic=lambda: self.clock[0])

    def metrics_put(self) -> list[tuple[float, HostMetricsState]]:
        return [(at, decode_host_metrics_state(value)) for at, key, value in self.puts if key == HOST_METRICS_KEY]


def test_metrics_are_put_when_a_band_changes_never_for_a_gauge_inside_its_band():
    """Mutation probe (criterion 5): put the metrics on every sample -> 30 puts, fails."""
    line = _Line()
    for tick in range(30):                     # a minute of 2 s samples, temperature drifting, uptime rising
        line.line.metrics(_metrics(60 + tick / 10, 1000 + 2 * tick))
        line.clock[0] += 2
    assert [at for at, _ in line.metrics_put()] == [100.0]          # the first at once, then nothing
    line.line.metrics(_metrics(76, 2000))                            # crosses the notice band
    assert line.metrics_put()[-1][1] == _metrics(76, 2000)
    line.line.metrics(_metrics(77, 2002))                            # inside it again: not put
    assert len(line.metrics_put()) == 2


def test_a_change_inside_the_interval_waits_for_it_and_is_then_put_once():
    line = _Line()
    line.line.metrics(_metrics(50, 1))
    line.clock[0] += 2
    line.line.metrics(_metrics(50, 3, throttled=1))                  # a flag set 2 s after a put
    assert len(line.metrics_put()) == 1
    line.clock[0] += HOST_METRICS_EVERY - 2.5
    line.line.metrics(_metrics(50, 4, throttled=1))
    assert len(line.metrics_put()) == 1
    line.clock[0] += 0.5
    line.line.metrics(_metrics(50, 5, throttled=1))
    line.line.metrics(_metrics(50, 6, throttled=1))
    puts = line.metrics_put()
    assert [at for at, _ in puts] == [100.0, 100.0 + HOST_METRICS_EVERY]
    assert puts[-1][1].metrics[2].value == 1


def test_facts_are_put_when_they_change():
    line = _Line()
    line.line.facts(_facts("192.168.1.40"))
    line.line.facts(_facts("192.168.1.40"))
    line.line.facts(_facts("192.168.1.41"))
    stated = [decode_host_facts_state(value) for _, key, value in line.puts if key == HOST_FACTS_KEY]
    assert stated == [_facts("192.168.1.40"), _facts("192.168.1.41")]


@pytest.fixture
def requests(monkeypatch):
    """A recording HTTP transport under the host: every HTTP request and every TCP connection the
    host's code opens is recorded, and none leaves this process."""
    made: list[tuple] = []

    def request(self, method, url, *args, **kwargs):
        made.append((method, url))
        raise OSError("recorded")

    def connect(address, *args, **kwargs):
        made.append(("connect", address))
        raise OSError("recorded")

    monkeypatch.setattr(http.client.HTTPConnection, "request", request)
    monkeypatch.setattr(socket, "create_connection", connect)
    return made


def test_the_runner_puts_on_its_line_and_posts_nothing(monkeypatch, requests):
    """HostRunner's tick puts its metrics, and its facts once per interval, and the host makes no
    HTTP request: the Pi's host speaks to Central only over the bus. Mutation probe: restore the
    old observation post in `tick` -> a request is recorded."""
    clock = [100.0]
    puts: list[tuple[float, str]] = []
    runner = HostRunner(HostLine(lambda key, value: puts.append((clock[0], key)), monotonic=lambda: clock[0]),
                        base_tag="v2.1.0", monotonic=lambda: clock[0])
    runner.sampler = SimpleNamespace(
        sample=lambda: (("soc_temperature", 50.0, "celsius"), ("uptime", clock[0], "seconds")),
        throttling=lambda: (), supervision=lambda: (), memory_rows=lambda: (),
        facts=lambda: {"kernel_release": "6.6.51+rpt-rpi-v8", "interface": "eth0", "link_state": "up",
                       "address": "192.168.1.40", "wall_clock": "synchronised"},
        failed_units=lambda: ())
    monkeypatch.setattr("appliance.host.host_runner.reset_cause", lambda path: None)
    monkeypatch.setattr("appliance.host.host_runner.read_boot_report", lambda **kwargs: None)
    while clock[0] < 160:
        runner.tick()
        clock[0] += 2
    assert puts == [(100.0, HOST_METRICS_KEY), (100.0, HOST_FACTS_KEY)]
    assert requests == []
    assert not {"session", "transport", "recovery", "journal", "delivery", "core"} & set(vars(runner))
