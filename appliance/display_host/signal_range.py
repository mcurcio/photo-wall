"""The Pi's HDMI signal range: which numbers the connector sends for black and white (display
tuning, decision 0024, design rule 3; Linux's connector property "Broadcast RGB", as `xrandr --set`
and KWin's rgbrange set it).

Weston holds the screen (DRM master) and never sends the property itself (Weston 14.0.2), so the
range is set before Weston starts: the display unit's privileged `ExecStartPre=` runs this module
(`main`, the display-signal-range launcher), which sets each range the controller last asked for.
The controller, unprivileged, reads the connector (`DrmConnectorRanges.read`, the card node open,
never master) and asks for a change by writing the request (`FileRangeRequests.request`); writing it
is the restart request: photo-wall-display-signal.path starts photo-wall-display-signal.service,
which restarts Weston, and this step sets the range before the new Weston takes the screen.

Rules this module owns (one home each):
- **What a read of the connector tells** (`RangeUnknown`): its range in force; NO_PROPERTY when
  the Pi has no such connector or the connector has no range property (a Virtual output, not
  HDMI); UNREADABLE when it could not be read (no libdrm, the card node or connector_id did not
  open, an ioctl failed, a value outside PROPERTY_VALUES), with the reason in the journal. Only a
  confirmed missing property is "no range setting".
- **The kernel is the truth** (`signal_outcome`): UNSUPPORTED when the connector has no range
  property; FAILED when it could not be read (never a restart: nothing is known to change);
  APPLIED when the connector holds the wanted range; FAILED
  when a restart was already asked for this range and the connector still differs, so a range the
  driver refuses is never asked again until the document's range changes or the Pi restarts (no
  restart loop); otherwise a restart is asked. Auto's outcome is never recomputed: Auto is the
  property's value.
- **The property's names** (PROPERTY_VALUES): vc4's enum names for each SignalRange.
- **The request's shape** (`encode_request`, `decode_request`): canonical JSON {output_id: value},
  at most REQUEST_BYTES, keys in OUTPUT_IDS. It lives in the controller's runtime directory
  (display_paths.SIGNAL_RANGE_REQUEST), never across a reboot: Central's document stays the one
  source, and a Display not on Auto costs one restart after a Pi start.
- **The root step shows only black** (`apply_requested`): it blanks Weston's tty (clear, cursor
  off) before anything else, sets only the ranges the connector does not already hold, and never
  raises; `main` exits 0 whatever happens, so Weston starts.

Rules it does NOT own: when the controller asks for a restart and how it orders it behind DDC/CI
and power (appliance/display_host/picture.py); the units (appliance/systemd/).
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import stat
import sys
from collections.abc import Mapping
from ctypes import POINTER, Structure, c_char, c_int, c_uint32, c_uint64
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

from appliance.kernel.display_paths import SIGNAL_RANGE_REQUEST
from contracts.node_output import OUTPUT_IDS
from contracts.node_picture import SignalRange, SignalResult
from contracts.strict_json import loads_object

from .power_methods import _connectors

PROPERTY: Final = "Broadcast RGB"
PROPERTY_VALUES: Final[Mapping[SignalRange, str]] = MappingProxyType({
    SignalRange.AUTO: "Automatic",
    SignalRange.FULL: "Full",
    SignalRange.LIMITED: "Limited 16:235",
})
RESTART_WAIT_SECONDS: Final = 30.0      # longest wait for the requested restart before answering FAILED
REQUEST_BYTES: Final = 512              # the request file's cap (root reads it)
TTY: Final = Path("/dev/tty1")          # Weston's tty (photo-wall-display.service TTYPath=)
_BLANK: Final = b"\033[2J\033[H\033[?25l"   # clear, cursor home, cursor off
_RANGES: Final = {name: signal_range for signal_range, name in PROPERTY_VALUES.items()}
_VALUES: Final = frozenset(str(item) for item in SignalRange)

log = logging.getLogger(__name__)


class RangeUnknown(Enum):
    """Why a read of the connector gave no range (the module's first rule)."""
    NO_PROPERTY = "no_property"     # no such connector, or it has no range property: no range setting
    UNREADABLE = "unreadable"       # it could not be read: a fault, logged


@dataclass(frozen=True)
class RangeRequest:
    """The ranges a Weston restart was asked for, per Output id (OUTPUT_IDS): the controller writes
    it, the root step reads it."""
    ranges: Mapping[str, SignalRange]


def signal_outcome(wanted: SignalRange, in_force: SignalRange | RangeUnknown,
                   requested: SignalRange | None) -> SignalResult | None:
    """The module's second rule. None: ask for a restart. Pure."""
    if in_force is RangeUnknown.NO_PROPERTY:
        return SignalResult.UNSUPPORTED
    if in_force is RangeUnknown.UNREADABLE:
        return SignalResult.FAILED
    if in_force == wanted:
        return SignalResult.APPLIED
    if requested == wanted:
        return SignalResult.FAILED
    return None


def encode_request(request: RangeRequest) -> bytes:
    """Canonical JSON {output_id: value}, keys sorted."""
    return json.dumps({output_id: str(value) for output_id, value in request.ranges.items()},
                      sort_keys=True, separators=(",", ":")).encode()


def decode_request(raw: bytes) -> RangeRequest:
    """Strict JSON (contracts.strict_json) of at most REQUEST_BYTES, keys in OUTPUT_IDS, values
    SignalRange values; ValueError("range_request") otherwise."""
    value = loads_object(raw, max_bytes=REQUEST_BYTES)
    if value is None or not all(key in OUTPUT_IDS and isinstance(item, str) and item in _VALUES
                                for key, item in value.items()):
        raise ValueError("range_request")
    return RangeRequest({key: SignalRange(item) for key, item in value.items()})


class ConnectorRanges(Protocol):
    def read(self, output_id: str) -> SignalRange | RangeUnknown: ...   # never raises


class ConnectorRangeSetter(ConnectorRanges, Protocol):
    def set(self, output_id: str, signal_range: SignalRange) -> bool: ...  # True when the read-back equals it


# -- libdrm over ctypes (xf86drmMode.h) -----------------------------------------------------------

_OBJECT_CONNECTOR: Final = 0xC0C0C0C0


class _Enum(Structure):
    _fields_ = [("value", c_uint64), ("name", c_char * 32)]


class _Property(Structure):
    _fields_ = [("prop_id", c_uint32), ("flags", c_uint32), ("name", c_char * 32),
                ("count_values", c_int), ("values", POINTER(c_uint64)),
                ("count_enums", c_int), ("enums", POINTER(_Enum)),
                ("count_blobs", c_int), ("blob_ids", POINTER(c_uint32))]


class _ObjectProperties(Structure):
    _fields_ = [("count_props", c_uint32), ("props", POINTER(c_uint32)), ("prop_values", POINTER(c_uint64))]


def _libdrm() -> ctypes.CDLL:
    drm = ctypes.CDLL("libdrm.so.2", use_errno=True)
    drm.drmModeObjectGetProperties.restype = POINTER(_ObjectProperties)
    drm.drmModeObjectGetProperties.argtypes = [c_int, c_uint32, c_uint32]
    drm.drmModeFreeObjectProperties.argtypes = [POINTER(_ObjectProperties)]
    drm.drmModeGetProperty.restype = POINTER(_Property)
    drm.drmModeGetProperty.argtypes = [c_int, c_uint32]
    drm.drmModeFreeProperty.argtypes = [POINTER(_Property)]
    drm.drmModeObjectSetProperty.restype = c_int
    drm.drmModeObjectSetProperty.argtypes = [c_int, c_uint32, c_uint32, c_uint32, c_uint64]
    return drm


@dataclass(frozen=True)
class _Found:
    prop_id: int
    current: int
    enums: Mapping[str, int]


class DrmConnectorRanges:
    """`ConnectorRangeSetter` over libdrm: `<drm_root>/card*-<output_id>/connector_id` names the
    card and the connector. `read` needs only the card node open (the controller, group video);
    `set` needs DRM master (the root step, with no Weston holding it). Never raises."""

    def __init__(self, drm_root: Path = Path("/sys/class/drm"), dev_root: Path = Path("/dev/dri")) -> None:
        self._drm_root, self._dev_root = drm_root, dev_root

    def read(self, output_id: str) -> SignalRange | RangeUnknown:
        def judge(drm: ctypes.CDLL, fd: int, connector: int, found: _Found) -> SignalRange | RangeUnknown:
            if (in_force := self._range(found)) is None:
                log.warning("signal range: %s's %s holds a value outside %s", output_id, PROPERTY,
                            sorted(PROPERTY_VALUES.values()))
                return RangeUnknown.UNREADABLE
            return in_force
        return self._with(output_id, judge)

    def set(self, output_id: str, signal_range: SignalRange) -> bool:
        def write(drm: ctypes.CDLL, fd: int, connector: int, found: _Found) -> bool:
            value = found.enums.get(PROPERTY_VALUES[signal_range])
            if value is None:
                return False
            if drm.drmModeObjectSetProperty(fd, connector, _OBJECT_CONNECTOR, found.prop_id, value) != 0:
                log.warning("signal range: %s refused %s: %s", output_id, signal_range,
                            os.strerror(ctypes.get_errno()))
            again = self._find(drm, fd, connector)        # the read-back judges the set
            return again is not None and self._range(again) is signal_range
        return self._with(output_id, write) is True

    @staticmethod
    def _range(found: _Found) -> SignalRange | None:
        name = next((name for name, value in found.enums.items() if value == found.current), None)
        return _RANGES.get(name) if name is not None else None

    def _with(self, output_id, act):
        """`act(drm, fd, connector_id, found)` on the connector's card; NO_PROPERTY when there is
        no such connector or it has no PROPERTY; UNREADABLE (logged) when the connector_id, libdrm
        or the card node does not open, or an ioctl fails."""
        answer = RangeUnknown.NO_PROPERTY
        for directory in _connectors(self._drm_root, output_id):
            try:
                connector = int((directory / "connector_id").read_text().strip())
                card = directory.name.split("-", 1)[0]
                drm = _libdrm()
                fd = os.open(self._dev_root / card, os.O_RDWR | os.O_CLOEXEC)
            except (OSError, ValueError):
                log.warning("signal range: %s's connector did not open", output_id, exc_info=True)
                answer = RangeUnknown.UNREADABLE
                continue
            try:
                found = self._find(drm, fd, connector)
                return RangeUnknown.NO_PROPERTY if found is None else act(drm, fd, connector, found)
            except (OSError, ValueError, ctypes.ArgumentError):
                log.warning("signal range: %s's connector did not read", output_id, exc_info=True)
                return RangeUnknown.UNREADABLE
            finally:
                os.close(fd)
        return answer

    @staticmethod
    def _find(drm: ctypes.CDLL, fd: int, connector: int) -> _Found | None:
        """The connector's PROPERTY: its id, current value and enum names."""
        properties = drm.drmModeObjectGetProperties(fd, connector, _OBJECT_CONNECTOR)
        if not properties:
            return None
        try:
            listing = properties.contents
            for index in range(listing.count_props):
                held = drm.drmModeGetProperty(fd, listing.props[index])
                if not held:
                    continue
                try:
                    item = held.contents
                    if item.name.decode(errors="replace") == PROPERTY:
                        enums = {item.enums[j].name.decode(errors="replace"): int(item.enums[j].value)
                                 for j in range(item.count_enums)}
                        return _Found(int(item.prop_id), int(listing.prop_values[index]), enums)
                finally:
                    drm.drmModeFreeProperty(held)
            return None
        finally:
            drm.drmModeFreeObjectProperties(properties)


class RangeRequests(Protocol):
    def last(self) -> RangeRequest: ...                 # empty after a Pi start

    def request(self, request: RangeRequest) -> None: ...   # writing it IS the restart request; never blocks


class FileRangeRequests:
    """`RangeRequests` as the file at `path` (display_paths.SIGNAL_RANGE_REQUEST), replaced
    atomically, so the path unit sees one complete request. A file that is missing or does not
    decode is the empty request; a failed write is logged."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def last(self) -> RangeRequest:
        try:
            return decode_request(_read_capped(self._path))
        except (OSError, ValueError):
            return RangeRequest({})

    def request(self, request: RangeRequest) -> None:
        partial = self._path.with_suffix(".partial")
        try:
            partial.write_bytes(encode_request(request))
            os.replace(partial, self._path)
        except OSError:
            log.warning("signal range: could not write the request", exc_info=True)


def _read_capped(path: Path) -> bytes:
    """The regular file at `path`, never through a symlink, never blocking on a FIFO; ValueError
    past REQUEST_BYTES."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("range_request")
        raw = os.read(fd, REQUEST_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > REQUEST_BYTES:
        raise ValueError("range_request")
    return raw


def apply_requested(request: RangeRequest, connectors: ConnectorRangeSetter, tty: Path) -> None:
    """The root step before Weston starts: blank `tty` (clear, cursor off) so a change shows black,
    never text; then set each requested range the connector does not already hold. Never raises."""
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            os.write(fd, _BLANK)
        finally:
            os.close(fd)
    except OSError:
        log.warning("signal range: %s did not blank", tty, exc_info=True)
    for output_id, wanted in sorted(request.ranges.items()):
        try:
            before = connectors.read(output_id)
            if before in (RangeUnknown.NO_PROPERTY, wanted):
                log.info("signal range: %s read %s, wants %s: nothing to set", output_id, before.value,
                         wanted.value)
                continue
            taken = connectors.set(output_id, wanted)
            log.info("signal range: %s read %s, set %s, read back %s", output_id, before.value,
                     wanted.value, connectors.read(output_id).value)
            if not taken:
                log.warning("signal range: %s did not take %s", output_id, wanted)
        except Exception:
            log.exception("signal range: %s failed", output_id)


def _main(path: Path, connectors: ConnectorRangeSetter, tty: Path) -> None:
    """`main` on its parts: read the request (a missing, symlinked, oversized or malformed one is
    the empty request), apply it. Never raises."""
    try:
        request = decode_request(_read_capped(path))
    except FileNotFoundError:
        request = RangeRequest({})
    except (OSError, ValueError):
        log.warning("signal range: the request at %s did not read", path, exc_info=True)
        request = RangeRequest({})
    apply_requested(request, connectors, tty)


def main() -> None:
    """The display-signal-range launcher's entry (photo-wall-display.service ExecStartPre=+): exit
    0 always, so Weston starts whatever happens."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    try:
        _main(SIGNAL_RANGE_REQUEST, DrmConnectorRanges(), TTY)
    except Exception:
        log.exception("signal range: the step failed")
    sys.exit(0)


__all__ = ["PROPERTY", "PROPERTY_VALUES", "REQUEST_BYTES", "RESTART_WAIT_SECONDS", "TTY", "ConnectorRangeSetter",
           "ConnectorRanges", "DrmConnectorRanges", "FileRangeRequests", "RangeRequest", "RangeRequests", "RangeUnknown",
           "apply_requested", "decode_request", "encode_request", "main", "signal_outcome"]

if __name__ == "__main__":
    main()
