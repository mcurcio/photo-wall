"""A light meter wired to the Pi (decision 0028; owner, 2026-10-15: "A light sensor connected to a
player"), read through Linux IIO and reported on the host line's state as LIGHT_SENSOR_KEY.

HostCore samples it every SAMPLE_SECONDS on its own thread (`LightMeter`), never waiting on the bus:
the first iio device (by name) that reads illuminance (`find_meter`), its lux (`read_lux`; None
when the read fails or is out of range), put as contracts/room_light.py `LightSensorReading` when
`worth_reporting`. A Pi with no meter writes no key; a meter that reported and then went away
reports unavailable. Central turns a reading into the state of the
room light the Sensor reports (central/room_light/); nothing here knows which.
"""
from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

from contracts.room_light import LIGHT_SENSOR_KEY, LUX_MAX, LightSensorReading, encode_light_sensor

log = logging.getLogger(__name__)

IIO_ROOT: Final = Path("/sys/bus/iio/devices")
SAMPLE_SECONDS: Final = 5.0
CHANGE_SHARE: Final = 0.05          # a reading is reported when it moves by more than this share...
CHANGE_LUX_LOW: Final = 1.0         # ...or, below LOW_LUX, by more than this many lux
LOW_LUX: Final = 20.0
# IIO's illuminance channel, unindexed or as channel 0 (tsl2563, isl29018): a processed `_input`,
# or `_raw` with an optional `_offset` and `_scale` (IIO: lux = (raw + offset) x scale), each of
# which may also be shared, without the index.
_CHANNELS: Final = ("in_illuminance", "in_illuminance0")


def _channel(device: Path) -> str | None:
    """The device's illuminance channel prefix, `_input` before `_raw`; None: it reads none."""
    for suffix in ("_input", "_raw"):
        for channel in _CHANNELS:
            if (device / (channel + suffix)).is_file():
                return channel + suffix
    return None


def find_meter(root: Path = IIO_ROOT) -> Path | None:
    """The first iio device (by name) with an illuminance channel (`_CHANNELS`, processed or raw);
    None: no meter (or no iio at all)."""
    try:
        devices = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None
    return next((device for device in devices if _channel(device) is not None), None)


def _number(path: Path) -> float:
    return float(path.read_text().strip())


def _attribute(device: Path, channel: str, name: str, default: float) -> float:
    """A raw channel's `_offset` or `_scale`: its own, else the shared one, else `default`."""
    for prefix in (channel, "in_illuminance"):
        if (device / f"{prefix}_{name}").is_file():
            return _number(device / f"{prefix}_{name}")
    return default


def read_lux(device: Path) -> float | None:
    """lux, or None when the read fails or is outside 0..LUX_MAX."""
    try:
        found = _channel(device)
        if found is None:
            return None
        if found.endswith("_input"):
            lux = _number(device / found)
        else:
            channel = found.removesuffix("_raw")
            lux = ((_number(device / found) + _attribute(device, channel, "offset", 0.0))
                   * _attribute(device, channel, "scale", 1.0))
    except (OSError, ValueError):
        return None
    return lux if math.isfinite(lux) and 0 <= lux <= LUX_MAX else None


def worth_reporting(last: LightSensorReading | None, now: LightSensorReading) -> bool:
    """The first reading; None <-> number; or a change of more than 5 % (1 lx below 20 lx)."""
    if last is None or (last.lux is None) != (now.lux is None):
        return True
    if last.lux is None or now.lux is None:
        return False
    step = CHANGE_LUX_LOW if last.lux < LOW_LUX else CHANGE_SHARE * last.lux
    return abs(now.lux - last.lux) > step


class LightMeter:
    """Samples the wired meter every SAMPLE_SECONDS on its own thread and puts each reading worth
    reporting on the host line's state (`put`: the host session's `state.put`, which never blocks)."""

    def __init__(self, put: Callable[[str, bytes], None], *, root: Path = IIO_ROOT) -> None:
        self._put, self._root = put, root
        self._last: LightSensorReading | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> LightSensorReading | None:
        """One sample: the reading, put when worth reporting. None, and no key put, while no meter
        has been seen this boot; a meter seen and then gone (unplugged, its driver gone) reads
        unavailable, so Central never keeps a stale reading as the light's."""
        device = find_meter(self._root)
        if device is None and self._last is None:
            return None
        reading = LightSensorReading(None if device is None else read_lux(device))
        if worth_reporting(self._last, reading):
            self._put(LIGHT_SENSOR_KEY, encode_light_sensor(reading))
            self._last = reading
        return reading

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="host-light-meter", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:
                log.exception("host: the light meter's sample failed")
            self._stop.wait(SAMPLE_SECONDS)


__all__ = ["IIO_ROOT", "SAMPLE_SECONDS", "LightMeter", "find_meter", "read_lux", "worth_reporting"]
