"""The Frame output, read in one place (follow-ups 2026-10-11, slice A1; run ledger
.claude/runs/photo-4.md). Owner, 2026-10-11: compatibility must be "measuring against the Frame
output and not the Display".

`contracts.models.frame_output` turns a Frame's profile and Position (its Frame fit) and the size
its Output last reported into its picture's pixels; `central.infra.display_store.frame_outputs_in`
is the one read of every Frame's, which the media worker reaches as `PgPhotoLibrary.frame_outputs`.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb
from support.position import save_position
from test_registry import enroll

from central.db import ProcessTransactionClock
from central.infra.display_store import frame_outputs_in
from central.infra.photo_library_store import PgPhotoLibrary
from central.installation_models import FrameOutput
from central.registry import FrameCreate
from contracts.models import Calibration, FrameProfile, RectangleFit, frame_output

LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)
# A centred rectangle half the screen's width: a half-size mat opening.
OPENING = Calibration(fit=RectangleFit(width=0.5))


def test_no_reported_output_has_no_frame_output():
    for output in (None, (0, 0), (1920, 0)):
        assert frame_output(LANDSCAPE, OPENING, output, 0) is None


def test_the_frame_output_follows_the_position_not_the_display():
    """A half-size mat opening on a 1,920 x 1,080 Output needs 960 x 540, not the Display's size."""
    assert frame_output(LANDSCAPE, OPENING, (1920, 1080), 0) == (960, 540)


def _frame(registry, frame_id: str, profile: FrameProfile) -> None:
    wide = profile.width_px > profile.height_px
    registry.create_frame(FrameCreate(id=frame_id, width_mm=500 if wide else 300,
                                      height_mm=300 if wide else 500, profile=profile))


def test_every_frame_reads_its_frame_output_by_frame_id(registry):
    """A bound Frame with a reported Output reads its picture's pixels; a Frame whose Output
    reported 0 x 0 and an unbound Frame read None for both; ordered by frame id."""
    player, _key, _request = enroll(registry, count=2)
    _frame(registry, "c-lounge", LANDSCAPE)
    _frame(registry, "a-hall", PORTRAIT)
    _frame(registry, "b-attic", LANDSCAPE)
    registry.bind("c-lounge", player["player_id"], "HDMI-A-1", expected_generation=0)
    registry.bind("b-attic", player["player_id"], "HDMI-A-2", expected_generation=0)
    save_position(registry, "c-lounge", OPENING)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE outputs SET observation=%s WHERE player_id=%s AND output_id=%s",
                     (Jsonb({"output_id": "HDMI-A-2", "width_px": 0, "height_px": 0,
                             "connected": False}), player["player_id"], "HDMI-A-2"))
        outputs = frame_outputs_in(conn)
    assert outputs == (
        FrameOutput(frame_id="a-hall", profile=PORTRAIT, turn=0, output_px=None, display_px=None),
        FrameOutput(frame_id="b-attic", profile=LANDSCAPE, turn=0, output_px=None, display_px=None),
        FrameOutput(frame_id="c-lounge", profile=LANDSCAPE, turn=0, output_px=(960, 540),
                    display_px=(1920, 1080)),
    )
    store = PgPhotoLibrary(registry.db, times=ProcessTransactionClock(registry.clock))
    assert store.frame_outputs() == outputs


def test_a_turned_frames_rectangle_reads_its_frame_output_on_the_screen_as_it_hangs(registry):
    """A 16:9 Frame across the whole width of a 3840 x 2160 Output hung a quarter turn: the screen
    is 2160 wide, so its Frame output is 2160 x 1215 and its turn is the Frame's (frames.turn)."""
    player, _key, _request = enroll(registry, count=1, size=(3840, 2160))
    _frame(registry, "lounge", LANDSCAPE)
    registry.bind("lounge", player["player_id"], "HDMI-A-1", expected_generation=0)
    generation = next(frame for frame in registry.inventory().frames if frame.id == "lounge").generation
    registry.turn_frame("lounge", 90, expected_generation=generation)
    save_position(registry, "lounge", Calibration(fit=RectangleFit()))
    with registry.db.transaction() as conn:
        outputs = frame_outputs_in(conn)
    assert outputs == (FrameOutput(frame_id="lounge", profile=LANDSCAPE, turn=90, output_px=(2160, 1215),
                                   display_px=(3840, 2160)),)


def test_no_frame_reads_nothing(registry):
    with registry.db.transaction() as conn:
        assert frame_outputs_in(conn) == ()
