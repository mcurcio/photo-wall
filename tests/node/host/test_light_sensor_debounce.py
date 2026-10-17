"""HostCore's light meter holds its last good reading through a few failed reads, and starts on its own
thread: through a fake Linux IIO tree and the put HostCore hands it (the host session's state.put)."""
from __future__ import annotations

import threading

from appliance.host.light_sensor import LightMeter, reading_of, start_light_meter
from contracts.room_light import LIGHT_SENSOR_KEY, decode_light_sensor


def meter_at(root, text: str):
    path = root / "iio:device0"
    path.mkdir(parents=True, exist_ok=True)
    (path / "in_illuminance_input").write_text(text)
    return path


def test_a_failed_read_keeps_the_last_good_reading_until_three_fail_in_a_row(tmp_path):
    """120 lx, two failed reads: nothing put; the third: unavailable; a good read: measured again.
    Mutation probe: UNAVAILABLE_AFTER_FAILED_READS = 1 -> the first failed read puts unavailable."""
    path = meter_at(tmp_path, "120\n")
    puts: list[tuple[str, bytes]] = []
    meter = LightMeter(lambda key, value: puts.append((key, value)), root=tmp_path)

    def put_readings():
        return [decode_light_sensor(value) for key, value in puts if key == LIGHT_SENSOR_KEY]

    assert meter.sample() == reading_of(120.0)
    (path / "in_illuminance_input").write_text("not a number\n")
    assert meter.sample() == reading_of(120.0)
    assert meter.sample() == reading_of(120.0)
    assert put_readings() == [reading_of(120.0)]
    assert meter.sample() == reading_of(None)
    assert put_readings() == [reading_of(120.0), reading_of(None)]
    (path / "in_illuminance_input").write_text("118\n")
    assert meter.sample() == reading_of(118.0)
    assert put_readings() == [reading_of(120.0), reading_of(None), reading_of(118.0)]


def test_a_good_read_between_failures_resets_the_count(tmp_path):
    path = meter_at(tmp_path, "50\n")
    puts: list[tuple[str, bytes]] = []
    meter = LightMeter(lambda key, value: puts.append((key, value)), root=tmp_path)
    meter.sample()
    for text in ("x\n", "x\n", "50\n", "x\n", "x\n"):
        (path / "in_illuminance_input").write_text(text)
        meter.sample()
    assert [decode_light_sensor(value) for _, value in puts] == [reading_of(50.0)]


def test_start_light_meter_samples_on_its_own_thread(tmp_path):
    """HostCore's composition: started, its first sample reaches the put at once. Mutation probe:
    build the meter without starting it -> nothing is put."""
    meter_at(tmp_path, "75\n")
    put = threading.Event()
    puts: list[tuple[str, bytes]] = []

    def record(key: str, value: bytes) -> None:
        puts.append((key, value))
        put.set()

    meter = start_light_meter(record, root=tmp_path)
    try:
        assert put.wait(5)
        assert [(key, decode_light_sensor(value)) for key, value in puts] == [(LIGHT_SENSOR_KEY, reading_of(75.0))]
    finally:
        meter.stop()
