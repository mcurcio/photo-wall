"""HostCore's wired light meter (decision 0028), through a fake Linux IIO tree: the reading reaches the
host line's state, a failing read reports None, no device puts no key, and only a change worth
reporting is put again."""
from __future__ import annotations

from appliance.host.bus import HOST_STATE
from appliance.host.light_sensor import (
    LightMeter,
    find_meter,
    read_lux,
    reading_of,
    worth_reporting,
)
from contracts.room_light import (
    LIGHT_SENSOR_BYTES,
    LIGHT_SENSOR_KEY,
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
    assert light.sample() == reading_of(125.0)
    [(key, value)] = puts
    assert key == LIGHT_SENSOR_KEY and decode_light_sensor(value) == reading_of(125.0)
    assert HOST_STATE.sizes[LIGHT_SENSOR_KEY] == LIGHT_SENSOR_BYTES


def test_an_input_channel_is_read_as_lux(tmp_path):
    path = device(tmp_path, "iio:device3", in_illuminance_input="42.5\n")
    assert find_meter(tmp_path) == path and read_lux(path) == 42.5


def test_a_failing_read_reports_none(tmp_path):
    path = device(tmp_path, "iio:device0", in_illuminance_input="not a number\n")
    light, puts = meter(tmp_path)
    assert light.sample() == reading_of(None)
    assert [decode_light_sensor(value) for _, value in puts] == [reading_of(None)]
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
    assert [decode_light_sensor(value).reading.lux for _, value in puts] == [100.0, 106.0]
    assert worth_reporting(reading_of(10.0), reading_of(10.9)) is False
    assert worth_reporting(reading_of(10.0), reading_of(11.1)) is True
    assert worth_reporting(reading_of(None), reading_of(None)) is False
    assert worth_reporting(reading_of(5.0), reading_of(None)) is True


def test_an_indexed_raw_channel_adds_its_offset_before_its_scale(tmp_path):
    """tsl2563 and isl29018 expose channel 0; IIO's processed value is (raw + offset) x scale.
    Mutation probes: drop the indexed names -> no meter; drop the offset -> 100.0."""
    path = device(tmp_path, "iio:device0", in_illuminance0_raw="200\n", in_illuminance0_offset="-40\n",
                  in_illuminance_scale="0.625\n")
    assert find_meter(tmp_path) == path and read_lux(path) == 100.0
    indexed = device(tmp_path, "iio:device1", in_illuminance0_input="12.5\n")
    assert read_lux(indexed) == 12.5


def test_a_meter_that_reported_and_went_away_reports_unavailable(tmp_path):
    """Central must not keep the last lux of an unplugged meter. Mutation probe: return None with no
    device whatever was read before -> nothing is put and the stale 80 lx stands."""
    path = device(tmp_path, "iio:device0", in_illuminance_input="80\n")
    light, puts = meter(tmp_path)
    light.sample()
    for child in path.iterdir():
        child.unlink()
    path.rmdir()
    assert light.sample() == reading_of(None)
    assert [decode_light_sensor(value) for _, value in puts] == [reading_of(80.0), reading_of(None)]
