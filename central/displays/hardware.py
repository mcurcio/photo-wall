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
- **A known list is kept** (`kept_report`): a report saying "not known yet" never replaces a
  known list for the same display, nor while no display is connected (what a display offered at
  its last check is a fact about that display; a pulled cable or a standby that drops hot-plug
  does not erase it).
- **The tab's rows** (`hardware_rows`): every offered setting and every set code, in picture-list
  order.
- **Changed on the display's buttons** (`changed_on_buttons`; 1c's re-read, kept by the main
  loop's addition, 2026-10-12): a value set in Photo Wall that the display's latest read
  (`HardwareSetting.current`) says is otherwise, while the display is connected, once the Pi
  carried out the picture change in force and took the write (or needed none). Photo Wall does not
  write it back until that setting is set again, or the display is carried out anew.

Which value a setting takes is the contract's (`HardwareRange.holds`); this module asks it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Literal

from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    PICTURE_LIST,
    HardwareResult,
    HardwareSetting,
    PictureReport,
    RangeKind,
    VcpCode,
    WriteResult,
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
    return None if setting.range.holds(value) else "out_of_range"   # one home: the contract


def offered_setting(offered: tuple[HardwareSetting, ...] | None, code: VcpCode) -> HardwareSetting | None:
    """The setting `offered` holds for `code`; None when the list is unknown or lacks it. Pure."""
    if offered is None:
        return None
    return next((setting for setting in offered if setting.code == code), None)


def level_share(value: int, source_maximum: int, target_maximum: int) -> int:
    """`value` of `source_maximum` at the same share of `target_maximum`, rounded half to even
    (Python's round): 46 of 100 -> 117 of 255. Pure; ValueError("level_share") unless both maxima
    are >= 1 and 0 <= value <= source_maximum."""
    if not (_whole(source_maximum) and _whole(target_maximum) and source_maximum >= 1 and target_maximum >= 1
            and _whole(value) and 0 <= value <= source_maximum):
        raise ValueError("level_share")
    return round(Fraction(value * target_maximum, source_maximum))


def copied_hardware(source: Mapping[VcpCode, int], *,
                    source_offered: tuple[HardwareSetting, ...] | None,
                    target_offered: tuple[HardwareSetting, ...] | None,
                    ) -> tuple[dict[VcpCode, int], tuple[VcpCode, ...]]:
    """(the values to set on the target, the source codes skipped, in picture-list order). A source
    code is copied when both lists offer it and: both are LEVEL (`level_share` of the two maxima),
    or both are CHOICE and the target lists a Choice equal to the source's (same value and name).
    Anything else is skipped, including every code when either list is unknown. Pure."""
    copied: dict[VcpCode, int] = {}
    skipped: list[VcpCode] = []
    for code in PICTURE_LIST:
        if code not in source:
            continue
        value = _copied_value(source[code], offered_setting(source_offered, code),
                              offered_setting(target_offered, code))
        if value is None:
            skipped.append(code)
        else:
            copied[code] = value
    return copied, tuple(skipped)


def _copied_value(value: int, source: HardwareSetting | None, target: HardwareSetting | None) -> int | None:
    """`value` as the target counts it, or None when it does not carry over (copied_hardware's rule)."""
    if source is None or target is None or source.range.kind is not target.range.kind:
        return None
    if source.range.kind is RangeKind.LEVEL:
        source_maximum, target_maximum = source.range.maximum or 0, target.range.maximum or 0
        return level_share(value, source_maximum, target_maximum) if source.range.holds(value) else None
    if source.range.kind is RangeKind.CHOICE:
        choice = next((item for item in source.range.choices if item.value == value), None)
        return value if choice is not None and choice in target.range.choices else None
    return None


def kept_report(previous: PictureReport | None, report: PictureReport) -> PictureReport:
    """What to record as the Output's picture report: `report`, except that one whose `offered` is
    None keeps `previous`'s identity and known list when `previous` describes a display (identity
    not None, list known) and `report` describes the same one or none (no display connected). Pure."""
    if (report.offered is None and previous is not None and previous.identity is not None
            and previous.offered is not None and report.identity in (None, previous.identity)):
        return replace(report, identity=previous.identity, offered=previous.offered)
    return report


def changed_on_buttons(value: int | None, offered: HardwareSetting | None, result: HardwareResult | None, *,
                       answered: bool) -> bool:
    """Whether the display now holds a value set in Photo Wall otherwise (someone used its own
    buttons): `value` is set, the display's latest read (`offered.current`, the Pi's last read of the
    code, a write's read-back and the periodic re-read included) answered and differs from it, the
    report `answered` the picture change that carries `value`, and that change's write of the code
    was taken or none was needed (a write not taken is "the display did not take it"). Pure."""
    return (answered and value is not None and offered is not None and offered.current is not None
            and offered.current != value and (result is None or result.result is WriteResult.TAKEN))


@dataclass(frozen=True)
class HardwareRow:
    """One row of the Picture tab's Display (hardware) section. `offered`: what the display offers
    for `code` at its last check (None: set here, not offered then, or the list is unknown);
    `value`: in force (None: as the display has it); `result`: the Pi's write for the latest
    picture change (None: none, or the report answers an older change); `changed_on_buttons`:
    `changed_on_buttons`'s rule."""
    code: VcpCode
    offered: HardwareSetting | None
    value: int | None
    result: HardwareResult | None
    changed_on_buttons: bool


def hardware_rows(values: Mapping[VcpCode, int], offered: tuple[HardwareSetting, ...] | None,
                  results: Mapping[VcpCode, HardwareResult] | None, *, connected: bool) -> tuple[HardwareRow, ...]:
    """One row per code that is offered or set (the union), in PICTURE_LIST order. `results` None:
    the report does not answer the picture change that carries `values` (picture.hardware_answered).
    `connected`: a display is connected now; a list kept from its last check never says "changed
    on the display's buttons" while it is away. Pure."""
    codes = set(values) | {setting.code for setting in offered or ()}
    rows = []
    for code in PICTURE_LIST:
        if code not in codes:
            continue
        setting, value = offered_setting(offered, code), values.get(code)
        result = None if results is None else results.get(code)
        rows.append(HardwareRow(code, setting, value, result,
                                changed_on_buttons(value, setting, result, answered=results is not None and connected)))
    return tuple(rows)


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = ["EditRefusal", "HardwareRow", "changed_on_buttons", "copied_hardware", "edit_refusal", "hardware_rows",
           "kept_report", "level_share", "offered_for", "offered_setting"]
