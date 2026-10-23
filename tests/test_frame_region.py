"""The Frame region: Central places the Frame's rectangle on its Output (issue #124; debug overlay
design revision 2; decision 0030: the Pi never computes a Frame's geometry).

`central.displays.model.frame_region` turns the bound Frame's Position box, its screen (the mode
Weston runs, from the Output report's `current_mode`, else the seen Display's preferred mode) and its
turn into whole pixels on the screen as it hangs; the worker's document source sends it on its own
key, `region-<output>`, beside the Output and picture documents, with its own change number
(`region_digest`), so a saved Position or a new mode never raises the Output document's `change`.
Every report travels the real path (a `PgLinkStore` commit judged by the `OutputReportJudge`); the
Position is saved through the one Save CAS; documents come from the worker's `DisplayDocuments`.
"""
from __future__ import annotations

import asyncio
import hashlib

from support.position import PORTRAIT_ON_WIDE, save_position
from test_display_identity import (
    _bind,
    _documents,
    _frame,
    _generation,
    _report,
    _unbind,
    enroll_pi,
)

from central.content_catalog.catalog import device_id_for_serial
from central.displays.model import frame_region, region_digest
from central.infra.display_store import DisplayWakes
from contracts.models import PositionBox
from contracts.node_output import (
    FrameRegion,
    FrameRegionDocument,
    OutputRect,
    decode_output_document,
    decode_region_document,
    encode_frame_region,
    output_document_key,
    region_document_key,
)

WHOLE = PositionBox()
PORTRAIT = PORTRAIT_ON_WIDE.fit


# frame_region and region_digest: pure.

def test_the_region_is_the_frames_box_in_whole_pixels_on_its_screen_as_it_hangs():
    assert frame_region("kitchen", PORTRAIT, (1920, 1080), 0) == FrameRegion(
        "kitchen", (1920, 1080), OutputRect(672, 28, 576, 1024))
    assert frame_region("kitchen", WHOLE, (1920, 1080), 90) == FrameRegion(
        "kitchen", (1080, 1920), OutputRect(0, 0, 1080, 1920))


def test_no_frame_no_box_or_no_screen_is_no_region():
    assert frame_region(None, WHOLE, (1920, 1080), 0) is None
    assert frame_region("kitchen", None, (1920, 1080), 0) is None
    assert frame_region("kitchen", WHOLE, None, 0) is None


def test_the_digest_is_the_regions_canonical_encoding_or_null():
    region = frame_region("kitchen", WHOLE, (1920, 1080), 0)
    assert region_digest(region) == hashlib.sha256(encode_frame_region(region)).hexdigest()
    assert region_digest(None) == hashlib.sha256(b"null").hexdigest()
    assert region_digest(frame_region("kitchen", WHOLE, (1920, 1080), 90)) != region_digest(region)


# The region document, through the worker's document source.

def _region(registry, output_id: str = "HDMI-A-1") -> FrameRegionDocument:
    return decode_region_document(_documents(registry, "pi-a")[region_document_key(output_id)])


def _output_change(registry, output_id: str = "HDMI-A-1") -> int:
    return decode_output_document(_documents(registry, "pi-a")[output_document_key(output_id)]).change


def _mode(registry, mode: tuple[int, int]) -> None:
    """The Pi's Output report of HDMI-A-1: Weston runs `mode` there, no Display identity read."""
    _report(registry, "pi-a", "HDMI-A-1", None, current_mode=mode)


def _kitchen(registry) -> str:
    pi = enroll_pi(registry, "pi-a")
    _bind(registry, _frame(registry, "kitchen"), pi, "HDMI-A-1")
    _mode(registry, (1920, 1080))
    return pi


def test_a_saved_position_moves_the_region_and_not_the_output_document(registry):
    """(a) Mutation probe: add the region's digest to the Output document's -> its change rises."""
    _kitchen(registry)
    first, change = _region(registry), _output_change(registry)
    assert first == FrameRegionDocument("HDMI-A-1", 1, FrameRegion("kitchen", (1920, 1080),
                                                                     OutputRect(0, 0, 1920, 1080)))
    assert _region(registry) == first                               # nothing moved: the same change

    save_position(registry, "kitchen", PORTRAIT_ON_WIDE)
    assert _region(registry) == FrameRegionDocument("HDMI-A-1", 2, FrameRegion(
        "kitchen", (1920, 1080), OutputRect(672, 28, 576, 1024)))
    assert _output_change(registry) == change


def test_a_turn_turns_the_regions_screen_and_its_rectangle(registry):
    """(b) A quarter turn: the screen as it hangs is 1080 x 1920, and the whole-screen box with it."""
    _kitchen(registry)
    assert _region(registry).region == FrameRegion("kitchen", (1920, 1080), OutputRect(0, 0, 1920, 1080))
    registry.turn_frame("kitchen", 90, expected_generation=_generation(registry, "kitchen"))
    turned = _region(registry)
    assert (turned.change, turned.region) == (2, FrameRegion("kitchen", (1080, 1920), OutputRect(0, 0, 1080, 1920)))


def test_a_new_mode_gives_the_region_its_pixels_and_not_the_output_document(registry):
    """(c) Weston runs 3840 x 2160 now: the saved box lands at twice the pixels. Mutation probe:
    `screen_px` skips `current_mode` -> the region stays at no screen (no Display seen)."""
    _kitchen(registry)
    save_position(registry, "kitchen", PORTRAIT_ON_WIDE)
    before, change = _region(registry), _output_change(registry)
    assert before.region == FrameRegion("kitchen", (1920, 1080), OutputRect(672, 28, 576, 1024))

    _mode(registry, (3840, 2160))
    after = _region(registry)
    assert (after.change, after.region) == (before.change + 1, FrameRegion(
        "kitchen", (3840, 2160), OutputRect(1344, 56, 1152, 2048)))
    assert _output_change(registry) == change


def test_an_unbound_output_gets_no_region(registry):
    """(d) HDMI-A-2 carries no Frame: region null; and an unbind takes HDMI-A-1's away."""
    _kitchen(registry)
    assert _region(registry, "HDMI-A-2") == FrameRegionDocument("HDMI-A-2", 1, None)
    bound = _region(registry)
    _unbind(registry, "kitchen")
    assert _region(registry) == FrameRegionDocument("HDMI-A-1", bound.change + 1, None)


def test_a_bound_output_with_no_screen_known_gets_no_region(registry):
    """No Output report states a mode and no Display is seen: the region is null, not a guess."""
    pi = enroll_pi(registry, "pi-a")
    _bind(registry, _frame(registry, "kitchen"), pi, "HDMI-A-1")
    assert _region(registry) == FrameRegionDocument("HDMI-A-1", 1, None)


def test_a_saved_position_wakes_the_nodes_document_source(registry):
    """Save wakes the bound Node's document source on commit, so its region goes without waiting
    for a deadline. Mutation probe: drop the wake from `commit_calibration_trial_in` -> times out."""
    _kitchen(registry)
    device = device_id_for_serial("pi-a")
    wakes = DisplayWakes()

    async def woken_by_save() -> None:
        listening = asyncio.create_task(wakes.listen(registry.db.dsn))
        try:
            seen = await asyncio.wait_for(wakes.after(device, wakes.count(device)), 5)   # it listens
            await asyncio.to_thread(save_position, registry, "kitchen", PORTRAIT_ON_WIDE)
            await asyncio.wait_for(wakes.after(device, seen), 5)
        finally:
            listening.cancel()

    asyncio.run(woken_by_save())
