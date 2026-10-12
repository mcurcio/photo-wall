"""What a display offers from the picture list: the Pi asks each code (decision 0022; the picture
split, slices S1 and S3).

A display's own self-description (the MCCS capabilities string) lies: the test monitor lists only
code 02 yet answers 10, 12 and D6 (display-control skill, Traps), and ddcutil's docs say "the only
way to know for sure … is to try it". So the Pi asks every code on
`contracts.node_picture.PICTURE_LIST` once per display, and a code is offered when it answers as a
level (an MCCS continuous answer) or a single value (simple non-continuous). A list setting's
choices come from the capabilities string when it names them; otherwise, or with more than
MAX_CHOICES, it is offered READ_ONLY_CHOICE.

Rules this module owns (one home each):
- **Ask on the port, picture's turn:** codes are asked ASK_BATCH at a time, each batch one
  `getvcp` with Urgency.PICTURE (ddc.py: a power command always goes first, and a picture command
  holds the port at most PICTURE_HOLD_SECONDS).
- **Known or not:** the ask is known only when every batch's command finished; a batch that did
  not finish leaves the whole list unknown (None), asked again later, never reported partial. A
  finished command answers the codes it read; the rest are not offered. An Output with no DDC/CI
  bus offers nothing: known, (). A batch or a capabilities run that did not finish is logged with
  its Output, its codes and the time it took (the hold is not measured on the bench yet).
- **One answer to one setting** (`offered_setting`).
- **Choices from the capabilities string** (`parse_capability_choices`): read once per ask, after
  the batches and only when a list setting answered, as one more hold at Urgency.PICTURE; names cut
  to CHOICE_NAME_MAX printable ASCII without `"` and `\\`. A string that does not come leaves every
  list setting READ_ONLY_CHOICE; the list is still known.
- **Read again** (`PictureListAsker.read_again`): the held list's codes re-read in the same batches
  and turn, each current replaced by what it answers now (`with_current`); a batch that did not
  finish leaves the whole read unknown (None), never partial.
- **The last list per display survives a controller restart** (`OfferedCache`,
  `FileOfferedCache`): a cache, correct when empty (the Pi asks again).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, Protocol

from contracts.node_output import OUTPUT_IDS, DisplayIdentity
from contracts.node_picture import (
    CHOICE_NAME_MAX,
    MAX_CHOICES,
    PICTURE_LIST,
    Choice,
    HardwareRange,
    HardwareSetting,
    PictureReport,
    RangeKind,
    VcpCode,
    decode_picture_report,
    encode_picture_report,
)

from .ddc import PICTURE_HOLD_SECONDS, DdcChannel, Urgency, VcpAnswer, VcpReading, answer_value

ASK_BATCH: Final = 4                # codes per getvcp (one port hold); the bench step measures it
ASK_AGAIN_SECONDS: Final = 60.0     # an unknown list is asked again this long after the last try

_FEATURE: Final = re.compile(r"\s*Feature:\s*([0-9A-Fa-f]{2})\b.*")
_VALUES: Final = re.compile(r"\s*Values:\s*")                       # a named list follows, one value a line
_NAMED_VALUE: Final = re.compile(r"\s+([0-9A-Fa-f]{2}):\s*(.*)")    # "         05: 6500 K"
_FILE_PART: Final = re.compile(r"[^A-Za-z0-9._]")                   # what a cache file name keeps of a serial

log = logging.getLogger(__name__)


def offered_setting(code: VcpCode, answer: VcpAnswer, choices: tuple[Choice, ...] | None) -> HardwareSetting:
    """The setting one answer offers. A VcpReading is a LEVEL of its maximum, current its current;
    a VcpByte is a CHOICE of `choices` when 1..MAX_CHOICES are named, else READ_ONLY_CHOICE, current
    its value. Pure."""
    if isinstance(answer, VcpReading):
        return HardwareSetting(code, HardwareRange(RangeKind.LEVEL, answer.maximum, ()), answer.current)
    if choices is not None and 1 <= len(choices) <= MAX_CHOICES:
        return HardwareSetting(code, HardwareRange(RangeKind.CHOICE, None, choices), answer.value)
    return HardwareSetting(code, HardwareRange(RangeKind.READ_ONLY_CHOICE, None, ()), answer.value)


def parse_capability_choices(capabilities: str) -> dict[VcpCode, tuple[Choice, ...]]:
    """The values `ddcutil capabilities` names for each code on PICTURE_LIST ("Feature: 14 (Select
    color preset) / Values: / 05: 6500 K"), strictly increasing by value, each name cut to
    CHOICE_NAME_MAX printable ASCII (a name with none left is dropped); a code naming more than
    MAX_CHOICES is left out (offered READ_ONLY_CHOICE). Values the string lists without names
    ("Values: 00 01 (interpretation unavailable)") name nothing; a value named twice keeps its first
    name. Pure; never raises on odd text."""
    named: dict[VcpCode, dict[int, str]] = {}
    code: VcpCode | None = None
    listing = False
    for line in capabilities.splitlines():
        if (feature := _FEATURE.fullmatch(line)) is not None:
            code, listing = feature.group(1).upper(), False
        elif code is not None and _VALUES.fullmatch(line) is not None:
            listing = True
        elif listing and code is not None and (value := _NAMED_VALUE.fullmatch(line)) is not None:
            name = _choice_name(value.group(2))
            if code in PICTURE_LIST and name:
                named.setdefault(code, {}).setdefault(int(value.group(1), 16), name)
        else:
            listing = False                                 # anything else ends a named list
    return {code: tuple(Choice(value, name) for value, name in sorted(values.items()))
            for code, values in named.items() if len(values) <= MAX_CHOICES}


def _choice_name(text: str) -> str:
    """A display's name for a value as a Choice can hold it: printable ASCII without `"` and `\\`,
    cut to CHOICE_NAME_MAX, outer spaces trimmed ("" when nothing is left)."""
    kept = "".join(character for character in text if " " <= character <= "~" and character not in '"\\')
    return kept.strip()[:CHOICE_NAME_MAX].strip()


def with_current(setting: HardwareSetting, answer: VcpAnswer | None) -> HardwareSetting:
    """`setting` with what it answers now as its current: the answer's value when the setting's
    range holds it, else None (no answer, or a value the range cannot hold). The range is kept.
    Pure."""
    try:
        return replace(setting, current=answer_value(answer))
    except ValueError:
        return replace(setting, current=None)


class PictureAsk(Protocol):
    """What the picture controller asks of a display (PictureListAsker over the port)."""

    def ask(self, output_id: str) -> tuple[HardwareSetting, ...] | None:
        """Every offered setting in PICTURE_LIST order; None when the ask is not known (a batch did
        not finish). Blocks for at most the list's batches; never raises for a display that does not
        answer."""
        ...

    def read_again(self, output_id: str, offered: tuple[HardwareSetting, ...]) -> tuple[HardwareSetting, ...] | None:
        """`offered` with each current read again (the periodic re-read, so a value changed on the
        display's own buttons shows); None when the read is not known (a batch did not finish).
        Bounded as `ask`; never raises for a display that does not answer."""
        ...


class PictureListAsker:
    """`PictureAsk` over the port's DDC/CI channel, picture's turn (Urgency.PICTURE)."""

    def __init__(self, channel: DdcChannel) -> None:
        self._channel = channel

    def ask(self, output_id: str) -> tuple[HardwareSetting, ...] | None:
        if self._channel.bus(output_id) is None:
            return ()
        answers = self._read(output_id, tuple(PICTURE_LIST))
        if answers is None:
            return None                                 # never a partial ask reported as known
        choices: dict[VcpCode, tuple[Choice, ...]] = {}
        if any(not isinstance(answer, VcpReading) for answer in answers.values()):
            began = time.monotonic()
            capabilities = self._channel.capabilities(output_id, urgency=Urgency.PICTURE,
                                                      timeout=PICTURE_HOLD_SECONDS)
            if capabilities is None:
                log.warning("picture: %s's capabilities did not answer (%.2f s, hold %.1f s): its list settings "
                            "are offered read-only", output_id, time.monotonic() - began, PICTURE_HOLD_SECONDS)
            choices = {} if capabilities is None else parse_capability_choices(capabilities)
        return answers_by_code(answers, choices)

    def read_again(self, output_id: str, offered: tuple[HardwareSetting, ...]) -> tuple[HardwareSetting, ...] | None:
        answers = self._read(output_id, tuple(setting.code for setting in offered))
        if answers is None:
            return None
        return tuple(with_current(setting, answers.get(setting.code)) for setting in offered)

    def _read(self, output_id: str, codes: tuple[VcpCode, ...]) -> dict[VcpCode, VcpAnswer] | None:
        """`codes` read ASK_BATCH at a time, each batch one hold at picture's turn: what answered;
        None as soon as one batch did not finish."""
        answers: dict[VcpCode, VcpAnswer] = {}
        for start in range(0, len(codes), ASK_BATCH):
            batch = codes[start:start + ASK_BATCH]
            began = time.monotonic()
            read = self._channel.read_many(output_id, batch, urgency=Urgency.PICTURE, timeout=PICTURE_HOLD_SECONDS)
            if read is None:
                log.warning("picture: %s's getvcp %s did not finish (%.2f s, hold %.1f s): the read is not known",
                            output_id, " ".join(batch), time.monotonic() - began, PICTURE_HOLD_SECONDS)
                return None
            answers.update({code: answer for code, answer in read.items() if code in batch})
        return answers


class OfferedCache(Protocol):
    """The last known list per display identity, kept across controller restarts."""

    def get(self, identity: DisplayIdentity) -> tuple[HardwareSetting, ...] | None: ...   # None: none kept

    def put(self, identity: DisplayIdentity, offered: tuple[HardwareSetting, ...]) -> None: ...   # never raises


class FileOfferedCache:
    """`OfferedCache` as one small JSON file per identity in `directory` (the controller's preserved
    runtime directory, as `picture.FileAppliedPictures`), named
    offered-<maker>-<product>-<serial or "none">-<sha256(name)[:8]>.json, a serial's characters
    outside [A-Za-z0-9._] written "_". The file holds the list in the picture report's own encoding
    (contracts.node_picture, the one home of the wire shape) beside its identity, which `get`
    checks, so two displays whose names meet in one file never share a list. A file that is
    missing, does not read or names another identity is "none kept"; a failed write is logged."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, identity: DisplayIdentity) -> Path:
        serial = "none" if identity.serial is None else _FILE_PART.sub("_", identity.serial)
        name = hashlib.sha256(identity.name.encode()).hexdigest()[:8]
        return self._directory / f"offered-{identity.maker}-{identity.product}-{serial}-{name}.json"

    def get(self, identity: DisplayIdentity) -> tuple[HardwareSetting, ...] | None:
        try:
            kept = decode_picture_report(self._path(identity).read_bytes())
        except (OSError, ValueError):
            return None
        return kept.offered if kept.identity == identity else None

    def put(self, identity: DisplayIdentity, offered: tuple[HardwareSetting, ...]) -> None:
        path = self._path(identity)
        partial = path.with_suffix(".partial")
        try:
            partial.write_bytes(encode_picture_report(PictureReport(_CACHE_OUTPUT, identity, offered, None, {}, None)))
            os.replace(partial, path)
        except (OSError, ValueError):
            log.warning("picture: could not keep %s's list", identity, exc_info=True)


_CACHE_OUTPUT: Final = OUTPUT_IDS[0]    # the cache is per display, not per Output: any valid id carries the list


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
           "answers_by_code", "offered_setting", "parse_capability_choices", "with_current"]
