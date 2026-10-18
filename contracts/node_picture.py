"""One HDMI port's picture: the display's own settings and Photo Wall's, each on its own
(decision 0022; the picture split, slice S1). Stdlib and contracts only: the Pi runs it.

Two documents ride the Node bus's `display` store line beside the Output document and report
(contracts/node_output.py, which carries power alone from schema major 4), one key each way per
Output:

- The **picture document** (Central -> Pi, the display line's desired bucket, key
  `picture_document_key(output_id)`): `picture_change`, which rises whenever anything below it
  changes and never with the Output document's `change`; `hardware`, the display settings someone
  set in Photo Wall, by MCCS code (an absent code is "as the display has it": nothing is written);
  `software`, Photo Wall's group (contrast, colour temperature and gamma), always all present;
  `light`, the Display's light (`DisplayLight`: the matched lux and the panel's black, the fixed
  point the Player's physics dims from).
- The **picture report** (Pi -> Central, the state bucket, key `picture_report_key(output_id)`):
  the identity of the display `offered` describes; `offered`, the settings on the picture list the
  display answered when the Pi asked (None: not known yet; (): asked, it offers none); `results`,
  each hardware write the Pi made for `for_change`; `presented_change`, the newest picture change
  the Player presented. The software group is never reported as a value (U9): only
  `presented_change`.

Display tuning (decision 0024, schema major 6) adds a third group and one session-only field:
- the document's `signal_range` (`SignalRange`): which numbers the Pi's HDMI signal uses for black
  and white, the Display's, stored, kept by Done and put back by Revert like the other groups; the
  Pi changes the connector's range only when it differs from the connector's, reads it back and
  reports it (`SignalReport`, the report's `signal`);
- the document's `test_pattern` (`TestPatternAsk`): shown on every Frame of the Output instead of
  photos while the Display's picture session is live, never stored; the Pi takes it off
  TEST_PATTERN_SECONDS after it first received that ask (its own clock alone), so a Central
  outage cannot leave it up; the report's `test_pattern_shown` names the ask the Player presented.
  Schema major 7 (calibration targets, decisions 0026 and 0027): the ask may name either calibration
  target, the screen board drawn raw or through Photo Wall's steps.

Rules this module owns (one home each):
- the **picture list** (`PICTURE_LIST`): the only codes Photo Wall asks a display about, edits or
  sends, each with ddcutil's English name (the console shows that name; it never crosses the wire);
- the MCCS code's text form (`VCP_CODE_PATTERN`: two upper-case hex digits) and the value bounds
  (a level 0..LEVEL_MAX, a choice's value one byte);
- **which value a setting takes** (`HardwareRange.holds`): a level 0..its maximum, a choice the
  display lists, nothing on a read-only list; Central's edit check and Copy and the Pi's write all
  ask it;
- **a level as a percent** (`HardwareRange.percent`): a whole percent of the display's maximum,
  half up, which the base page and the console show first, the display's own count second;
- the software ranges (contrast SOFTWARE_PERCENT_MIN..SOFTWARE_PERCENT_MAX, 100 changes nothing;
  colour temperature and gamma) and the Display's light's (MATCHED_LUX_*, PANEL_BLACK_*), schema
  major 8;
- the wire shapes, their size caps (which the bounds below make every valid value fit, so no
  valid report is ever refused for size) and the two keys;
- the signal range's vocabulary (`SignalRange`, `SignalResult`), a test pattern's ask
  (`TestPatternAsk`; the patterns themselves are contracts/tuning.py's) and its bound
  (TEST_PATTERN_SECONDS).

- `WriteResult`: one DDC/CI write judged by its read-back.

Rules it does NOT own: how the Pi asks and writes (appliance/display_host/picture_list.py,
picture.py, ddc.py); how it sets the connector's range (appliance/display_host/signal_range.py);
what is stored on a Display, the session, Done, Revert and Copy (Central's
central/displays/picture.py and hardware.py); how the Player draws a test pattern
(player/geometry.py `pattern_signal`).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from contracts.kelvin import KELVIN_WHITE
from contracts.node_output import OUTPUT_IDS, DisplayIdentity
from contracts.tuning import TestPattern
from contracts.wire_codec import decode, encode, fields_of, list_of, object_of, require, whole

VcpCode = str                                   # VCP_CODE_PATTERN, and on PICTURE_LIST wherever it is used
VCP_CODE_PATTERN: Final = r"[0-9A-F]{2}"

LEVEL_MAX: Final = 65535                        # an MCCS continuous value is 16 bits
CHOICE_VALUE_MAX: Final = 255                   # an MCCS simple non-continuous value is one byte
MAX_CHOICES: Final = 16                         # a list setting naming more is offered read-only
CHOICE_NAME_MAX: Final = 24                     # printable ASCII; a longer name is cut to this

SOFTWARE_PERCENT_MIN: Final = 0                 # owner, 2026-10-12: "0–150%"
SOFTWARE_PERCENT_MAX: Final = 150
SOFTWARE_PERCENT_NEUTRAL: Final = 100           # changes nothing
KELVIN_MIN: Final = 3000                        # colour temperature (#113), whole kelvin in KELVIN_STEP
KELVIN_MAX: Final = 9300
KELVIN_STEP: Final = 100
KELVIN_NEUTRAL: Final = KELVIN_WHITE            # changes nothing: the Display's white (contracts/kelvin.py)
GAMMA_TENTHS_MIN: Final = 18                    # gamma (#114) in tenths: 18..26 is 1.8..2.6
GAMMA_TENTHS_MAX: Final = 26
GAMMA_TENTHS_NEUTRAL: Final = 22                # 2.2: changes nothing

# The Display's light: the TV as a fixed instrument at its tuned raw settings.
MATCHED_LUX_MIN: Final = 10.0                   # the paper match's lux at the Frame, MATCHED_LUX_MIN..MATCHED_LUX_MAX
MATCHED_LUX_MAX: Final = 20000.0
PANEL_BLACK_MIN: Final = 0.00001                # the panel's black as a share of its white (an OLED reads near 0)
PANEL_BLACK_MAX: Final = 0.05

TEST_PATTERN_SECONDS: Final = 600               # the Pi takes a test pattern off this long after it first
                                                # received its ask (design rule 1; a placeholder, bench-tuned)

PICTURE_DOCUMENT_BYTES: Final = 1024            # a picture document's largest encoding
PICTURE_REPORT_BYTES: Final = 32768             # a picture report's largest encoding (30 settings, 16 choices each)

# The picture list (decision 0022): ddcutil's colour feature group plus sharpness, less its
# actions (04, 05, 08, 1F) and read-only facts (0B). Names are ddcutil's (`ddcutil vcpinfo <code>`),
# checked on the image's ddcutil at the bench step. Not on it: power D6, input 60, volume 62,
# maker codes E0-FF, position and size on screen, menu lock, the self-description 02, 03, 52.
PICTURE_LIST: Final[Mapping[VcpCode, str]] = MappingProxyType({
    "0C": "Color temperature request",
    "10": "Brightness",
    "12": "Contrast",
    "14": "Select color preset",
    "16": "Video gain: Red",
    "18": "Video gain: Green",
    "1A": "Video gain: Blue",
    "59": "6 axis saturation: Red",
    "5A": "6 axis saturation: Yellow",
    "5B": "6 axis saturation: Green",
    "5C": "6 axis saturation: Cyan",
    "5D": "6 axis saturation: Blue",
    "5E": "6 axis saturation: Magenta",
    "6B": "Backlight level: White",
    "6C": "Video black level: Red",
    "6D": "Backlight level: Red",
    "6E": "Video black level: Green",
    "6F": "Backlight level: Green",
    "70": "Video black level: Blue",
    "71": "Backlight level: Blue",
    "87": "Sharpness",
    "8A": "Color saturation",
    "90": "Hue",
    "9B": "6 axis hue control: Red",
    "9C": "6 axis hue control: Yellow",
    "9D": "6 axis hue control: Green",
    "9E": "6 axis hue control: Cyan",
    "9F": "6 axis hue control: Blue",
    "A0": "6 axis hue control: Magenta",
    "DC": "Display mode",
})


class WriteResult(StrEnum):
    """One DDC/CI write's result, judged by its read-back (never by the tool's exit status)."""
    TAKEN = "taken"                 # the read-back equals the value written
    NOT_TAKEN = "not-taken"         # the display answered the read-back with another value
    NO_ANSWER = "no-answer"         # no read-back in time


class SignalRange(StrEnum):
    """Which numbers the Pi's HDMI signal uses for black and white (Linux's connector property
    "Broadcast RGB"; HDMI's RGB quantization range). The Display's; every Display starts at AUTO."""
    AUTO = "auto"           # the kernel's choice: Limited for TV modes (every 4K and 1080p mode)
    FULL = "full"           # black 0, white 255
    LIMITED = "limited"     # black 16, white 235


class SignalResult(StrEnum):
    """What the Pi made of a document's signal range, judged by reading the connector back."""
    APPLIED = "applied"             # the connector holds the document's range
    UNSUPPORTED = "unsupported"     # this output has no range setting (not HDMI); nothing was set
    FAILED = "failed"               # not set: the connector kept `SignalReport.in_force`


@dataclass(frozen=True)
class TestPatternAsk:
    """One ask to show `pattern`: `ask` is the asking edit's session sequence. A document carrying
    a (pattern, ask) pair other than the last one the Pi received restarts the Pi's bound."""
    __test__ = False        # a domain name, not a pytest class

    pattern: TestPattern
    ask: int

    def __post_init__(self) -> None:
        """ValueError("test_pattern") unless pattern is a TestPattern and ask an int >= 1."""
        require(isinstance(self.pattern, TestPattern) and whole(self.ask, 1), "test_pattern")


@dataclass(frozen=True)
class SignalReport:
    """The Pi's answer to the signal range of the picture change `for_change`. `in_force`: the
    connector's range read back after the attempt (for AUTO, the property's value, never the
    kernel's outcome recomputed; for UNSUPPORTED, AUTO: the output sends what the kernel picks)."""
    for_change: int
    in_force: SignalRange
    result: SignalResult

    def __post_init__(self) -> None:
        """ValueError("signal_report") unless for_change >= 1, in_force is a SignalRange and result a
        SignalResult."""
        require(whole(self.for_change, 1) and isinstance(self.in_force, SignalRange)
                and isinstance(self.result, SignalResult), "signal_report")


class RangeKind(StrEnum):
    """How a display counts one setting, as it answered the Pi (observed, never configured)."""
    LEVEL = "level"                         # 0..maximum, the display's own count (an MCCS continuous answer)
    CHOICE = "choice"                       # one of the values the display lists (simple non-continuous)
    READ_ONLY_CHOICE = "read-only-choice"   # answers a value but lists none (or more than MAX_CHOICES): shown, not editable


@dataclass(frozen=True)
class Choice:
    """One value a list setting offers: its byte and the display's name for it ("6500 K", "User 1")."""
    value: int
    name: str

    def __post_init__(self) -> None:
        """ValueError("choice") unless 0 <= value <= CHOICE_VALUE_MAX and name is 1..CHOICE_NAME_MAX
        printable ASCII characters other than `"` and `\\` (the Pi drops those from a display's name)."""
        require(whole(self.value, 0, CHOICE_VALUE_MAX) and _ascii(self.name, 1, CHOICE_NAME_MAX), "choice")


@dataclass(frozen=True)
class HardwareRange:
    """LEVEL: `maximum` 1..LEVEL_MAX, `choices` (). CHOICE: `maximum` None, `choices` 1..MAX_CHOICES
    with strictly increasing values. READ_ONLY_CHOICE: `maximum` None, `choices` ()."""
    kind: RangeKind
    maximum: int | None
    choices: tuple[Choice, ...]

    def __post_init__(self) -> None:
        """ValueError("hardware_range") for any combination but the three above."""
        kind, maximum, choices = self.kind, self.maximum, self.choices
        require(isinstance(kind, RangeKind) and isinstance(choices, tuple)
                and all(isinstance(choice, Choice) for choice in choices), "hardware_range")
        if kind is RangeKind.LEVEL:
            require(whole(maximum, 1, LEVEL_MAX) and not choices, "hardware_range")
        elif kind is RangeKind.CHOICE:
            values = [choice.value for choice in choices]
            require(maximum is None and 1 <= len(choices) <= MAX_CHOICES
                    and all(left < right for left, right in zip(values, values[1:], strict=False)), "hardware_range")
        else:
            require(maximum is None and not choices, "hardware_range")

    def holds(self, value: object) -> bool:
        """Whether `value` may be set on a setting with this range: a whole number 0..maximum for a
        LEVEL, a value its choices list for a CHOICE, never for a READ_ONLY_CHOICE. Pure."""
        if not (isinstance(value, int) and not isinstance(value, bool)):
            return False
        if self.kind is RangeKind.LEVEL:
            return 0 <= value <= (self.maximum or 0)
        return any(choice.value == value for choice in self.choices)

    def percent(self, value: int) -> int:
        """A LEVEL's `value` as a whole percent of its maximum, rounded half up (117 of 255 is 46):
        how the console and the base page show a display's own level. The console's
        central/console/src/levelWords.js `levelPercent` is the same rule (tests hold the two to
        each other). ValueError("level_value") unless this is a LEVEL that holds `value`. Pure."""
        require(self.kind is RangeKind.LEVEL and self.holds(value), "level_value")
        maximum = self.maximum or 1
        return (value * 200 + maximum) // (2 * maximum)


@dataclass(frozen=True)
class HardwareSetting:
    """One setting on the picture list a display answered: observed, never desired. `current` is the
    display's latest read of it (the ask, the read before a write, a write's read-back or the
    periodic re-read; None: no answer at that read); a level's is 0..maximum, a list's any byte (a
    display may sit at a value it does not list)."""
    code: VcpCode
    range: HardwareRange
    current: int | None

    def __post_init__(self) -> None:
        """ValueError("hardware_setting") unless code is on PICTURE_LIST, range is a HardwareRange,
        and current is None or in 0..maximum (LEVEL) or 0..CHOICE_VALUE_MAX (otherwise)."""
        require(isinstance(self.code, str) and self.code in PICTURE_LIST and isinstance(self.range, HardwareRange),
                "hardware_setting")
        top = self.range.maximum if self.range.kind is RangeKind.LEVEL else CHOICE_VALUE_MAX
        require(self.current is None or whole(self.current, 0, top or 0), "hardware_setting")


def _finite(value: object, minimum: float, maximum: float) -> bool:
    """A finite float or int (never a bool) in minimum..maximum."""
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


@dataclass(frozen=True)
class DisplayLight:
    """The Display's light (the TV set for the brightest room, its backlight never assumed
    controllable): `matched_lux`, the light at the Frame (lux) when the TV's full white
    matched a sheet of paper (the paper match, contracts/frame_lighting.py SHEET_WHITENESS); `black`,
    the panel's black as a share of its white (its glow; the calibration reader measures it). The one
    fixed point the Player's physics dims from."""
    matched_lux: float
    black: float

    def __post_init__(self) -> None:
        """ValueError("display_light") unless matched_lux is a finite number (an int is accepted,
        never a bool) in MATCHED_LUX_MIN..MATCHED_LUX_MAX and black one in PANEL_BLACK_MIN..PANEL_BLACK_MAX."""
        require(_finite(self.matched_lux, MATCHED_LUX_MIN, MATCHED_LUX_MAX)
                and _finite(self.black, PANEL_BLACK_MIN, PANEL_BLACK_MAX), "display_light")


@dataclass(frozen=True)
class SoftwarePicture:
    """Photo Wall's picture settings (the Display's software group), always all present: contrast,
    colour temperature and gamma. Each at its neutral value changes nothing (NEUTRAL_SOFTWARE)."""
    contrast_percent: int
    colour_temperature_kelvin: int
    gamma_tenths: int

    def __post_init__(self) -> None:
        """ValueError("software_picture") unless contrast_percent is an int in
        SOFTWARE_PERCENT_MIN..SOFTWARE_PERCENT_MAX, KELVIN_MIN <= kelvin <= KELVIN_MAX in KELVIN_STEP,
        and GAMMA_TENTHS_MIN <= gamma_tenths <= GAMMA_TENTHS_MAX (ints, never bools)."""
        require(whole(self.contrast_percent, SOFTWARE_PERCENT_MIN, SOFTWARE_PERCENT_MAX)
                and whole(self.colour_temperature_kelvin, KELVIN_MIN, KELVIN_MAX)
                and (self.colour_temperature_kelvin - KELVIN_MIN) % KELVIN_STEP == 0
                and whole(self.gamma_tenths, GAMMA_TENTHS_MIN, GAMMA_TENTHS_MAX), "software_picture")


@dataclass(frozen=True)
class PictureDocument:
    """What is wanted of one HDMI port's picture. `hardware`: only the codes someone set (each on
    PICTURE_LIST, a value 0..LEVEL_MAX); `software`: always complete; `signal_range`: the
    Display's; `test_pattern`: the live session's ask, None when none is up. Treat `hardware` as
    read-only.

    The display-tuning fields default to "no change" (AUTO, no pattern) in Python; the wire never
    defaults them (`decode_picture_document` needs every field)."""
    output_id: str
    picture_change: int
    hardware: Mapping[VcpCode, int]
    software: SoftwarePicture
    signal_range: SignalRange = SignalRange.AUTO
    test_pattern: TestPatternAsk | None = None
    light: DisplayLight = field(kw_only=True)      # always present, never defaulted

    def __post_init__(self) -> None:
        """ValueError("picture_document") unless output_id is in OUTPUT_IDS, picture_change >= 1,
        every hardware code is on PICTURE_LIST with an int value in 0..LEVEL_MAX, software is a
        SoftwarePicture, signal_range a SignalRange, test_pattern None or a TestPatternAsk and light a
        DisplayLight."""
        require(self.output_id in OUTPUT_IDS and whole(self.picture_change, 1)
                and isinstance(self.hardware, Mapping)
                and all(code in PICTURE_LIST and whole(value, 0, LEVEL_MAX) for code, value in self.hardware.items())
                and isinstance(self.software, SoftwarePicture)
                and isinstance(self.signal_range, SignalRange)
                and (self.test_pattern is None or isinstance(self.test_pattern, TestPatternAsk))
                and isinstance(self.light, DisplayLight), "picture_document")


@dataclass(frozen=True)
class HardwareResult:
    """One hardware write for the report's `for_change`. `result` is judged by the read-back, never
    the tool's exit status: TAKEN when `after` equals the value written. `before`: the display's
    value read just before this write (None: no answer); `after`: the read-back (None: no answer)."""
    result: WriteResult
    before: int | None
    after: int | None

    def __post_init__(self) -> None:
        """ValueError("hardware_result") unless result is a WriteResult, before and after are None or
        0..LEVEL_MAX, after is not None when result is TAKEN or NOT_TAKEN, and after is None when it
        is NO_ANSWER."""
        answered = self.result in (WriteResult.TAKEN, WriteResult.NOT_TAKEN)
        require(isinstance(self.result, WriteResult)
                and (self.before is None or whole(self.before, 0, LEVEL_MAX))
                and (self.after is None or whole(self.after, 0, LEVEL_MAX))
                and (self.after is not None) == answered, "hardware_result")


@dataclass(frozen=True)
class PictureReport:
    """What the Pi did with one HDMI port's picture. `identity`: the display `offered` describes
    (None: none connected or unreadable). `offered`: unique codes in PICTURE_LIST order (None: not
    known yet: the Pi never reports a partial ask as known). `for_change`: the picture change whose
    hardware writes the Pi finished (None: none yet); `results` holds a write per code it wrote for
    that change (a code already at its value, or not offered, has none), and is empty when
    for_change is None. `presented_change`: the newest picture change the Player presented (a
    change whose picture operations equal the ones last presented counts as presented).
    `signal`: the Pi's answer to the newest document's signal range (None: none carried out since
    this controller started). `test_pattern_shown`: the ask whose pattern the Player presented
    (None: no pattern on screen, or the Pi took it off at its bound).

    The display-tuning fields default to None in Python; the wire never defaults them."""
    output_id: str
    identity: DisplayIdentity | None
    offered: tuple[HardwareSetting, ...] | None
    for_change: int | None
    results: Mapping[VcpCode, HardwareResult]
    presented_change: int | None
    signal: SignalReport | None = None
    test_pattern_shown: TestPatternAsk | None = None

    def __post_init__(self) -> None:
        """ValueError("picture_report") unless output_id is in OUTPUT_IDS, identity is None or a
        DisplayIdentity, offered is None or HardwareSettings with unique codes in PICTURE_LIST order,
        for_change and presented_change are None or >= 1, results' codes are on PICTURE_LIST with
        HardwareResult values, results is empty when for_change is None, signal is None or a
        SignalReport and test_pattern_shown None or a TestPatternAsk."""
        offered = self.offered
        require(self.output_id in OUTPUT_IDS
                and (self.identity is None or isinstance(self.identity, DisplayIdentity))
                and (offered is None or (isinstance(offered, tuple)
                                         and all(isinstance(setting, HardwareSetting) for setting in offered)
                                         and _in_list_order([setting.code for setting in offered])))
                and (self.for_change is None or whole(self.for_change, 1))
                and (self.presented_change is None or whole(self.presented_change, 1))
                and isinstance(self.results, Mapping)
                and all(code in PICTURE_LIST and isinstance(result, HardwareResult)
                        for code, result in self.results.items())
                and (self.for_change is not None or not self.results)
                and (self.signal is None or isinstance(self.signal, SignalReport))
                and (self.test_pattern_shown is None or isinstance(self.test_pattern_shown, TestPatternAsk)),
                "picture_report")


def picture_document_key(output_id: str) -> str:
    """The desired-bucket key of `output_id`'s picture document: "picture-HDMI-A-1".
    ValueError("output_id") for an id outside OUTPUT_IDS."""
    return _key(output_id)


def picture_report_key(output_id: str) -> str:
    """The state-bucket key of `output_id`'s picture report: "picture-HDMI-A-1".
    ValueError("output_id")."""
    return _key(output_id)


def encode_picture_document(document: PictureDocument) -> bytes:
    """Canonical JSON (contracts.wire_codec) of at most PICTURE_DOCUMENT_BYTES;
    ValueError("picture_document_too_large") past it (never for a valid document)."""
    return encode(document, PICTURE_DOCUMENT_BYTES, "picture_document_too_large")


def decode_picture_document(raw: bytes) -> PictureDocument:
    """The document `raw` encodes; ValueError("picture_document") for anything else."""
    def build(value: dict[str, Any]) -> PictureDocument:
        return PictureDocument(**{**value, "hardware": dict(object_of(value["hardware"])),
                                  "software": SoftwarePicture(**fields_of(SoftwarePicture, value["software"])),
                                  "signal_range": SignalRange(value["signal_range"]),
                                  "test_pattern": _test_pattern_ask(value["test_pattern"]),
                                  "light": DisplayLight(**fields_of(DisplayLight, value["light"]))})

    return decode(raw, PICTURE_DOCUMENT_BYTES, PictureDocument, build, "picture_document")


def encode_picture_report(report: PictureReport) -> bytes:
    """Canonical JSON of at most PICTURE_REPORT_BYTES; ValueError("picture_report_too_large")
    (never for a valid report)."""
    return encode(report, PICTURE_REPORT_BYTES, "picture_report_too_large")


def decode_picture_report(raw: bytes) -> PictureReport:
    """The report `raw` encodes; ValueError("picture_report") for anything else."""
    def build(value: dict[str, Any]) -> PictureReport:
        identity, offered = value["identity"], value["offered"]
        return PictureReport(**{
            **value,
            "identity": None if identity is None else DisplayIdentity(**fields_of(DisplayIdentity, identity)),
            "offered": None if offered is None else tuple(_hardware_setting(item) for item in list_of(offered)),
            "results": {code: _hardware_result(result) for code, result in object_of(value["results"]).items()},
            "signal": _signal_report(value["signal"]),
            "test_pattern_shown": _test_pattern_ask(value["test_pattern_shown"])})

    return decode(raw, PICTURE_REPORT_BYTES, PictureReport, build, "picture_report")


NEUTRAL_SOFTWARE: Final = SoftwarePicture(contrast_percent=SOFTWARE_PERCENT_NEUTRAL,
                                          colour_temperature_kelvin=KELVIN_NEUTRAL,
                                          gamma_tenths=GAMMA_TENTHS_NEUTRAL)

# Every Display's light on upgrade, until the owner types the paper match and the reader measures the
# black (placeholders for the Samsung QN43Q8F: white about 290 nits at 1,000 lx, black 0.03 % of white).
UPGRADE_DISPLAY_LIGHT: Final = DisplayLight(matched_lux=1000.0, black=0.0003)

_KEY_PREFIX: Final = "picture-"                 # both buckets: picture-<output_id>
_LIST_ORDER: Final[Mapping[VcpCode, int]] = MappingProxyType({code: index for index, code in enumerate(PICTURE_LIST)})


def _key(output_id: str) -> str:
    require(output_id in OUTPUT_IDS, "output_id")
    return _KEY_PREFIX + output_id


def _ascii(value: object, shortest: int, longest: int) -> bool:
    """A str of shortest..longest printable ASCII characters other than `"` and `\\`, the two JSON
    escapes as two bytes: without them no valid report outgrows PICTURE_REPORT_BYTES."""
    return (isinstance(value, str) and shortest <= len(value) <= longest
            and all(" " <= character <= "~" and character not in '"\\' for character in value))


def _in_list_order(codes: list[object]) -> bool:
    """Unique codes on PICTURE_LIST, in its order."""
    if not all(isinstance(code, str) and code in _LIST_ORDER for code in codes):
        return False
    ranks = [_LIST_ORDER[code] for code in codes]  # type: ignore[index]
    return all(left < right for left, right in zip(ranks, ranks[1:], strict=False))


def _hardware_setting(value: object) -> HardwareSetting:
    setting = fields_of(HardwareSetting, value)
    hardware_range = fields_of(HardwareRange, setting["range"])
    return HardwareSetting(**{**setting, "range": HardwareRange(**{
        **hardware_range, "kind": RangeKind(hardware_range["kind"]),
        "choices": tuple(Choice(**fields_of(Choice, choice)) for choice in list_of(hardware_range["choices"]))})})


def _hardware_result(value: object) -> HardwareResult:
    result = fields_of(HardwareResult, value)
    return HardwareResult(**{**result, "result": WriteResult(result["result"])})


def _test_pattern_ask(value: object) -> TestPatternAsk | None:
    if value is None:
        return None
    ask = fields_of(TestPatternAsk, value)
    return TestPatternAsk(**{**ask, "pattern": TestPattern(ask["pattern"])})


def _signal_report(value: object) -> SignalReport | None:
    if value is None:
        return None
    signal = fields_of(SignalReport, value)
    return SignalReport(**{**signal, "in_force": SignalRange(signal["in_force"]),
                           "result": SignalResult(signal["result"])})


__all__ = [
    "MATCHED_LUX_MAX", "MATCHED_LUX_MIN", "PANEL_BLACK_MAX", "PANEL_BLACK_MIN", "UPGRADE_DISPLAY_LIGHT", "DisplayLight",
    "CHOICE_NAME_MAX", "CHOICE_VALUE_MAX", "GAMMA_TENTHS_MAX", "GAMMA_TENTHS_MIN", "GAMMA_TENTHS_NEUTRAL",
    "KELVIN_MAX", "KELVIN_MIN", "KELVIN_NEUTRAL", "KELVIN_STEP", "LEVEL_MAX", "MAX_CHOICES",
    "NEUTRAL_SOFTWARE", "PICTURE_DOCUMENT_BYTES", "PICTURE_LIST", "PICTURE_REPORT_BYTES", "SOFTWARE_PERCENT_MAX",
    "SOFTWARE_PERCENT_MIN", "SOFTWARE_PERCENT_NEUTRAL", "TEST_PATTERN_SECONDS", "VCP_CODE_PATTERN", "Choice",
    "HardwareRange", "HardwareResult", "HardwareSetting", "PictureDocument", "PictureReport", "RangeKind",
    "SignalRange", "SignalReport", "SignalResult", "SoftwarePicture", "TestPatternAsk",
    "VcpCode", "WriteResult", "decode_picture_document", "decode_picture_report", "encode_picture_document",
    "encode_picture_report", "picture_document_key", "picture_report_key",
]
