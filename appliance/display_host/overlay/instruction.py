"""Display's published overlay language: what to draw on an Output's health layer, and what was shown.

PURE, stdlib only. The judge (appliance.health) sends an `OverlayInstruction` per Output; the overlay
client answers a `PresentedReport` once that serial is really presented. Both are bounded at
construction, so no out-of-bounds value can be encoded, and parsing refuses anything else
(unknown or duplicate keys, wrong types, over-long text) with one code per message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

PULSE_DEADLINE_MS = 3000        # D: a new serial is presented within this, or it is counted late
INSTRUCTION_STALE_MS = 15000    # V: no instruction for an Output within this -> unavailable card
UNAVAILABLE_LINES = ("Health status unavailable", "")
MAX_TEXT = 96                   # one card line, and an Output name


def _text(value: object, *, empty: bool) -> bool:
    return isinstance(value, str) and len(value) <= MAX_TEXT and (empty or bool(value))


def _serial(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True, slots=True)
class OverlayInstruction:
    output: str
    serial: int
    tint: bool
    lines: tuple[str, str]

    def __post_init__(self) -> None:
        if not (_text(self.output, empty=False) and _serial(self.serial)
                and isinstance(self.tint, bool) and isinstance(self.lines, tuple)
                and len(self.lines) == 2 and all(_text(line, empty=True) for line in self.lines)):
            raise ValueError("overlay_instruction")


@dataclass(frozen=True, slots=True)
class PresentedReport:
    output: str
    serial: int

    def __post_init__(self) -> None:
        if not (_text(self.output, empty=False) and _serial(self.serial)):
            raise ValueError("presented_report")


def _document(raw: bytes, keys: frozenset[str], code: str) -> dict:
    def unique(pairs: list[tuple[str, object]]) -> dict:
        document = dict(pairs)
        if len(document) != len(pairs):
            raise ValueError(code)
        return document

    def constant(_name: str) -> object:
        raise ValueError(code)   # NaN / Infinity are not JSON

    try:
        document = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError(code) from error
    if not isinstance(document, dict) or set(document) != keys:
        raise ValueError(code)
    return document


def encode_overlay_instruction(instruction: OverlayInstruction) -> bytes:
    return json.dumps({"output": instruction.output, "serial": instruction.serial,
                       "tint": instruction.tint, "lines": list(instruction.lines)},
                      separators=(",", ":"), sort_keys=True).encode()


def parse_overlay_instruction(raw: bytes) -> OverlayInstruction:
    document = _document(raw, frozenset({"output", "serial", "tint", "lines"}),
                         "overlay_instruction")
    lines = document["lines"]
    if not isinstance(lines, list):
        raise ValueError("overlay_instruction")
    return OverlayInstruction(document["output"], document["serial"], document["tint"],
                              tuple(lines))


def encode_presented_report(report: PresentedReport) -> bytes:
    return json.dumps({"output": report.output, "serial": report.serial},
                      separators=(",", ":"), sort_keys=True).encode()


def parse_presented_report(raw: bytes) -> PresentedReport:
    document = _document(raw, frozenset({"output", "serial"}), "presented_report")
    return PresentedReport(document["output"], document["serial"])
