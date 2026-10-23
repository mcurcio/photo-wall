"""Where the debug overlay draws on one Output: the explain area. PURE: stdlib and contracts only.

The owner, 2026-10-20: "make sure that the debug overlay shows within the configured Frame regions
when available, and only falls back to the Display when the Frames are not configured or cannot be
met"; and "a singular special overlay on the Frames/Display that shows up when errors occur, or
when Central overrides it on". The slate and the health card are that one debug overlay, and this
module is their one layout rule (issue #124): `overlay.render` draws inside the area this module
returns and leaves the screen black outside it.

Rules this module owns (one home each):
- **The explain area**: the Frame region Central sent, when it can be met on the Output as it is
  now; otherwise the whole Output, with the reason.
- **"Cannot be met"**: no region (none sent yet, no Frame bound, no screen known, or the document
  was refused), the screen as it hangs now is not the one Central placed on (a new mode or a turn
  Central has not placed on yet), or the rectangle is smaller than the slate's smallest page.

Rules it does NOT own: where the Frame is (Central, contracts/models.py `frame_placement`; Display
Host never computes a Frame's geometry, decision 0030); when the overlay shows (the judge's
instructions and the shell's slate); what the region's wire shape is (contracts/node_output.py).

The overlay's input is shaped for a second trigger: whether it shows is the caller's question (the
judge's tint, the shell's slate, and later Central's switch); where it shows is this module's
alone, the same for every trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from contracts.node_output import FrameRegion, OutputRect

# Why the explain area is the whole Output.
FallbackReason = Literal[
    "no_region",        # no region on this connection yet, no Frame bound, no screen known, or refused
    "screen_differs",   # the Output's size as it hangs now is not region.screen
    "too_small",        # region.rect is narrower or shorter than MIN_AREA
]
# The slate's smallest page (overlay/render.py `render_slate`: a 1280 x 720 page scaled by at least
# 0.35), so the slate and the card stay legible inside a Frame. A placeholder the bench may tune.
MIN_AREA: Final[tuple[int, int]] = (448, 252)


@dataclass(frozen=True, slots=True)
class FrameArea:
    """The debug overlay draws inside the Frame's rectangle; black outside it."""
    rect: OutputRect


@dataclass(frozen=True, slots=True)
class OutputArea:
    """The debug overlay draws on the whole Output, for `why`."""
    why: FallbackReason


ExplainArea = FrameArea | OutputArea


def explain_area(region: FrameRegion | None, output: tuple[int, int]) -> ExplainArea:
    """PURE. The explain area on an Output whose size as it hangs now is `output` (width, height:
    the shell's `output` configure, already turned):

    - region None -> OutputArea("no_region");
    - region.screen != output -> OutputArea("screen_differs");
    - region.rect.width < MIN_AREA[0] or region.rect.height < MIN_AREA[1] -> OutputArea("too_small");
    - else FrameArea(region.rect).

    Never raises for a well-formed FrameRegion (its own check keeps rect on region.screen, so a
    FrameArea always lies on the Output)."""
    if region is None:
        return OutputArea("no_region")
    if region.screen != output:
        return OutputArea("screen_differs")
    if region.rect.width < MIN_AREA[0] or region.rect.height < MIN_AREA[1]:
        return OutputArea("too_small")
    return FrameArea(region.rect)


def area_rect(area: ExplainArea, output: tuple[int, int]) -> tuple[int, int, int, int]:
    """PURE. The area as (x, y, width, height) on an Output of size `output`: the rect of a
    FrameArea, (0, 0, *output) for an OutputArea. What render.py lays out in."""
    if isinstance(area, FrameArea):
        return area.rect.x, area.rect.y, area.rect.width, area.rect.height
    return 0, 0, *output


__all__ = ["MIN_AREA", "ExplainArea", "FallbackReason", "FrameArea", "OutputArea", "area_rect",
           "explain_area"]
