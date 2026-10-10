"""One HDMI port's wanted power and what the Pi saw on it (roadmap 1b; run ledger
.claude/runs/display-1b.md, slice T1); stdlib and contracts only.

Two documents ride the Node bus's `display` store line, one key per Output (HDMI port):

- The **Output document** (Central -> Pi, the display line's desired bucket, key
  `output_document_key(output_id)`): the stack of power requests for the port, top first, plus the
  power method and the two input switches of the Display last seen on it. It explains itself, so
  any writer (Central today, a local writer later) can produce it and the Pi needs nothing else.
- The **Output report** (Pi -> Central, the display line's state bucket, key
  `output_report_key(output_id)`): whether a display is connected, its identity and modes as its
  EDID says, which power methods answered a read-only check, the method in use, and the result of
  the last power attempt for the change number it names. The Pi reports; Central alone decides
  whether two identities are the same Display.

Each power attempt is also one event on `display.record.power` (`PowerAttempt`), so Central records
every attempt, not only the latest.

Rules this module owns (one home each):
- the wire shape and its JSON codec (canonical: sorted keys, no spaces, so a digest of the bytes
  is stable for an unchanged document);
- the vocabulary: `PowerMethod`, `Power`, `RequestReason`, `PowerResult`, `BEST_DETECTED`;
- the best-detected order of methods (`METHOD_PRECEDENCE`, setting #116);
- the bus keys and their largest sizes (the display line's key tables are built from them in
  appliance/display_host/bus.py; Central's writer learns them from the bucket itself).

Rules it does NOT own: which request is in force and when to act (the Pi's
appliance/display_host/output_power.py), whether a display is the same Display and whether a Frame
is ready (Central's central/displays/).

Durations, never timestamps, cross the wire: `for_seconds` counts on the Pi's monotonic clock from
when the Pi applies the request; `remaining_seconds` is the Pi's own count. No clock is compared
across machines.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from typing import Any, Final, Literal, TypeVar

from contracts.node_protocol import token
from contracts.strict_json import loads_object

SCHEMA_MAJOR: Final = 1                         # the display line's schema major (birth, envelope)

# Every Output a Node can have: the Pi 5's two HDMI connectors, and the two virtual connectors the
# software end-to-end tier runs (player/output_discovery.py CONFIGURED_OUTPUT_IDS names the same
# four; slice P1 points it here).
OUTPUT_IDS: Final[tuple[str, ...]] = ("HDMI-A-1", "HDMI-A-2", "Virtual-1", "Virtual-2")

OUTPUT_DOCUMENT_BYTES: Final = 4096             # an Output document's largest encoding
OUTPUT_REPORT_BYTES: Final = 8192               # an Output report's largest encoding
POWER_ATTEMPT_BYTES: Final = 1024               # a PowerAttempt event's largest encoding
MAX_POWER_REQUESTS: Final = 8                   # the deepest request stack a document carries
MAX_DISPLAY_MODES: Final = 64                   # the most EDID modes a report carries
MAX_REQUEST_SECONDS: Final = 24 * 3600          # the longest `for_seconds`

_DOCUMENT_PREFIX: Final = "output-"             # desired bucket key: output-<output_id>
_REPORT_PREFIX: Final = "output-"               # state bucket key: output-<output_id>


class PowerMethod(StrEnum):
    """How the Pi turns a display off and on (setting #116, smart plug excluded in 1b)."""
    HDMI_CEC = "hdmi-cec"       # the TV remote-control channel in the HDMI cable
    DDC_CI = "ddc-ci"           # the monitor settings channel in the HDMI cable
    SIGNAL_OFF = "signal-off"   # the Pi stops sending a picture; the panel may sleep


# Best detected (#116): the first method in this order that answered the read-only check.
METHOD_PRECEDENCE: Final[tuple[PowerMethod, ...]] = (
    PowerMethod.HDMI_CEC, PowerMethod.DDC_CI, PowerMethod.SIGNAL_OFF)

BEST_DETECTED: Final = "best-detected"
MethodChoice = PowerMethod | Literal["best-detected"]


class Power(StrEnum):
    ON = "on"
    OFF = "off"


class RequestReason(StrEnum):
    """Who asked, as a type (never display text). 2a adds schedule, all-off and home-assistant."""
    STANDING = "standing"           # the bottom of every stack in 1b: displays are on
    CONSOLE_TEST = "console-test"   # Test: turn off / turn on on the Frame's Power tab


class PowerResult(StrEnum):
    """What one power attempt established. Never a claim about pixels (design-language P4)."""
    CONFIRMED = "confirmed"             # the display read back the wanted state
    SIGNAL_STOPPED = "signal-stopped"   # signal off: the Pi stopped (or restarted) its picture; nothing claimed about the panel
    DID_NOT_ANSWER = "did-not-answer"   # the method's channel gave no answer in time, or no read-back
    NOT_SUPPORTED = "not-supported"     # no method available for this display (none answered the check)
    ANOTHER_INPUT = "another-input"     # #118 kept it on: the display is showing another input


@dataclass(frozen=True)
class PowerRequest:
    """One layer of an Output's power stack. `request_id` is the writer's id for it (Central: the
    power_requests row id; the standing request: "standing"); `for_seconds` None = until removed."""
    request_id: str
    power: Power
    reason: RequestReason
    for_seconds: int | None = None

    def __post_init__(self) -> None:
        """ValueError("power_request") unless request_id is a node token, power and reason are
        members, and for_seconds is None or 1..MAX_REQUEST_SECONDS."""
        _require(_is_token(self.request_id) and isinstance(self.power, Power)
                 and isinstance(self.reason, RequestReason)
                 and (self.for_seconds is None or _count(self.for_seconds, 1, MAX_REQUEST_SECONDS)), "power_request")


@dataclass(frozen=True)
class OutputDocument:
    """What is wanted on one HDMI port. `change` rises on every change of anything else in it; the
    report echoes the change it acted on. `power` is top first and never empty: its last request has
    no `for_seconds` (the stack always ends in something that does not end)."""
    output_id: str
    change: int
    power: tuple[PowerRequest, ...]
    method: MethodChoice
    switch_input_on_power_on: bool      # #117
    never_off_on_other_input: bool      # #118

    def __post_init__(self) -> None:
        """ValueError("output_document") unless output_id is in OUTPUT_IDS, change >= 1, power has
        1..MAX_POWER_REQUESTS requests with unique request_ids and an untimed last one, and method
        is a PowerMethod or BEST_DETECTED."""
        power = self.power
        _require(self.output_id in OUTPUT_IDS and _count(self.change, 1)
                 and isinstance(power, tuple) and 1 <= len(power) <= MAX_POWER_REQUESTS
                 and all(isinstance(request, PowerRequest) for request in power)
                 and len({request.request_id for request in power}) == len(power)
                 and power[-1].for_seconds is None
                 and (isinstance(self.method, PowerMethod) or _is_best_detected(self.method))
                 and type(self.switch_input_on_power_on) is bool and type(self.never_off_on_other_input) is bool,
                 "output_document")


@dataclass(frozen=True)
class DisplayIdentity:
    """A display's identity as its EDID states it, normalised on the Pi but never matched there.
    `maker` is the three-letter PNP id ("XYM"); `product` the 16-bit product code (5475); `name` the
    monitor-name descriptor ("MNN", "" when absent); `serial` the serial-text descriptor when present
    and non-blank, else the 32-bit serial number in decimal when non-zero, else None."""
    maker: str
    product: int
    name: str
    serial: str | None

    def __post_init__(self) -> None:
        """ValueError("display_identity") unless maker matches [A-Z]{3}, 0 <= product <= 65535,
        name is printable and <= 13 characters, serial is None or 1..32 printable characters."""
        _require(isinstance(self.maker, str) and _MAKER.fullmatch(self.maker) is not None
                 and _count(self.product, 0, 65535) and _printable(self.name, 0, 13)
                 and (self.serial is None or _printable(self.serial, 1, 32)), "display_identity")


@dataclass(frozen=True)
class DisplayMode:
    """One mode the EDID offers. Refresh in millihertz, so 59.94 Hz is exact (59940)."""
    width: int
    height: int
    refresh_millihertz: int
    preferred: bool = False

    def __post_init__(self) -> None:
        """ValueError("display_mode") unless 1 <= width, height <= 16384 and
        1000 <= refresh_millihertz <= 1_000_000."""
        _require(_count(self.width, 1, 16384) and _count(self.height, 1, 16384)
                 and _count(self.refresh_millihertz, 1000, 1_000_000) and type(self.preferred) is bool, "display_mode")


@dataclass(frozen=True)
class InForce:
    """The request the Pi is carrying out, and its own count of what is left (None = untimed)."""
    request_id: str
    remaining_seconds: int | None

    def __post_init__(self) -> None:
        """ValueError("in_force") unless request_id is a node token and remaining_seconds is None or
        0..MAX_REQUEST_SECONDS."""
        _require(_is_token(self.request_id) and (
            self.remaining_seconds is None or _count(self.remaining_seconds, 0, MAX_REQUEST_SECONDS)), "in_force")


@dataclass(frozen=True)
class OutputReport:
    """What the Pi saw on one HDMI port. `identity` None = unreadable now (Central keeps the Display
    it last recorded); `answers` = methods that answered the read-only check, in METHOD_PRECEDENCE
    order; `method` = the one the Pi uses (None while none answered); `for_change` = the document
    change these results belong to (None: no document seen since the controller started); `result`
    = the last attempt's result for that change (None: no attempt for it yet, e.g. the display was
    already in the wanted state at start and was left alone); `in_force` = the request the Pi is
    carrying out (None without a document)."""
    output_id: str
    connected: bool
    identity: DisplayIdentity | None
    modes: tuple[DisplayMode, ...]
    answers: tuple[PowerMethod, ...]
    method: PowerMethod | None
    for_change: int | None
    result: PowerResult | None
    in_force: InForce | None

    def __post_init__(self) -> None:
        """ValueError("output_report") unless output_id is in OUTPUT_IDS, modes has at most
        MAX_DISPLAY_MODES entries, answers are unique and in METHOD_PRECEDENCE order, method is in
        answers when set, and for_change >= 1 when set."""
        answers = self.answers
        _require(self.output_id in OUTPUT_IDS and type(self.connected) is bool
                 and (self.identity is None or isinstance(self.identity, DisplayIdentity))
                 and isinstance(self.modes, tuple) and len(self.modes) <= MAX_DISPLAY_MODES
                 and all(isinstance(mode, DisplayMode) for mode in self.modes)
                 and isinstance(answers, tuple) and all(isinstance(method, PowerMethod) for method in answers)
                 and list(answers) == sorted(set(answers), key=METHOD_PRECEDENCE.index)
                 and (self.method is None or self.method in answers)
                 and (self.for_change is None or _count(self.for_change, 1))
                 and (self.result is None or isinstance(self.result, PowerResult))
                 and (self.in_force is None or isinstance(self.in_force, InForce)), "output_report")


@dataclass(frozen=True)
class PowerAttempt:
    """One power attempt, as a `display.record.power` event."""
    output_id: str
    for_change: int
    request_id: str
    power: Power
    method: PowerMethod | None
    result: PowerResult
    elapsed_ms: int

    def __post_init__(self) -> None:
        """ValueError("power_attempt") unless output_id is in OUTPUT_IDS, for_change >= 1, request_id
        is a node token, power, method (when set) and result are members, and elapsed_ms >= 0."""
        _require(self.output_id in OUTPUT_IDS and _count(self.for_change, 1) and _is_token(self.request_id)
                 and isinstance(self.power, Power) and (self.method is None or isinstance(self.method, PowerMethod))
                 and isinstance(self.result, PowerResult) and _count(self.elapsed_ms, 0), "power_attempt")


def output_document_key(output_id: str) -> str:
    """The desired-bucket key of `output_id`'s document: "output-HDMI-A-1". ValueError("output_id")
    for an id outside OUTPUT_IDS."""
    if output_id not in OUTPUT_IDS:
        raise ValueError("output_id")
    return _DOCUMENT_PREFIX + output_id


def output_report_key(output_id: str) -> str:
    """The state-bucket key of `output_id`'s report: "output-HDMI-A-1". ValueError("output_id")."""
    if output_id not in OUTPUT_IDS:
        raise ValueError("output_id")
    return _REPORT_PREFIX + output_id


def encode_output_document(document: OutputDocument) -> bytes:
    """Canonical JSON (sorted keys, no spaces, snake_case field names, enums as their values) of at
    most OUTPUT_DOCUMENT_BYTES; ValueError("output_document_too_large") past it."""
    return _encode(document, OUTPUT_DOCUMENT_BYTES, "output_document_too_large")


def decode_output_document(raw: bytes) -> OutputDocument:
    """The document `raw` encodes (contracts.strict_json rules: no duplicate keys, no NaN, no
    unknown field); ValueError("output_document") for anything else."""
    def build(value: dict[str, Any]) -> OutputDocument:
        method = value["method"]
        return OutputDocument(**{
            **value, "power": tuple(_power_request(request) for request in _list(value["power"])),
            "method": BEST_DETECTED if _is_best_detected(method) else PowerMethod(method)})
    return _decode(raw, OUTPUT_DOCUMENT_BYTES, OutputDocument, build, "output_document")


def encode_output_report(report: OutputReport) -> bytes:
    """Canonical JSON of at most OUTPUT_REPORT_BYTES; ValueError("output_report_too_large")."""
    return _encode(report, OUTPUT_REPORT_BYTES, "output_report_too_large")


def decode_output_report(raw: bytes) -> OutputReport:
    """ValueError("output_report") for anything but a valid encoding."""
    def build(value: dict[str, Any]) -> OutputReport:
        identity, in_force = value["identity"], value["in_force"]
        return OutputReport(**{
            **value,
            "identity": None if identity is None else DisplayIdentity(**_fields(DisplayIdentity, identity)),
            "modes": tuple(DisplayMode(**_fields(DisplayMode, mode)) for mode in _list(value["modes"])),
            "answers": tuple(PowerMethod(method) for method in _list(value["answers"])),
            "method": _optional(PowerMethod, value["method"]),
            "result": _optional(PowerResult, value["result"]),
            "in_force": None if in_force is None else InForce(**_fields(InForce, in_force))})
    return _decode(raw, OUTPUT_REPORT_BYTES, OutputReport, build, "output_report")


def encode_power_attempt(attempt: PowerAttempt) -> bytes:
    """Canonical JSON of at most POWER_ATTEMPT_BYTES; ValueError("power_attempt_too_large")."""
    return _encode(attempt, POWER_ATTEMPT_BYTES, "power_attempt_too_large")


def decode_power_attempt(raw: bytes) -> PowerAttempt:
    """ValueError("power_attempt") for anything but a valid encoding."""
    def build(value: dict[str, Any]) -> PowerAttempt:
        return PowerAttempt(**{**value, "power": Power(value["power"]),
                               "method": _optional(PowerMethod, value["method"]),
                               "result": PowerResult(value["result"])})
    return _decode(raw, POWER_ATTEMPT_BYTES, PowerAttempt, build, "power_attempt")


# The checks and the codec above share these: one home for the wire's rules.

_MAKER: Final = re.compile(r"[A-Z]{3}")


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _count(value: object, minimum: int, maximum: int = 2**63 - 1) -> bool:
    """An int (never a bool) in minimum..maximum."""
    return type(value) is int and minimum <= value <= maximum


def _is_token(value: object) -> bool:
    """A node token (contracts.node_protocol.token)."""
    try:
        token(value)
    except ValueError:
        return False
    return True


def _is_best_detected(value: object) -> bool:
    return type(value) is str and value == BEST_DETECTED


def _printable(value: object, shortest: int, longest: int) -> bool:
    return isinstance(value, str) and shortest <= len(value) <= longest and value.isprintable()


def _plain(value: object) -> object:
    """A wire value as JSON types: a dataclass as an object of its field names, a tuple as a list,
    an enum as its value; None stays null."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _encode(value: object, limit: int, too_large: str) -> bytes:
    raw = json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode()
    if len(raw) > limit:
        raise ValueError(too_large)
    return raw


def _fields(cls: type, value: object) -> dict[str, Any]:
    """`value` as `cls`'s keyword arguments: an object holding exactly its field names."""
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
        raise ValueError(cls.__name__)
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("list")
    return value


def _optional(kind: type[StrEnum], value: object) -> Any:
    return None if value is None else kind(value)


def _power_request(value: object) -> PowerRequest:
    request = _fields(PowerRequest, value)
    return PowerRequest(**{**request, "power": Power(request["power"]), "reason": RequestReason(request["reason"])})


_Wire = TypeVar("_Wire")


def _decode(raw: bytes, limit: int, cls: type[_Wire], build: Callable[[dict[str, Any]], _Wire], code: str) -> _Wire:
    """The `cls` that `raw` encodes, built by `build` from its checked fields; ValueError(code) for
    anything else (too large, not strict JSON, a missing or unknown field, a wrong type or value)."""
    value = loads_object(raw, max_bytes=limit) if isinstance(raw, bytes) else None
    try:
        if value is None:
            raise ValueError(code)
        return build(_fields(cls, value))
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(code) from error


__all__ = [
    "BEST_DETECTED", "MAX_DISPLAY_MODES", "MAX_POWER_REQUESTS", "MAX_REQUEST_SECONDS", "METHOD_PRECEDENCE",
    "OUTPUT_DOCUMENT_BYTES", "OUTPUT_IDS", "OUTPUT_REPORT_BYTES", "POWER_ATTEMPT_BYTES", "SCHEMA_MAJOR",
    "DisplayIdentity", "DisplayMode", "InForce", "MethodChoice", "OutputDocument", "OutputReport", "Power",
    "PowerAttempt", "PowerMethod", "PowerRequest", "PowerResult", "RequestReason",
    "decode_output_document", "decode_output_report", "decode_power_attempt", "encode_output_document",
    "encode_output_report", "encode_power_attempt", "output_document_key", "output_report_key",
]
