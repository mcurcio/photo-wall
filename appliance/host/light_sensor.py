"""A light meter wired to the Pi (decision 0028; owner, 2026-10-15: "A light sensor connected to a
player"), read through Linux IIO and reported on the host line's state as LIGHT_SENSOR_KEY.

HostCore samples it every SAMPLE_SECONDS on its own thread (`LightMeter`), never waiting on the bus:
the first iio device (by name) that reads illuminance (`find_meter`), its lux (`read_lux`; None
when the read fails or is out of range), put as contracts/room_light.py `LightSensorReading` when
`worth_reporting`. A Pi with no meter writes no key. Central turns a reading into the state of the
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
_INPUT: Final = "in_illuminance_input"
_RAW: Final = "in_illuminance_raw"
_SCALE: Final = "in_illuminance_scale"


def find_meter(root: Path = IIO_ROOT) -> Path | None:
    """The first iio device (by name) with in_illuminance_input, or in_illuminance_raw (+
    in_illuminance_scale, default 1); None: no meter (or no iio at all)."""
    try:
        devices = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None
    return next((device for device in devices
                 if (device / _INPUT).is_file() or (device / _RAW).is_file()), None)


def _number(path: Path) -> float:
    return float(path.read_text().strip())


def read_lux(device: Path) -> float | None:
    """lux, or None when the read fails or is outside 0..LUX_MAX."""
    try:
        if (device / _INPUT).is_file():
            lux = _number(device / _INPUT)
        else:
            scale = _number(device / _SCALE) if (device / _SCALE).is_file() else 1.0
            lux = _number(device / _RAW) * scale
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
        """One sample: the reading (None: no meter, and no key put), put when worth reporting."""
        device = find_meter(self._root)
        if device is None:
            return None
        reading = LightSensorReading(read_lux(device))
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
