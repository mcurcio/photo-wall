"""A Display's hardware picture settings against what its display offers, pure (decision 0022;
the picture split, slices S2 and S4).

The display settings someone set in Photo Wall are stored on the Display by MCCS code
(central/displays/picture.py `DisplayPicture.hardware`); what the display offers is observed, read
from the Output's recorded picture report (`contracts.node_picture.PictureReport.offered`). This
module holds every rule that reads one against the other, one home each:

- **May this value be set** (`edit_refusal`): a code off the picture list is never set; a value is
  set only on a setting the display offers as a level (0..its maximum) or a choice (a value it
  lists); handing a setting back ("as the display has it", value None) is refused only off the list.
- **Copy** (`copied_hardware`, `level_share`; owner, 2026-10-12, Copy: "Only ones set in Photo
  Wall (Recommended)"): each value set on the source, where the target offers that code the same
  way: a choice only when the target lists the same value and name, a level at the same share of
  its range. Everything else is skipped and named, and left as it is on the target.
- **A known list is kept** (`kept_offered`): a report saying "not known yet" never replaces a
  known list for the same display.
- **The tab's rows** (`hardware_rows`): every offered setting and every set code, in picture-list
  order.

Slice S2 holds offered_for, edit_refusal, offered_setting and hardware_rows; S4 brings
copied_hardware, level_share and kept_offered.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    PICTURE_LIST,
    HardwareResult,
    HardwareSetting,
    PictureReport,
    RangeKind,
    VcpCode,
)

EditRefusal = Literal["not_offered", "out_of_range"]


def offered_for(identity: DisplayIdentity | None, report: PictureReport | None) -> tuple[HardwareSetting, ...] | None:
    """What the display on an Output offers, from the Output's recorded picture report: its `offered`
    when the report describes `identity` (the identity recorded on the Output from its power report;
    both not None and equal), else None (unknown). Pure."""
    if identity is None or report is None or report.identity != identity:
        return None
    return report.offered


def edit_refusal(code: VcpCode, value: int | None,
                 offered: tuple[HardwareSetting, ...] | None) -> EditRefusal | None:
    """None when `value` may be set on `code`. "not_offered": `code` is off PICTURE_LIST; or `value`
    is not None and `offered` is None, lacks `code`, or offers it READ_ONLY_CHOICE. "out_of_range":
    a LEVEL value outside 0..maximum, or a CHOICE value its choices do not list. Pure."""
    if code not in PICTURE_LIST:
        return "not_offered"
    if value is None:
        return None
    setting = offered_setting(offered, code)
    if setting is None or setting.range.kind is RangeKind.READ_ONLY_CHOICE:
        return "not_offered"
    if setting.range.kind is RangeKind.LEVEL:
        maximum = setting.range.maximum or 0
        return None if type(value) is int and 0 <= value <= maximum else "out_of_range"
    return None if any(choice.value == value for choice in setting.range.choices) else "out_of_range"


def offered_setting(offered: tuple[HardwareSetting, ...] | None, code: VcpCode) -> HardwareSetting | None:
    """The setting `offered` holds for `code`; None when the list is unknown or lacks it. Pure."""
    if offered is None:
        return None
    return next((setting for setting in offered if setting.code == code), None)


def level_share(value: int, source_maximum: int, target_maximum: int) -> int:
    """`value` of `source_maximum` at the same share of `target_maximum`, rounded half to even
    (Python's round): 46 of 100 -> 117 of 255. Pure; ValueError("level_share") unless both maxima
    are >= 1 and 0 <= value <= source_maximum."""
    raise NotImplementedError("picture split S4")


def copied_hardware(source: Mapping[VcpCode, int], *,
                    source_offered: tuple[HardwareSetting, ...] | None,
                    target_offered: tuple[HardwareSetting, ...] | None,
                    ) -> tuple[dict[VcpCode, int], tuple[VcpCode, ...]]:
    """(the values to set on the target, the source codes skipped, in picture-list order). A source
    code is copied when both lists offer it and: both are LEVEL (`level_share` of the two maxima),
    or both are CHOICE and the target lists a Choice equal to the source's (same value and name).
    Anything else is skipped, including every code when either list is unknown. Pure."""
    raise NotImplementedError("picture split S4")


def kept_offered(previous: PictureReport | None, report: PictureReport) -> tuple[HardwareSetting, ...] | None:
    """What to record as the Output's offered list: `report.offered`, unless it is None while
    `previous` describes the same identity (not None, equal) with a known list; then that list.
    Pure."""
    raise NotImplementedError("picture split S4")


@dataclass(frozen=True)
class HardwareRow:
    """One row of the Picture tab's Display (hardware) section. `offered`: what the display offers
    for `code` at its last check (None: set here, not offered then, or the list is unknown);
    `value`: in force (None: as the display has it); `result`: the Pi's write for the latest
    picture change (None: none, or the report answers an older change)."""
    code: VcpCode
    offered: HardwareSetting | None
    value: int | None
    result: HardwareResult | None


def hardware_rows(values: Mapping[VcpCode, int], offered: tuple[HardwareSetting, ...] | None,
                  results: Mapping[VcpCode, HardwareResult] | None) -> tuple[HardwareRow, ...]:
    """One row per code that is offered or set (the union), in PICTURE_LIST order. `results` None:
    the report does not answer the latest picture change. Pure."""
    codes = set(values) | {setting.code for setting in offered or ()}
    return tuple(HardwareRow(code, offered_setting(offered, code), values.get(code),
                             None if results is None else results.get(code))
                 for code in PICTURE_LIST if code in codes)


__all__ = ["EditRefusal", "HardwareRow", "copied_hardware", "edit_refusal", "hardware_rows", "kept_offered",
           "level_share", "offered_for", "offered_setting"]
