"""A Display's picture history, pure (configuration snapshots; owner, 2026-10-13: "configuration
snapshots" that keep "a history of what has been done in the past"; migration 096).

Two kinds of entry, each written where its state converges and never by an editor:

- **Kept** entries, keyed by Display: what Photo Wall keeps, appended by the one kept write
  (central/infra/display_store.py `write_picture`: Done and Copy), numbered per Display in commit
  order under the Display's row lock. A kept write that finds the stored values differ from the
  Display's newest kept entry first appends an unrecorded one (`unrecorded_record`, design rule 3),
  so nothing changes unseen.
- **Sent** entries, keyed by port: what Central sent the Pi, appended by the picture document's
  projection (`output_documents`) each time the port's `picture_change` rises (its number), with the
  Display seen there and the Frame bound to it then (`sent_record` decides the kind).

An entry is a record of what was, never a second source of current state: the stored picture
(displays) and the picture document (picture_documents) stay the truth. Every `at` is PostgreSQL's
clock at the append. The Picture tab's Restore is a session edit and lives with the session's rules
(central/displays/picture.py `restored`); a test pattern is never restored.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from central.displays.picture import DisplayPicture, PictureSession
from contracts.node_picture import TestPatternAsk
from contracts.tuning import TestPattern


class PictureEntryKind(StrEnum):
    """Why an entry was written (the stored `kind`)."""
    KEPT_DONE = "kept-done"                 # Done kept a session's draft
    KEPT_COPIED = "kept-copied"             # Copy wrote another Display's values here
    KEPT_UNRECORDED = "kept-unrecorded"     # the stored values differed from the newest kept entry when a
                                            # kept write came: changed outside Photo Wall at an unknown
                                            # time before `at` (a migration, a repair)
    SENT_DRAFT = "sent-draft"               # an open session's draft (and test pattern): not kept
    SENT_BACK = "sent-back"                 # Revert or a lapse put the wall back from a draft; nothing kept
    SENT_KEPT = "sent-kept"                 # the kept values sent anew (after Done, Copy, a Display newly seen)


KEPT_KINDS: Final = frozenset({PictureEntryKind.KEPT_DONE, PictureEntryKind.KEPT_COPIED,
                               PictureEntryKind.KEPT_UNRECORDED})
SENT_KINDS: Final = frozenset({PictureEntryKind.SENT_DRAFT, PictureEntryKind.SENT_BACK,
                               PictureEntryKind.SENT_KEPT})


@dataclass(frozen=True)
class Port:
    """One HDMI port of one Pi: a sent entry's key."""
    player_id: str
    output_id: str


@dataclass(frozen=True)
class PictureRecord:
    """What a writer appends; the store gives it its id, number and time. `picture`'s LUT, when
    set, names the recipe of its time; `test_pattern` is a sent kind's only (None for every kept
    kind); `restored_from` the entry the session's draft was last restored from."""
    kind: PictureEntryKind
    picture: DisplayPicture
    session_id: UUID | None
    restored_from: UUID | None
    test_pattern: TestPattern | None


@dataclass(frozen=True)
class PictureEntry:
    """One stored entry. `number`: a kept entry's commit order per Display, a sent entry's port
    `picture_change`; `at`: aware UTC, PostgreSQL's clock_timestamp() at the append; `port` and
    `frame_id` (the Frame bound to the port then, None when unbound or since deleted): sent kinds
    only."""
    entry_id: UUID
    display_id: UUID
    number: int
    at: datetime
    port: Port | None
    frame_id: str | None
    record: PictureRecord


def same_values(a: DisplayPicture, b: DisplayPicture) -> bool:
    """`a` and `b` set the same picture: equal hardware, software and signal range, the LUT compared
    by its paper profile alone (the stored picture names today's recipe, an entry the recipe of its
    time, and a new recipe is not a change anyone made)."""
    ...


def unrecorded_record(latest_kept: PictureEntry | None, stored: DisplayPicture) -> PictureRecord | None:
    """Design rule 3, before a kept write: a KEPT_UNRECORDED record of `stored` (no session, no
    restore, no test pattern) when it differs (`same_values`) from the newest kept entry's picture,
    or when the Display has no kept entry and `stored` is not `picture.default_picture()`; else
    None (nothing changed unseen)."""
    ...


def done_record(session: PictureSession) -> PictureRecord:
    """The KEPT_DONE record of `session`'s draft: its session id and `restored_from`."""
    ...


def copied_record(picture: DisplayPicture) -> PictureRecord:
    """The KEPT_COPIED record of the values Copy wrote on a target Display (no session)."""
    ...


def sent_record(*, session: PictureSession | None, now: float, previous: PictureEntry | None,
                picture: DisplayPicture, test_pattern: TestPatternAsk | None) -> PictureRecord:
    """The sent record of a picture document whose picture_change rose at Central's `now`, carrying
    `picture` (the values in force with any pending put-back, as the document) and the ask's pattern:
    - SENT_DRAFT while `session` is live at `now` (its session id and `restored_from`);
    - else SENT_BACK when `previous` (the port's newest sent entry) is SENT_DRAFT and its picture
      differs from `picture` (`same_values`): the draft went and the wall was put back (previous's
      session id, no restore);
    - else SENT_KEPT (no session, no restore).
    ValueError("sent_test_pattern") for a test pattern with no live session (the projection never
    sends one)."""
    ...


__all__ = ["KEPT_KINDS", "SENT_KINDS", "PictureEntry", "PictureEntryKind", "PictureRecord", "Port",
           "copied_record", "done_record", "same_values", "sent_record", "unrecorded_record"]
