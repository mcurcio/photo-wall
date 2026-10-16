"""HostCore's wired light meter (decision 0028), through a fake Linux IIO tree: the reading reaches the
host line's state, a failing read reports None, no device puts no key, and only a change worth
reporting is put again."""
from __future__ import annotations

from appliance.host.bus import HOST_STATE
from appliance.host.light_sensor import LightMeter, find_meter, read_lux, worth_reporting
from contracts.room_light import (
    LIGHT_SENSOR_BYTES,
    LIGHT_SENSOR_KEY,
    LightSensorReading,
    decode_light_sensor,
)


def device(root, name, **files):
    path = root / name
    path.mkdir(parents=True)
    (path / "name").write_text("fake\n")
    for key, text in files.items():
        (path / key).write_text(text)
    return path


def meter(root):
    puts = []
    return LightMeter(lambda key, value: puts.append((key, value)), root=root), puts


def test_a_fake_iio_meter_reaches_the_host_lines_state(tmp_path):
    device(tmp_path, "iio:device0", in_voltage0_raw="12\n")                     # an ADC, not a meter
    device(tmp_path, "iio:device1", in_illuminance_raw="250\n", in_illuminance_scale="0.5\n")
    light, puts = meter(tmp_path)
    assert light.sample() == LightSensorReading(125.0)
    [(key, value)] = puts
    assert key == LIGHT_SENSOR_KEY and decode_light_sensor(value) == LightSensorReading(125.0)
    assert HOST_STATE.sizes[LIGHT_SENSOR_KEY] == LIGHT_SENSOR_BYTES


def test_an_input_channel_is_read_as_lux(tmp_path):
    path = device(tmp_path, "iio:device3", in_illuminance_input="42.5\n")
    assert find_meter(tmp_path) == path and read_lux(path) == 42.5


def test_a_failing_read_reports_none(tmp_path):
    path = device(tmp_path, "iio:device0", in_illuminance_input="not a number\n")
    light, puts = meter(tmp_path)
    assert light.sample() == LightSensorReading(None)
    assert [decode_light_sensor(value) for _, value in puts] == [LightSensorReading(None)]
    (path / "in_illuminance_input").write_text("-3\n")                           # out of range
    assert read_lux(path) is None


def test_no_device_puts_no_key(tmp_path):
    light, puts = meter(tmp_path / "absent")
    assert light.sample() is None and puts == []
    device(tmp_path, "iio:device0", in_voltage0_raw="1\n")
    light, puts = meter(tmp_path)
    assert light.sample() is None and puts == []


def test_only_a_change_worth_reporting_is_put_again(tmp_path):
    path = device(tmp_path, "iio:device0", in_illuminance_input="100\n")
    light, puts = meter(tmp_path)
    light.sample()
    (path / "in_illuminance_input").write_text("104\n")
    light.sample()
    (path / "in_illuminance_input").write_text("106\n")
    light.sample()
    assert [decode_light_sensor(value).lux for _, value in puts] == [100.0, 106.0]
    assert worth_reporting(LightSensorReading(10.0), LightSensorReading(10.9)) is False
    assert worth_reporting(LightSensorReading(10.0), LightSensorReading(11.1)) is True
    assert worth_reporting(LightSensorReading(None), LightSensorReading(None)) is False
    assert worth_reporting(LightSensorReading(5.0), LightSensorReading(None)) is True
