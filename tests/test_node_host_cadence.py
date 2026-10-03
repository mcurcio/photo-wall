"""Host-health tracer, node and derivation side (console DDD §63, bead T1): the sampler's
`soc_temperature` (omitted when unreadable, never zero), Host Management's per-interval posting
on its own monotonic clock, and Central's cap and silence limit derived from the one contract
constant. No database."""
import json
import math
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from appliance.node.host_linux import LinuxHostSampler
from appliance.node.host_runner import HostRunner
from central.fleet.host_thresholds import HOST_SILENT_AFTER_SECONDS, thresholds_document
from central.fleet.node_sessions import OBSERVATION_DAILY_CAP
from contracts.node_observation import HOST_OBSERVATION_INTERVAL_SECONDS, parse_host_observation
from contracts.node_protocol import NodeProducerV2

ROOT = Path(__file__).parents[1]


def _proc(tmp_path):
    """A fake /proc; the caller's monkeypatch fixes the boot clock (CLOCK_BOOTTIME is Linux-only)."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    (proc / "loadavg").write_text("0.50 0.40 0.30 1/100 1234\n")
    return proc


def _rows(sampler):
    return {row[0]: row for row in sampler.sample()}


def test_a_thermal_zone_reading_yields_soc_temperature_in_celsius(tmp_path, monkeypatch):
    monkeypatch.setattr("appliance.node.host_linux.boottime_ms", lambda: 5000)
    sys_root = tmp_path / "sys"
    zone = sys_root / "class/thermal/thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("81234\n")
    sampled = LinuxHostSampler(_proc(tmp_path), tmp_path, sys_root).sample()
    assert [row for row in sampled if row[0] == "soc_temperature"] == [("soc_temperature", 81.2, "celsius")]


def test_no_readable_thermal_zone_sends_no_temperature_row(tmp_path, monkeypatch):
    monkeypatch.setattr("appliance.node.host_linux.boottime_ms", lambda: 5000)
    proc = _proc(tmp_path)
    assert "soc_temperature" not in _rows(LinuxHostSampler(proc, tmp_path, tmp_path / "no-sys"))
    zone = tmp_path / "sys/class/thermal/thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("not a number\n")
    assert "soc_temperature" not in _rows(LinuxHostSampler(proc, tmp_path, tmp_path / "sys"))


class _Store:
    failed = False

    def __init__(self):
        self.writes = 0

    def write(self, name, value):
        self.writes += name == "observation"


def _runner(monkeypatch, clock):
    """A HostRunner with fake session, transport and sampler; `clock` is its monotonic clock."""
    runner = HostRunner.__new__(HostRunner)
    runner.monotonic = lambda: clock[0]
    runner.recovery = SimpleNamespace(telemetry=lambda: ((), None))
    runner.store = _Store()
    runner.sampler = SimpleNamespace(sample=lambda: (("uptime", 1, "seconds"),), throttling=lambda: (),
                                     supervision=lambda: (), facts=lambda: dict.fromkeys(
                                         ("kernel_release", "interface", "link_state", "address")))
    runner.delivery = SimpleNamespace(flush=lambda *args, **kwargs: 0)
    sequence = iter(range(1, 10_000))
    runner.journal = SimpleNamespace(next_sequence=lambda: next(sequence))
    posts = []

    def request(method, path, body=None):
        if path == "/v2/node/observations":
            posts.append((clock[0], parse_host_observation(body)))
        return 404, b""

    def session_for(session_id):
        producer = NodeProducerV2("site", "device-" + "a" * 64, 1, uuid4(), "host_core", uuid4())
        runner.core = SimpleNamespace(producer=producer, session_id=session_id)
        runner.session = SimpleNamespace(claim=SimpleNamespace(session_id=session_id),
                                         ensure=lambda: object(), request=request)

    runner.establish_session = lambda: True
    monkeypatch.setattr("appliance.node.host_runner.boottime_ms", lambda: int(clock[0] * 1000))
    return runner, posts, session_for


def test_the_runner_posts_at_session_start_then_once_per_interval_while_sampling_every_2_s(monkeypatch):
    clock = [100.0]
    runner, posts, session_for = _runner(monkeypatch, clock)
    session_for(uuid4())
    ticks = 0
    while clock[0] < 160:
        runner.tick()
        ticks += 1
        clock[0] += 2
    assert runner.store.writes == ticks == 30  # every tick samples and journals
    interval = HOST_OBSERVATION_INTERVAL_SECONDS
    times = [at for at, _ in posts]
    assert times[0] == 100.0  # the first at once
    assert all(later - earlier >= interval for earlier, later in zip(times, times[1:]))
    assert all(later - earlier < interval + 2 for earlier, later in zip(times, times[1:]))
    assert len(times) == 4
    # A new session posts at once, even inside the interval.
    session_for(uuid4())
    runner.tick()
    assert posts[-1][0] == clock[0] and clock[0] - times[-1] < interval
    # Sequences rise only with posts (the shared journal counter is not spent on coalesced ticks);
    # the process's first host facts document (§64) takes 2, and its 404 turns facts off.
    assert [observation.sequence for _, observation in posts] == [1, 3, 4, 5, 6]


def test_the_cap_and_the_silence_limit_derive_from_the_one_constant():
    interval = HOST_OBSERVATION_INTERVAL_SECONDS
    assert OBSERVATION_DAILY_CAP == 2 * math.ceil(86400 / interval) == 11520
    assert HOST_SILENT_AFTER_SECONDS == 4 * interval == 60
    assert thresholds_document()["host_silent_after_seconds"] == HOST_SILENT_AFTER_SECONDS
    # Change the constant before anything derives from it: both move with it.
    probe = ("import json, contracts.node_observation as c\n"
             "c.HOST_OBSERVATION_INTERVAL_SECONDS = 7\n"
             "from central.fleet.node_sessions import OBSERVATION_DAILY_CAP\n"
             "from central.fleet.host_thresholds import thresholds_document\n"
             "print(json.dumps([OBSERVATION_DAILY_CAP, thresholds_document()['host_silent_after_seconds']]))\n")
    out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                         check=True, timeout=60).stdout
    assert json.loads(out) == [2 * math.ceil(86400 / 7), 28]


def test_the_thresholds_document_is_numbers_only():
    document = thresholds_document()
    assert set(document) == {"host_silent_after_seconds", "metrics"}
    for metric in document["metrics"]:
        assert set(metric) == {"name", "unit", "notice_at", "alarm_at"}
        # A band a metric does not have is null (a flag is an alarm now, a notice occurred).
        assert all(type(metric[key]) in (int, float, type(None)) for key in ("notice_at", "alarm_at"))
        assert any(type(metric[key]) in (int, float) for key in ("notice_at", "alarm_at"))
    document["metrics"].clear()
    assert thresholds_document()["metrics"], "the served table is a fresh copy"
