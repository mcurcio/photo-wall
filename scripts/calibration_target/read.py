"""Reading located photos against their sent entry (decision 0026; the owner's reflection plan,
2026-10-13). Pure.

The rules, in order:
1. **Each surface against itself.** A patch's value is the median of its middle PATCH_SHARE, less
   its own surface's BLACK in its group; only the whites cross surfaces (screen white over paper
   white). Nothing is subtracted across surfaces.
2. **Reflection edges drop the group.** A group whose screen black or paper black stands above the
   median of its surface's group blacks by more than BLACK_EDGE_TOLERANCE of the white has a
   reflection edge across it: the whole group is dropped (REFLECTION_EDGE), wherever it is.
3. **Copies vote.** Per value, copies outside their tolerance of the median of the rest are dropped
   (COPY_UNLIKE_THE_REST); a value with fewer than two agreeing copies is unread
   (TOO_FEW_CLEAN_COPIES, or COPIES_DISAGREE when the copies split). Never averaged across a split.
4. **Refuse rather than guess.** Under half the white copies left: WHITE_UNREAD, `where` the dropped
   groups' places. A white patch over CLIP_SHARE clipped: CLIPPED.
5. **Two photos.** The codes must name one entry (DIFFERENT_ENTRIES); each photo's white is read
   alone first and they must agree within PHOTOS_TOLERANCE (PHOTOS_DISAGREE: a shadow over the
   whole paper shows only so); then per patch the darker of the two readings above its black is
   kept (reflections only add light) and the copies vote as one set.
6. **Normal photos** give the white only: `tone` empty, `lowest_distinct` None.

A perfectly even reflection over the whole target is unseen and harmless (both surfaces and the
viewer get it); a whole-paper shadow is unseen in one photo (decision 0026, Costs).
"""
from __future__ import annotations

from central.displays.views import PictureEntryView

from .locate import Located
from .model import Reading, Refused


def read_photos(located: tuple[Located] | tuple[Located, Located], entry: PictureEntryView) -> Reading | Refused:
    """The Reading of one or two located photos of the sent entry their code names (the caller
    fetched `entry` for located[0].code). Refused NOT_A_TARGET when entry.test_pattern is not in
    contracts/tuning.py TARGET_PATTERNS; DIFFERENT_ENTRIES when two photos' codes differ; then the
    module's rules. Two photos of different kinds (RAW and NORMAL) are read as NORMAL. Pure."""
    ...


__all__ = ["read_photos"]
