"""One HDMI port's wanted power and what the Pi saw on it (roadmap 1b; run ledger
.claude/runs/display-1b.md, slice T1); stdlib and contracts only.

Two documents ride the Node bus's `display` store line, one key per Output (HDMI port):

- The **Output document** (Central -> Pi, the display line's desired bucket, key
  `output_document_key(output_id)`): the port's power timetable (Central's decision now, the set
  times it changes at over the next days, and 1b's console test on top), plus the power method and the two input switches of the Display last seen on it. It explains itself, so
  any writer (Central today, a local writer later) can produce it and the Pi needs nothing else.
- The **Output report** (Pi -> Central, the display line's state bucket, key
  `output_report_key(output_id)`): whether a display is connected, its identity and modes as its
  EDID says, which power methods answered a read-only check, the method in use, and the result of
  the last power attempt for the change number it names. The Pi reports; Central alone decides
  whether two identities are the same Display.

Each power attempt is also one event on `display.record.power` (`PowerAttempt`), so Central records
every attempt, not only the latest.

Roadmap 1c (run ledger .claude/runs/display-1c.md) adds a **picture part** to both: the document's
`picture` (brightness, contrast, colour temperature, gamma, and where brightness and contrast act)
with its own `picture_change`, which rises only when the picture part changes and never with
`change` (a picture edit never re-runs power); the report's `picture` (what the display answered,
each setting's result for the `picture_change` it names, and the newest picture change the Player
has presented). Both document fields are required: the display line's schema major is 2 from 1c on,
and Central records no report from a schema-1 writer (no backwards compatibility).

Rules this module owns (one home each):
- the wire shape and its JSON codec (canonical: sorted keys, no spaces, so a digest of the bytes
  is stable for an unchanged document);
- the vocabulary: `PowerMethod`, `Power`, `Darkness`, `InForceKind`, `PowerResult`, `BEST_DETECTED`;
- the set time's text form (`encode_set_time`, `decode_set_time`);
- the best-detected order of methods (`METHOD_PRECEDENCE`, setting #116);
- the picture vocabulary and ranges: `ActsOn`, `AS_THE_DISPLAY_HAS_IT`, `WriteResult`, percent
  0..100, colour temperature KELVIN_MIN..KELVIN_MAX in KELVIN_STEP, gamma in tenths
  GAMMA_TENTHS_MIN..GAMMA_TENTHS_MAX (settings #111-#114);
- the bus keys and their largest sizes (the display line's key tables are built from them in
  appliance/display_host/bus.py; Central's writer learns them from the bucket itself).

Rules it does NOT own: what is in force at the Pi's clock and when to act (the Pi's
appliance/display_host/power_timetable.py and output_power.py), the timetable itself (Central's
central/power/timetable.py), whether a display is the same Display and whether a Frame
is ready (Central's central/displays/); how a picture setting becomes a DDC/CI write or a picture
operation (appliance/display_host/picture.py); where a setting acts on a Display (Central's
central/displays/picture.py, set at the Display's first probe and kept).

Durations and calendar set times cross the wire, never a clock reading (the design's rule, from
the owner's "Yes, send 7 days of set times", 2026-10-12): a set time is a calendar time Central
worked out from the house's rules; the Pi compares it only with its
own clock; a test's `for_seconds` counts on the Pi's monotonic clock from when the Pi applies it,
and `remaining_seconds` is the Pi's own count.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Literal

from contracts.node_protocol import token
from contracts.wire_codec import (
    decode,
    encode,
    fields_of,
    list_of,
    optional,
    printable,
    require,
    whole,
)

SCHEMA_MAJOR: Final = 3                         # the display line's schema major (birth, envelope); 3 from 2a

# Every Output a Node can have: the Pi 5's two HDMI connectors, and the two virtual connectors the
# software end-to-end tier runs (player/output_discovery.py's `weston_ini` iterates these).
OUTPUT_IDS: Final[tuple[str, ...]] = ("HDMI-A-1", "HDMI-A-2", "Virtual-1", "Virtual-2")

OUTPUT_DOCUMENT_BYTES: Final = 4096             # an Output document's largest encoding
OUTPUT_REPORT_BYTES: Final = 8192               # an Output report's largest encoding
POWER_ATTEMPT_BYTES: Final = 1024               # a PowerAttempt event's largest encoding
MAX_DISPLAY_MODES: Final = 64                   # the most EDID modes a report carries
MAX_REQUEST_SECONDS: Final = 24 * 3600          # the longest `for_seconds`

PERCENT_MAX: Final = 100                        # brightness and contrast: whole percents 0..100
KELVIN_MIN: Final = 3000                        # colour temperature (#113), whole kelvin in KELVIN_STEP
KELVIN_MAX: Final = 9300
KELVIN_STEP: Final = 100
KELVIN_NEUTRAL: Final = 6500                    # no change
GAMMA_TENTHS_MIN: Final = 18                    # gamma (#114) in tenths: 18..26 is 1.8..2.6
GAMMA_TENTHS_MAX: Final = 26
GAMMA_TENTHS_NEUTRAL: Final = 22                # 2.2: no change

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


class PowerResult(StrEnum):
    """What one power attempt established. Never a claim about pixels (design-language P4)."""
    CONFIRMED = "confirmed"             # the display read back the wanted state
    SIGNAL_STOPPED = "signal-stopped"   # signal off: the Pi stopped (or restarted) its picture; nothing claimed about the panel
    DID_NOT_ANSWER = "did-not-answer"   # the method's channel gave no answer in time, or no read-back
    NOT_SUPPORTED = "not-supported"     # no method available for this display (none answered the check)
    ANOTHER_INPUT = "another-input"     # #118 kept it on: the display is showing another input


@dataclass(frozen=True)
class PowerRequest:
    """1b's console test, on top of the timetable. `request_id` is the writer's id for it (Central:
    the power_requests row id); `for_seconds` counts on the Pi from when it applies the test: a test
    always ends."""
    request_id: str
    power: Power
    for_seconds: int

    def __post_init__(self) -> None:
        """ValueError("power_request") unless request_id is a node token, power is a member and
        for_seconds is 1..MAX_REQUEST_SECONDS."""
        require(_is_token(self.request_id) and isinstance(self.power, Power)
                 and whole(self.for_seconds, 1, MAX_REQUEST_SECONDS), "power_request")


# Power and the week (roadmap 2a). The design's rule (decision 0021), from the owner's "Yes, send 7
# days of set times" (2026-10-12): the wire carries durations and calendar set times, never a clock
# reading. A set time is a calendar time Central worked out from the house's
# rules ("2026-10-11T22:00:00Z", whole seconds, UTC), never a reading of Central's clock; the Pi
# compares it with its own clock only.

MAX_POWER_CHANGES: Final = 32                   # the most set times one timetable carries (8 days of 2, with room)
SET_TIME_PATTERN: Final = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"


class Darkness(StrEnum):
    """How a display is dark on purpose (setting #83)."""
    DISPLAY_OFF = "display-off"         # the display's power method turns it off
    BLACK_PICTURE = "black-picture"     # the display stays on; Display Host shows black (slice S2)


class InForceKind(StrEnum):
    """What the Pi is carrying out on one Output (never a claim about pixels)."""
    TEST = "test"                               # 1b's console test, counted on the Pi's monotonic clock
    TIMETABLE = "timetable"                     # the power timetable at the Pi's own clock
    TIMETABLE_RAN_OUT = "timetable-ran-out"     # the Pi's clock is past `until`: the display is left as it is


@dataclass(frozen=True)
class PowerChange:
    """One set time of the power timetable: at `at` (a set time) the display's wanted power becomes
    `power`."""
    at: str
    power: Power

    def __post_init__(self) -> None:
        """ValueError("power_change") unless `at` decodes (`decode_set_time`) and power is a member."""
        require(_is_set_time(self.at) and isinstance(self.power, Power), "power_change")


@dataclass(frozen=True)
class OutputPower:
    """The Output document's power part. `now`: Central's decision, in force from when the Pi reads
    the document until the first change its own clock has reached. `changes`: the timetable,
    strictly increasing `at`, each before `until`. `until`: the end of the timetable's horizon;
    past it the Pi leaves the display as it is (InForceKind.TIMETABLE_RAN_OUT). `test`: 1b's
    console test, on top of the timetable while the Pi's own count runs (slice T drops its
    `reason`)."""
    now: Power
    changes: tuple[PowerChange, ...]
    until: str
    darkness: Darkness
    test: PowerRequest | None

    def __post_init__(self) -> None:
        """ValueError("output_power") unless now and darkness are members, changes holds at most
        MAX_POWER_CHANGES PowerChanges with strictly increasing `at`, every `at` < `until`, `until`
        decodes, and test is None or a PowerRequest."""
        changes = self.changes
        require(isinstance(self.now, Power) and isinstance(self.darkness, Darkness)
                 and isinstance(changes, tuple) and len(changes) <= MAX_POWER_CHANGES
                 and all(isinstance(change, PowerChange) for change in changes)
                 and _is_set_time(self.until)
                 and (self.test is None or isinstance(self.test, PowerRequest)), "output_power")
        moments = [decode_set_time(change.at) for change in changes] + [decode_set_time(self.until)]
        require(all(left < right for left, right in zip(moments, moments[1:], strict=False)), "output_power")


_SET_TIME: Final = re.compile(SET_TIME_PATTERN)
_SET_TIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


def encode_set_time(seconds: int) -> str:
    """UTC seconds as a set time: 1791842400 -> "2026-10-12T22:00:00Z". ValueError("set_time") for a
    negative or non-int value."""
    require(whole(seconds, 0), "set_time")
    try:
        return datetime.fromtimestamp(seconds, UTC).strftime(_SET_TIME_FORMAT)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("set_time") from error


def decode_set_time(text: str) -> int:
    """A set time as UTC seconds; ValueError("set_time") for anything not SET_TIME_PATTERN or not a
    real calendar moment. Stdlib only (the Pi runs it)."""
    require(isinstance(text, str) and _SET_TIME.fullmatch(text) is not None, "set_time")
    try:
        return int(datetime.strptime(text, _SET_TIME_FORMAT).replace(tzinfo=UTC).timestamp())
    except ValueError as error:
        raise ValueError("set_time") from error


class ActsOn(StrEnum):
    """Where one picture setting acts (1c design rule 1: exactly one place, recorded on the Display)."""
    DISPLAY = "display"                         # the display's own setting, over DDC/CI (VCP 0x10, 0x12)
    PICTURE_ADJUSTMENT = "picture-adjustment"   # Photo Wall changes the picture it sends; never a panel value


AS_THE_DISPLAY_HAS_IT: Final = "as-the-display-has-it"   # nobody set it: nothing written, nothing adjusted
PictureValue = int | Literal["as-the-display-has-it"]   # a whole percent 0..PERCENT_MAX, or unset


class WriteResult(StrEnum):
    """One DDC/CI write's result, judged by its read-back (never by the tool's exit status)."""
    TAKEN = "taken"                 # the read-back equals the value written
    NOT_TAKEN = "not-taken"         # the display answered the read-back with another value
    NO_ANSWER = "no-answer"         # no read-back in time


@dataclass(frozen=True)
class PictureSetting:
    """Brightness (#111) or contrast (#112) on one Display: where it acts and its value."""
    acts_on: ActsOn
    value: PictureValue

    def __post_init__(self) -> None:
        """ValueError("picture_setting") unless acts_on is a member and value is an int (never a
        bool) in 0..PERCENT_MAX or exactly AS_THE_DISPLAY_HAS_IT."""
        require(isinstance(self.acts_on, ActsOn) and _is_picture_value(self.value), "picture_setting")


@dataclass(frozen=True)
class OutputPicture:
    """The document's picture part: the Display's four settings. Colour temperature and gamma act
    only as picture adjustment in 1c; their neutral values (KELVIN_NEUTRAL, GAMMA_TENTHS_NEUTRAL)
    change nothing."""
    brightness: PictureSetting
    contrast: PictureSetting
    colour_temperature_kelvin: int
    gamma_tenths: int

    def __post_init__(self) -> None:
        """ValueError("output_picture") unless brightness and contrast are PictureSettings,
        KELVIN_MIN <= colour_temperature_kelvin <= KELVIN_MAX in KELVIN_STEP, and
        GAMMA_TENTHS_MIN <= gamma_tenths <= GAMMA_TENTHS_MAX (ints, never bools)."""
        require(isinstance(self.brightness, PictureSetting) and isinstance(self.contrast, PictureSetting)
                 and whole(self.colour_temperature_kelvin, KELVIN_MIN, KELVIN_MAX)
                 and (self.colour_temperature_kelvin - KELVIN_MIN) % KELVIN_STEP == 0
                 and whole(self.gamma_tenths, GAMMA_TENTHS_MIN, GAMMA_TENTHS_MAX), "output_picture")


@dataclass(frozen=True)
class SettingReport:
    """One setting as the Pi left it for the report's picture change. `acts_on` echoes the
    document. On the display: `read` is the percent the display last read back (None: no read
    yet, or no answer) and `result` the write's result for `PictureReport.for_change` (None:
    nothing was written for it, e.g. unset, or the display already read the value). As picture
    adjustment both are None: a software value is never reported as a panel value."""
    acts_on: ActsOn
    read: int | None
    result: WriteResult | None

    def __post_init__(self) -> None:
        """ValueError("setting_report") unless acts_on is a member; read is None or an int in
        0..PERCENT_MAX; result is None or a WriteResult; and both are None when acts_on is
        PICTURE_ADJUSTMENT."""
        require(isinstance(self.acts_on, ActsOn)
                 and (self.read is None or whole(self.read, 0, PERCENT_MAX))
                 and (self.result is None or isinstance(self.result, WriteResult))
                 and (self.acts_on is ActsOn.DISPLAY or (self.read is None and self.result is None)),
                 "setting_report")


@dataclass(frozen=True)
class PictureReport:
    """The report's picture part. `brightness_answered` and `contrast_answered`: whether the
    display answered a DDC/CI read of VCP 0x10 and 0x12 at its last appearance (Central places a
    setting on the display at the Display's first probe only when it answered). `for_change`: the
    document picture_change the two results belong to. `presented_change`: the newest
    picture_change the Player has presented (a change whose picture operations equal the ones
    last presented counts as presented); None before the first."""
    brightness_answered: bool
    contrast_answered: bool
    for_change: int
    brightness: SettingReport
    contrast: SettingReport
    presented_change: int | None

    def __post_init__(self) -> None:
        """ValueError("picture_report") unless the two answered flags are bools, for_change >= 1,
        brightness and contrast are SettingReports, and presented_change is None or >= 1."""
        require(type(self.brightness_answered) is bool and type(self.contrast_answered) is bool
                 and whole(self.for_change, 1)
                 and isinstance(self.brightness, SettingReport) and isinstance(self.contrast, SettingReport)
                 and (self.presented_change is None or whole(self.presented_change, 1)), "picture_report")


@dataclass(frozen=True)
class OutputDocument:
    """What is wanted on one HDMI port. `change` rises when the power part (power, method, switches)
    changes; the report echoes the change it acted on. `power` is the port's power timetable with
    the console test on top."""
    output_id: str
    change: int
    power: OutputPower
    method: MethodChoice
    switch_input_on_power_on: bool      # #117
    never_off_on_other_input: bool      # #118
    picture_change: int                 # rises only when `picture` changes; never with `change`
    picture: OutputPicture              # the Display's picture settings (#111-#114)

    def __post_init__(self) -> None:
        """ValueError("output_document") unless output_id is in OUTPUT_IDS, change >= 1, power is an
        OutputPower, method is a PowerMethod or BEST_DETECTED, picture_change >= 1 and picture an
        OutputPicture."""
        require(self.output_id in OUTPUT_IDS and whole(self.change, 1)
                 and isinstance(self.power, OutputPower)
                 and (isinstance(self.method, PowerMethod) or _is_best_detected(self.method))
                 and type(self.switch_input_on_power_on) is bool and type(self.never_off_on_other_input) is bool
                 and whole(self.picture_change, 1) and isinstance(self.picture, OutputPicture),
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
        require(isinstance(self.maker, str) and _MAKER.fullmatch(self.maker) is not None
                 and whole(self.product, 0, 65535) and printable(self.name, 0, 13)
                 and (self.serial is None or printable(self.serial, 1, 32)), "display_identity")


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
        require(whole(self.width, 1, 16384) and whole(self.height, 1, 16384)
                 and whole(self.refresh_millihertz, 1000, 1_000_000) and type(self.preferred) is bool, "display_mode")


@dataclass(frozen=True)
class InForce:
    """What the Pi is carrying out on one Output. TEST: the console test `request_id`, with the Pi's
    own count of what is left. TIMETABLE: the timetable's `power` at the Pi's clock, `since` the set
    time of the change in force (None while the document's `now` is). TIMETABLE_RAN_OUT: the Pi's
    clock is past `until`; nothing is wanted (`power` None)."""
    kind: InForceKind
    power: Power | None
    request_id: str | None
    remaining_seconds: int | None
    since: str | None

    def __post_init__(self) -> None:
        """ValueError("in_force") for any combination but the three above."""
        kind = self.kind
        test = kind is InForceKind.TEST
        require(isinstance(kind, InForceKind)
                 and (self.power is None if kind is InForceKind.TIMETABLE_RAN_OUT else isinstance(self.power, Power))
                 and (_is_token(self.request_id) if test else self.request_id is None)
                 and (whole(self.remaining_seconds, 0, MAX_REQUEST_SECONDS) if test
                      else self.remaining_seconds is None)
                 and (self.since is None or (kind is InForceKind.TIMETABLE and _is_set_time(self.since))),
                 "in_force")


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
    picture: PictureReport | None = None   # 1c: None = no picture document seen, or no display connected

    def __post_init__(self) -> None:
        """ValueError("output_report") unless output_id is in OUTPUT_IDS, modes has at most
        MAX_DISPLAY_MODES entries, answers are unique and in METHOD_PRECEDENCE order, method is in
        answers when set, and for_change >= 1 when set. `for_change` names power changes only.
        1c (slice T0): and picture is None or a PictureReport."""
        answers = self.answers
        require(self.output_id in OUTPUT_IDS and type(self.connected) is bool
                 and (self.identity is None or isinstance(self.identity, DisplayIdentity))
                 and isinstance(self.modes, tuple) and len(self.modes) <= MAX_DISPLAY_MODES
                 and all(isinstance(mode, DisplayMode) for mode in self.modes)
                 and isinstance(answers, tuple) and all(isinstance(method, PowerMethod) for method in answers)
                 and list(answers) == sorted(set(answers), key=METHOD_PRECEDENCE.index)
                 and (self.method is None or self.method in answers)
                 and (self.for_change is None or whole(self.for_change, 1))
                 and (self.result is None or isinstance(self.result, PowerResult))
                 and (self.in_force is None or isinstance(self.in_force, InForce))
                 and (self.picture is None or isinstance(self.picture, PictureReport)), "output_report")


@dataclass(frozen=True)
class PowerAttempt:
    """One power attempt, as a `display.record.power` event. `in_force` is what was carried out: a
    TEST or the TIMETABLE, never TIMETABLE_RAN_OUT (nothing is carried out then)."""
    output_id: str
    for_change: int
    in_force: InForce
    method: PowerMethod | None
    result: PowerResult
    elapsed_ms: int

    def __post_init__(self) -> None:
        """ValueError("power_attempt") unless output_id is in OUTPUT_IDS, for_change >= 1, in_force is
        an InForce of kind TEST or TIMETABLE, method (when set) and result are members, and
        elapsed_ms >= 0."""
        require(self.output_id in OUTPUT_IDS and whole(self.for_change, 1)
                 and isinstance(self.in_force, InForce) and self.in_force.kind is not InForceKind.TIMETABLE_RAN_OUT
                 and (self.method is None or isinstance(self.method, PowerMethod))
                 and isinstance(self.result, PowerResult) and whole(self.elapsed_ms, 0), "power_attempt")


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
    return encode(document, OUTPUT_DOCUMENT_BYTES, "output_document_too_large")


def decode_output_document(raw: bytes) -> OutputDocument:
    """The document `raw` encodes (contracts.strict_json rules: no duplicate keys, no NaN, no
    unknown field); ValueError("output_document") for anything else. 1c (slice T0): builds the
    nested picture part (`acts_on` an ActsOn, `value` an int or AS_THE_DISPLAY_HAS_IT)."""
    def build(value: dict[str, Any]) -> OutputDocument:
        method, picture = value["method"], value["picture"]
        return OutputDocument(**{
            **value, "power": _output_power(value["power"]),
            "method": BEST_DETECTED if _is_best_detected(method) else PowerMethod(method),
            "picture": _output_picture(picture)})
    return decode(raw, OUTPUT_DOCUMENT_BYTES, OutputDocument, build, "output_document")


def encode_output_report(report: OutputReport) -> bytes:
    """Canonical JSON of at most OUTPUT_REPORT_BYTES; ValueError("output_report_too_large")."""
    return encode(report, OUTPUT_REPORT_BYTES, "output_report_too_large")


def decode_output_report(raw: bytes) -> OutputReport:
    """ValueError("output_report") for anything but a valid encoding. 1c (slice T0): builds the
    nested picture part."""
    def build(value: dict[str, Any]) -> OutputReport:
        identity, in_force, picture = value["identity"], value["in_force"], value["picture"]
        return OutputReport(**{
            **value,
            "identity": None if identity is None else DisplayIdentity(**fields_of(DisplayIdentity, identity)),
            "modes": tuple(DisplayMode(**fields_of(DisplayMode, mode)) for mode in list_of(value["modes"])),
            "answers": tuple(PowerMethod(method) for method in list_of(value["answers"])),
            "method": optional(PowerMethod, value["method"]),
            "result": optional(PowerResult, value["result"]),
            "in_force": None if in_force is None else _in_force(in_force),
            "picture": None if picture is None else _picture_report(picture)})
    return decode(raw, OUTPUT_REPORT_BYTES, OutputReport, build, "output_report")


def encode_power_attempt(attempt: PowerAttempt) -> bytes:
    """Canonical JSON of at most POWER_ATTEMPT_BYTES; ValueError("power_attempt_too_large")."""
    return encode(attempt, POWER_ATTEMPT_BYTES, "power_attempt_too_large")


def decode_power_attempt(raw: bytes) -> PowerAttempt:
    """ValueError("power_attempt") for anything but a valid encoding."""
    def build(value: dict[str, Any]) -> PowerAttempt:
        return PowerAttempt(**{**value, "in_force": _in_force(value["in_force"]),
                               "method": optional(PowerMethod, value["method"]),
                               "result": PowerResult(value["result"])})
    return decode(raw, POWER_ATTEMPT_BYTES, PowerAttempt, build, "power_attempt")


# The checks and the codec above share these: one home for the wire's rules.

_MAKER: Final = re.compile(r"[A-Z]{3}")


def _is_token(value: object) -> bool:
    """A node token (contracts.node_protocol.token)."""
    try:
        token(value)
    except ValueError:
        return False
    return True


def _is_picture_value(value: object) -> bool:
    """A whole percent 0..PERCENT_MAX (never a bool) or exactly AS_THE_DISPLAY_HAS_IT."""
    return whole(value, 0, PERCENT_MAX) or (type(value) is str and value == AS_THE_DISPLAY_HAS_IT)


def _is_best_detected(value: object) -> bool:
    return type(value) is str and value == BEST_DETECTED


def _is_set_time(value: object) -> bool:
    try:
        decode_set_time(value)  # type: ignore[arg-type]
    except ValueError:
        return False
    return True


def _power_request(value: object) -> PowerRequest:
    request = fields_of(PowerRequest, value)
    return PowerRequest(**{**request, "power": Power(request["power"])})


def _output_power(value: object) -> OutputPower:
    power = fields_of(OutputPower, value)
    test = power["test"]
    return OutputPower(**{
        **power, "now": Power(power["now"]), "darkness": Darkness(power["darkness"]),
        "changes": tuple(PowerChange(**{**(change := fields_of(PowerChange, item)), "power": Power(change["power"])})
                         for item in list_of(power["changes"])),
        "test": None if test is None else _power_request(test)})


def _in_force(value: object) -> InForce:
    in_force = fields_of(InForce, value)
    return InForce(**{**in_force, "kind": InForceKind(in_force["kind"]),
                      "power": optional(Power, in_force["power"])})


def _picture_setting(value: object) -> PictureSetting:
    setting = fields_of(PictureSetting, value)
    return PictureSetting(**{**setting, "acts_on": ActsOn(setting["acts_on"])})


def _output_picture(value: object) -> OutputPicture:
    picture = fields_of(OutputPicture, value)
    return OutputPicture(**{**picture, "brightness": _picture_setting(picture["brightness"]),
                            "contrast": _picture_setting(picture["contrast"])})


def _setting_report(value: object) -> SettingReport:
    report = fields_of(SettingReport, value)
    return SettingReport(**{**report, "acts_on": ActsOn(report["acts_on"]),
                            "result": optional(WriteResult, report["result"])})


def _picture_report(value: object) -> PictureReport:
    report = fields_of(PictureReport, value)
    return PictureReport(**{**report, "brightness": _setting_report(report["brightness"]),
                            "contrast": _setting_report(report["contrast"])})


__all__ = [
    "AS_THE_DISPLAY_HAS_IT", "BEST_DETECTED", "GAMMA_TENTHS_MAX", "GAMMA_TENTHS_MIN", "GAMMA_TENTHS_NEUTRAL",
    "KELVIN_MAX", "KELVIN_MIN", "KELVIN_NEUTRAL", "KELVIN_STEP",
    "MAX_DISPLAY_MODES", "MAX_REQUEST_SECONDS", "METHOD_PRECEDENCE",
    "OUTPUT_DOCUMENT_BYTES", "OUTPUT_IDS", "OUTPUT_REPORT_BYTES", "PERCENT_MAX", "POWER_ATTEMPT_BYTES",
    "MAX_POWER_CHANGES", "SCHEMA_MAJOR", "SET_TIME_PATTERN", "ActsOn", "Darkness", "DisplayIdentity", "DisplayMode",
    "InForce", "InForceKind", "MethodChoice", "OutputDocument", "OutputPicture", "OutputPower", "OutputReport",
    "PictureReport", "PictureSetting", "PictureValue", "Power", "PowerAttempt", "PowerChange", "PowerMethod",
    "PowerRequest", "PowerResult", "SettingReport", "WriteResult",
    "decode_output_document", "decode_output_report", "decode_power_attempt", "decode_set_time",
    "encode_output_document", "encode_output_report", "encode_power_attempt", "encode_set_time",
    "output_document_key", "output_report_key",
]
