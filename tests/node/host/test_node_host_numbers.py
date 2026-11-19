"""Node numbers and the preparation's storage room (console DDD §63, §65, bead N1), node side: the
sampler's firmware throttle flags, `cpu_busy` and `link_speed` (each omitted when unreadable,
never zero, each name once), `preparation_room` as the one admission decision, and the throttle
flags on the host line. No database."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from appliance.host.host_linux import LinuxHostSampler
from appliance.kernel.capacity import (
    EMERGENCY_HEADROOM,
    GIB,
    StorageShort,
    admit_preparation,
    device_class,
    preparation_room,
)
from contracts.node_observation import (
    MAX_OBSERVATION_BYTES,
    HostMetricsState,
    HostMetricV2,
    encode_host_metrics_state,
    valid_metrics,
)

MIB = 1024**2
FLAGS = ("under_voltage", "frequency_capped", "throttled", "soft_temperature_limit")


def _proc(tmp_path, stat="cpu  100 0 100 800 0 0 0 0 0 0\n", route=None):
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    (proc / "loadavg").write_text("0.50 0.40 0.30 1/100 1234\n")
    (proc / "stat").write_text(stat + "cpu0 1 2 3 4 5 6 7 8 0 0\n")
    if route is not None:
        (proc / "net/route").write_text(
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n" + route)
    return proc


def _firmware(tmp_path, text):
    path = tmp_path / "sys/devices/platform/soc/soc:firmware/get_throttled"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return tmp_path / "sys"


@pytest.fixture(autouse=True)
def _boot_clock(monkeypatch):
    # CLOCK_BOOTTIME is Linux-only; the sampler's uptime reads it.
    monkeypatch.setattr("appliance.host.host_linux.boottime_ms", lambda: 5000)


def test_get_throttled_0x50005_sets_under_voltage_and_throttled_now_and_occurred(tmp_path):
    rows = LinuxHostSampler(_proc(tmp_path), tmp_path, _firmware(tmp_path, "0x50005\n")).throttling()
    assert all(unit == "boolean" and source == "firmware" for _, _, unit, source in rows)
    values = {name: value for name, value, _, _ in rows}
    expected = {f"{flag}_{when}": 0 for when in ("now", "occurred") for flag in FLAGS}
    expected.update(under_voltage_now=1, throttled_now=1, under_voltage_occurred=1, throttled_occurred=1)
    assert values == expected and len(rows) == 8
    # The kernel prints the attribute without a prefix; the same bits read the same.
    again = LinuxHostSampler(_proc(tmp_path), tmp_path, _firmware(tmp_path, "50005\n")).throttling()
    assert again == rows


@pytest.mark.parametrize("text", [None, "not hex\n", "-1\n", ""])
def test_unreadable_flags_send_none_of_the_eight(tmp_path, text):
    sys_root = tmp_path / "no-firmware" if text is None else _firmware(tmp_path, text)
    assert LinuxHostSampler(_proc(tmp_path), tmp_path, sys_root).throttling() == ()


def test_cpu_busy_is_absent_on_the_first_sample_and_correct_from_two_readings(tmp_path):
    proc = _proc(tmp_path, stat="cpu  100 0 100 700 100 0 0 0 50 0\n")
    sampler = LinuxHostSampler(proc, tmp_path, tmp_path / "sys")
    assert "cpu_busy" not in {row[0] for row in sampler.sample()}
    # +300 busy (user 150, system 50, irq 50, steal 50), +100 idle, +100 iowait: total +500,
    # idle (with iowait) +200, so 60 % busy. Guest time (already in user) is not counted twice.
    (proc / "stat").write_text("cpu  250 0 150 800 200 50 0 50 999 0\n")
    assert [row for row in sampler.sample() if row[0] == "cpu_busy"] == [("cpu_busy", 60.0, "percent")]
    # An unreadable reading sends none, and the next one starts again from its own baseline.
    (proc / "stat").write_text("garbage\n")
    assert "cpu_busy" not in {row[0] for row in sampler.sample()}


def test_link_speed_of_the_default_route_interface_and_none_when_negative(tmp_path):
    route = ("wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
             "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
             "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n")
    proc = _proc(tmp_path, route=route)
    for interface, speed in (("eth0", "1000\n"), ("wlan0", "300\n")):
        (tmp_path / "sys/class/net" / interface).mkdir(parents=True, exist_ok=True)
        (tmp_path / "sys/class/net" / interface / "speed").write_text(speed)
    sampler = LinuxHostSampler(proc, tmp_path, tmp_path / "sys")
    assert sampler.default_route_interface() == "eth0"  # the lowest metric wins
    assert [row for row in sampler.sample() if row[0] == "link_speed"] == [
        ("link_speed", 1000, "megabits_per_second")]
    (tmp_path / "sys/class/net/eth0/speed").write_text("-1\n")
    assert "link_speed" not in {row[0] for row in sampler.sample()}
    assert "link_speed" not in {row[0] for row in LinuxHostSampler(_proc(tmp_path / "none"),
                                                                   tmp_path, tmp_path / "sys").sample()}


def test_each_name_is_emitted_once_and_the_largest_sample_fits_the_contract(tmp_path, monkeypatch):
    route = "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    proc = _proc(tmp_path, route=route)
    sys_root = _firmware(tmp_path, "0xf000f\n")
    (sys_root / "class/thermal/thermal_zone0").mkdir(parents=True)
    (sys_root / "class/thermal/thermal_zone0/temp").write_text("81234\n")
    (sys_root / "class/net/eth0").mkdir(parents=True)
    (sys_root / "class/net/eth0/speed").write_text("1000\n")
    sampler = LinuxHostSampler(proc, tmp_path, sys_root)
    sampler.sample()
    (proc / "stat").write_text("cpu  200 0 200 900 0 0 0 0 0 0\n")
    host = sampler.sample() + sampler.throttling()
    names = [row[0] for row in host]
    assert len(names) == len(set(names)) == 16
    # The largest sample: every host row, the full base supervisor summary, every memory row
    # (memcg, CMA, four peaks, three OOM counts) (32), with the longest
    # names and values, plus the appended drop count, all kept by `valid_metrics`.
    monkeypatch.setattr("appliance.host.host_linux.read_supervisor_status", lambda *a, **k: {
        "running": True, "attempts": 3, "fault": None, "sampled_boottime_ms": 1})
    monkeypatch.setattr("appliance.host.host_linux.boot_id", lambda: uuid4())
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\nCmaTotal: 524288 kB\n"
                                  "CmaFree: 1024 kB\n")
    cgroup = sys_root / "fs/cgroup"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    for name in ("photowallhostcore", "photowallbase", "photowallpreparation", "photowallapp"):
        (cgroup / f"{name}.slice").mkdir()
        (cgroup / f"{name}.slice/memory.peak").write_text("123456789\n")
        (cgroup / f"{name}.slice/memory.events").write_text("low 0\nhigh 0\nmax 4\noom 2\noom_kill 2\n")
    rows = host + sampler.supervision() + sampler.memory_rows()
    assert len(rows) == 32
    metrics = valid_metrics((name, 1.0e15 + 0.123456789, unit, *source) for name, _, unit, *source in rows)
    assert len(metrics) == 33 and metrics[-1] == HostMetricV2("metrics_dropped", 0, "count", "host_core")
    raw = encode_host_metrics_state(HostMetricsState(metrics, fault_code="sampler_partial_" + "x" * 48))
    assert len(raw) <= MAX_OBSERVATION_BYTES and len(metrics) <= 64


def _old_admit(size, *, total, available, free, used):
    """The decision before N1 (capacity.py at batch 3), kept as the reference; the store cap is
    the device class's since the 4 GB tracer (T1), and an image is counted once since E2c."""
    incremental = size
    return not (used + incremental > device_class(total).store_bytes
                or incremental > min(free, available - EMERGENCY_HEADROOM))


@pytest.mark.parametrize("size", [128, 200 * MIB, 288 * MIB, 984207360, GIB, 2 * GIB])
@pytest.mark.parametrize("total,available", [(4045 * MIB, 600 * MIB), (4045 * MIB, 3 * GIB),
                                             (8 * GIB, 600 * MIB), (8 * GIB, 2 * GIB),
                                             (8 * GIB, 3 * GIB), (8 * GIB, 7 * GIB), (16 * GIB, 12 * GIB)])
@pytest.mark.parametrize("free,used", [(4 * GIB, 0), (4 * GIB - 1261 * MIB, 1261 * MIB),
                                       (GIB, 3 * GIB), (100 * MIB, 0), (8 * GIB, 5 * GIB), (4 * GIB, 350 * MIB)])
def test_admit_preparation_refuses_exactly_when_preparation_room_is_short(size, total, available, free, used):
    room = preparation_room(total=total, available=available, free=free, used=used)
    required = size
    admitted = _old_admit(size, total=total, available=available, free=free, used=used)
    assert admitted == (required <= room)
    if admitted:
        assert admit_preparation(size, total=total, available=available, free=free, used=used) == required
    else:
        with pytest.raises(StorageShort, match="node_storage_capacity") as refused:
            admit_preparation(size, total=total, available=available, free=free, used=used)
        assert (refused.value.required, refused.value.room) == (required, room)
        assert isinstance(refused.value, ValueError) and room >= 0


def test_host_management_puts_the_throttle_flags_on_its_line():
    from appliance.host.host_runner import HostRunner
    stated = []
    runner = HostRunner(SimpleNamespace(metrics=stated.append, facts=lambda state: None),
                        monotonic=lambda: 100.0)
    runner._line_facts_at = 100.0    # facts are not this test's
    runner.sampler = SimpleNamespace(sample=lambda: (), supervision=lambda: (), memory_rows=lambda: (),
                                     throttling=lambda: (("throttled_now", 1, "boolean", "firmware"),))
    runner.tick()
    [state] = stated
    assert HostMetricV2("throttled_now", 1, "boolean", "firmware") in state.metrics
