"""Which of Immich's copies a photo is prepared from (follow-ups 2026-10-11, slice A4; run
ledger .claude/runs/photo-4.md). Owner, 2026-10-09: "use Immich downsampled copy only if it is
at least the visible Frame size, else the original"; owner, 2026-10-11: compatibility measures
"against the Frame output and not the Display". Pure rules, through their public functions."""

import pytest

from central.installation_models import FrameOutput
from central.photo_library.model import CopySize, LibraryAsset, OriginalFile
from contracts.models import FrameProfile
from media.library_client import FrameNeed, covers, fetch_plan, rendition_needs

READABLE = frozenset({"image/jpeg", "image/png", "image/webp"})
LANDSCAPE = FrameProfile(width_px=3840, height_px=2160, diagonal_inches=55)
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
UHD = FrameNeed(aspect=16 / 9, width=3840, height=2160)
SHA = "a" * 40


def photo(*, edited: bool = False, original_type: str | None = "image/jpeg",
          kind: str = "image") -> LibraryAsset:
    return LibraryAsset(
        connection_ref="main", upstream_id="00000000-0000-4000-8000-000000000001",
        revision=SHA + ("-" + "b" * 16 if edited else ""), kind=kind, taken_at=1.0,
        width=4032, height=3024, duration=10.0 if kind == "video" else None,
        original=None if original_type is None else OriginalFile(media_type=original_type))


def preview(width: int, height: int) -> dict:
    return {"preview": CopySize(name="preview", media_type="image/jpeg", width=width,
                                height=height, measured_at=1.0)}


@pytest.mark.parametrize(("asset", "needs", "known", "plan"), [
    (photo(kind="video", original_type="video/mp4"), (UHD,), {}, ("original",)),
    (photo(), (UHD,), {}, ("preview", "original")),
    (photo(edited=True), (UHD,), {}, ("preview", "fullsize")),
    (photo(original_type="image/heic"), (UHD,), {}, ("preview", "fullsize")),
    (photo(original_type=None), (UHD,), {}, ("preview", "fullsize")),
    (photo(), (UHD,), preview(1440, 1080), ("original",)),          # a known preview too small
    (photo(), (FrameNeed(aspect=16 / 9, width=1280, height=720),), preview(1440, 1080),
     ("preview", "original")),                                        # a known preview that holds
    (photo(edited=True), (), {}, ("fullsize",)),                      # no Frame
    (photo(), (), preview(1440, 1080), ("original",)),
    # A 4:3 preview whose long edge passes a 9:16 Frame's but whose 9:16 window (864 wide) does
    # not: the shape decides. Mutation probe: compare long edges and the preview is tried.
    (photo(), (FrameNeed(aspect=9 / 16, width=1080, height=1920),), preview(2048, 1536),
     ("original",)),
])
def test_the_plan_tries_the_preview_then_the_full_resolution_copy(asset, needs, known, plan):
    """A4 case 1."""
    assert fetch_plan(asset, needs, known, READABLE) == plan


def test_a_readable_type_is_the_callers_word():
    """HEIC is fetched as its original once preparation reads it (A5's STILL_MEDIA_TYPES)."""
    assert fetch_plan(photo(original_type="image/heic"), (UHD,), {},
                      READABLE | {"image/heic"}) == ("preview", "original")


def test_needs_are_each_frames_output_or_its_profile_until_reported():
    """A4 case 2: a Frame with no Frame output asks its profile's size (never too small); two
    Frames of one shape and size are one need."""
    frames = (
        FrameOutput(frame_id="hall", profile=PORTRAIT, output_px=None, display_px=None),
        FrameOutput(frame_id="lounge", profile=LANDSCAPE, output_px=(1536, 864),
                    display_px=(3840, 2160)),
        FrameOutput(frame_id="study", profile=LANDSCAPE, output_px=(1536, 864),
                    display_px=(3840, 2160)),
    )
    assert rendition_needs(frames) == (
        FrameNeed(aspect=1080 / 1920, width=1080, height=1920),
        FrameNeed(aspect=16 / 9, width=1536, height=864))
    assert rendition_needs(()) == ()


def test_a_copy_covers_only_when_it_holds_every_need():
    needs = (UHD, FrameNeed(aspect=9 / 16, width=1080, height=1920))
    assert covers(4032, 3024, needs)                # 4032 x 2268 at 16:9; 1701 x 3024 at 9:16
    assert not covers(3024, 2268, needs)            # its 16:9 window is 3024 x 1701
    assert covers(3840, 2160, (UHD,)) and not covers(3839, 2160, (UHD,))
    assert not covers(8000, 6000, ())               # no need: the full-resolution copy is taken
