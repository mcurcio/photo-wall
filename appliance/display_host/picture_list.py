"""What a display offers from the picture list: the Pi asks each code (decision 0022; the picture
split, slices S1 and S3).

A display's own self-description (the MCCS capabilities string) lies: the test monitor lists only
code 02 yet answers 10, 12 and D6 (display-control skill, Traps), and ddcutil's docs say "the only
way to know for sure … is to try it". So the Pi asks every code on
`contracts.node_picture.PICTURE_LIST` once per display, and a code is offered when it answers as a
level (an MCCS continuous answer) or a single value (simple non-continuous). A list setting's
choices come from the capabilities string when it names them (S3); otherwise, or with more than
MAX_CHOICES, it is offered READ_ONLY_CHOICE.

Rules this module owns (one home each):
- **Ask on the port, picture's turn:** codes are asked ASK_BATCH at a time, each batch one
  `getvcp` with Urgency.PICTURE (ddc.py: a power command always goes first, and a picture command
  holds the port at most PICTURE_HOLD_SECONDS).
- **Known or not:** the ask is known only when every batch's command finished; a batch that did
  not finish leaves the whole list unknown (None), asked again later, never reported partial. A
  finished command answers the codes it read; the rest are not offered. An Output with no DDC/CI
  bus offers nothing: known, ().
- **One answer to one setting** (`offered_setting`).
- **Choices from the capabilities string** (`parse_capability_choices`, S3): names cut to
  CHOICE_NAME_MAX printable ASCII.
- **The last list per display survives a controller restart** (`OfferedCache`,
  `FileOfferedCache`, S3): a cache, correct when empty (the Pi asks again).

S1 lands the ask; S3 the capabilities string's choices and the cache.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final, Protocol

from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    MAX_CHOICES,
    PICTURE_LIST,
    Choice,
    HardwareRange,
    HardwareSetting,
    RangeKind,
    VcpCode,
)

from .ddc import PICTURE_HOLD_SECONDS, DdcChannel, Urgency, VcpAnswer, VcpReading

ASK_BATCH: Final = 4                # codes per getvcp (one port hold); the bench step measures it
ASK_AGAIN_SECONDS: Final = 60.0     # an unknown list is asked again this long after the last try


def offered_setting(code: VcpCode, answer: VcpAnswer, choices: tuple[Choice, ...] | None) -> HardwareSetting:
    """The setting one answer offers. A VcpReading is a LEVEL of its maximum, current its current;
    a VcpByte is a CHOICE of `choices` when 1..MAX_CHOICES are named, else READ_ONLY_CHOICE, current
    its value. S1 passes choices None until S3 (every list setting READ_ONLY_CHOICE). Pure."""
    if isinstance(answer, VcpReading):
        return HardwareSetting(code, HardwareRange(RangeKind.LEVEL, answer.maximum, ()), answer.current)
    if choices is not None and 1 <= len(choices) <= MAX_CHOICES:
        return HardwareSetting(code, HardwareRange(RangeKind.CHOICE, None, choices), answer.value)
    return HardwareSetting(code, HardwareRange(RangeKind.READ_ONLY_CHOICE, None, ()), answer.value)


def parse_capability_choices(capabilities: str) -> dict[VcpCode, tuple[Choice, ...]]:
    """The values `ddcutil capabilities` names for each code on PICTURE_LIST ("Feature: 14 (Select
    color preset) / Values: / 05: 6500 K"), strictly increasing by value, each name cut to
    CHOICE_NAME_MAX printable ASCII (a name with none left is dropped); a code naming more than
    MAX_CHOICES is left out (offered READ_ONLY_CHOICE). Pure; never raises on odd text."""
    raise NotImplementedError("picture split S3")


class PictureAsk(Protocol):
    """What the picture controller asks of a display (PictureListAsker over the port)."""

    def ask(self, output_id: str) -> tuple[HardwareSetting, ...] | None:
        """Every offered setting in PICTURE_LIST order; None when the ask is not known (a batch did
        not finish). Blocks for at most the list's batches; never raises for a display that does not
        answer."""
        ...


class PictureListAsker:
    """`PictureAsk` over the port's DDC/CI channel, picture's turn (Urgency.PICTURE)."""

    def __init__(self, channel: DdcChannel) -> None:
        self._channel = channel

    def ask(self, output_id: str) -> tuple[HardwareSetting, ...] | None:
        if self._channel.bus(output_id) is None:
            return ()
        codes = tuple(PICTURE_LIST)
        answers: dict[VcpCode, VcpAnswer] = {}
        for start in range(0, len(codes), ASK_BATCH):
            batch = codes[start:start + ASK_BATCH]
            read = self._channel.read_many(output_id, batch, urgency=Urgency.PICTURE, timeout=PICTURE_HOLD_SECONDS)
            if read is None:
                return None                             # never a partial ask reported as known
            answers.update({code: answer for code, answer in read.items() if code in batch})
        return answers_by_code(answers, {})


class OfferedCache(Protocol):
    """The last known list per display identity, kept across controller restarts (S3)."""

    def get(self, identity: DisplayIdentity) -> tuple[HardwareSetting, ...] | None: ...   # None: none kept

    def put(self, identity: DisplayIdentity, offered: tuple[HardwareSetting, ...]) -> None: ...   # never raises


class FileOfferedCache:
    """`OfferedCache` as one small JSON file per identity in `directory` (the controller's preserved
    runtime directory, as `picture.FileAppliedPictures`). A file that is missing or does not read is
    "none kept"; a failed write is logged (S3)."""

    def __init__(self, directory: Path) -> None:
        raise NotImplementedError("picture split S3")

    def get(self, identity: DisplayIdentity) -> tuple[HardwareSetting, ...] | None:
        raise NotImplementedError("picture split S3")

    def put(self, identity: DisplayIdentity, offered: tuple[HardwareSetting, ...]) -> None:
        raise NotImplementedError("picture split S3")


def answers_by_code(answers: Mapping[VcpCode, VcpAnswer], choices: Mapping[VcpCode, tuple[Choice, ...]],
                    ) -> tuple[HardwareSetting, ...]:
    """Every answered code on PICTURE_LIST as its offered setting (`offered_setting`), in
    PICTURE_LIST order. Pure."""
    offered = []
    for code in PICTURE_LIST:
        if code in answers:
            try:
                offered.append(offered_setting(code, answers[code], choices.get(code)))
            except ValueError:
                continue                                # an answer no setting can hold (past LEVEL_MAX) offers nothing
    return tuple(offered)


__all__ = ["ASK_AGAIN_SECONDS", "ASK_BATCH", "FileOfferedCache", "OfferedCache", "PictureAsk", "PictureListAsker",
           "answers_by_code", "offered_setting", "parse_capability_choices"]
