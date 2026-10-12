"""The key content analyser (roadmap 5a, run ledger .claude/runs/photo-5a.md slice M1), offline.

Face answers are shaped as Immich v2.5.6's `AssetFaceResponseDto` (parsed into FaceBox by
tests/test_immich_library.py); thumbnails are drawn here with Pillow, no binary fixtures.
"""

from __future__ import annotations

import io
import random

import pytest
from PIL import Image

from central.wall_plan.fit import interest_judged
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.key_content import ANALYSIS_VERSION, INTEREST_COLUMNS, KeyContent
from media.key_content import (
    MeasureError,
    faces_of,
    interest_of,
    measure,
)
from media.library_client import FaceBox


def box(x1, y1, x2, y2, width=1440, height=1080) -> FaceBox:
    return FaceBox(x1=x1, y1=y1, x2=x2, y2=y2, image_width=width, image_height=height)


def encoded(image: Image.Image, kind: str = "JPEG") -> bytes:
    output = io.BytesIO()
    image.save(output, kind)
    return output.getvalue()


def textured_left(width: int = 320, height: int = 240) -> bytes:
    """Grey, with one bright textured square in its left third."""
    image = Image.new("L", (width, height), 128)
    noise = random.Random(5)
    side = width // 4
    top = (height - side) // 2
    for x in range(10, 10 + side):
        for y in range(top, top + side):
            image.putpixel((x, y), 160 + noise.randrange(96))
    return encoded(image)


def column_share(grid, first: int, last: int) -> float:
    return sum(grid.weights[row * grid.columns + column]
               for row in range(grid.rows) for column in range(first, last))


# 1. faces as fractions, clamped and unpadded, on the size Immich measured them ------------------


def test_faces_are_fractions_of_the_size_immich_measured_them_clamped_and_unpadded():
    """Follow-ups B1: no padding at measure time (the fit rule pads when it judges)."""
    preview = (box(0, 0, 100, 100), box(720, 540, 864, 720))
    faces = faces_of(preview)
    assert len(faces) == 2
    corner, middle = faces
    assert (corner.left, corner.top) == (0.0, 0.0)
    assert corner.right == pytest.approx(100 / 1440)
    assert corner.bottom == pytest.approx(100 / 1080)
    assert (middle.left, middle.top) == pytest.approx((0.5, 0.5))
    assert (middle.right, middle.bottom) == pytest.approx((0.6, 720 / 1080))
    # The same faces on a half-size answer are the same fractions: never the original's size.
    half = faces_of(tuple(box(b.x1 // 2, b.y1 // 2, b.x2 // 2, b.y2 // 2, 720, 540)
                          for b in preview))
    assert [f.model_dump() for f in half] == pytest.approx([f.model_dump() for f in faces])


def test_a_face_reaching_past_the_far_edge_is_clamped_to_it():
    (face,) = faces_of((box(1300, 900, 1500, 1100),))
    assert (face.right, face.bottom) == (1.0, 1.0)


# 2. every face kept, only degenerate boxes left out -------------------------------------------


def test_every_face_is_kept_and_only_a_degenerate_box_is_left_out():
    """Follow-ups B1: the smallest face that counts is a setting applied when judged, so a tiny
    face is stored."""
    tiny = box(700, 500, 740, 540)          # 40 x 40 on 1440 x 1080: 0.1 % of the area
    assert len(faces_of((tiny,))) == 1
    assert faces_of((box(300, 300, 300, 500),)) == ()        # no width
    assert faces_of((box(2000, 0, 2100, 100),)) == ()        # wholly outside the image
    assert len(faces_of((tiny, box(300, 300, 500, 500)))) == 2


# 3. the interest grid -------------------------------------------------------------------------


def test_a_bright_textured_square_on_the_left_holds_most_of_the_interest():
    grid, spread = interest_of(textured_left())
    assert spread > 0.05
    assert (grid.columns, grid.rows) == (INTEREST_COLUMNS, 12)
    assert len(grid.weights) == grid.columns * grid.rows
    assert sum(grid.weights) == pytest.approx(1.0, abs=1e-6)
    assert all(weight >= 0 for weight in grid.weights)
    left_third = column_share(grid, 0, INTEREST_COLUMNS // 3) + column_share(
        grid, INTEREST_COLUMNS // 3, INTEREST_COLUMNS // 3 + 1) / 3
    assert left_third > 0.5


@pytest.mark.parametrize("kind", ["JPEG", "PNG", "WEBP"])
def test_a_flat_grey_thumbnail_has_almost_no_spread_and_is_judged_as_even(kind):
    """Follow-ups B1: the spread is measured, the threshold applied when judged."""
    grid, spread = interest_of(encoded(Image.new("RGB", (320, 180), (128, 128, 128)), kind))
    assert (grid.columns, grid.rows) == (16, 9)
    assert spread < FitSettings().min_salience_spread
    key = KeyContent(analysis_version=ANALYSIS_VERSION, interest=grid, spread=spread)
    assert interest_judged(key, FitSettings()).weights == pytest.approx((1 / 144,) * 144)


def test_rows_follow_the_aspect_within_the_grid_bounds():
    assert interest_of(encoded(Image.new("L", (320, 12), 90)))[0].rows == 1
    assert interest_of(encoded(Image.new("L", (40, 400), 90)))[0].rows == 64


# 4. undecodable bytes -------------------------------------------------------------------------


@pytest.mark.parametrize("data", [b"", b"not an image at all", b"\xff\xd8\xff\xe0broken"])
def test_bytes_that_are_not_an_image_fail_to_decode(data):
    with pytest.raises(MeasureError) as caught:
        interest_of(data)
    assert caught.value.reason == "decode"


# 5. one measurement ---------------------------------------------------------------------------


def test_a_measurement_stores_unpadded_boxes_and_the_spread_at_version_two():
    """Follow-ups B1 case 10: `measure` stores the boxes as Immich found them and the saliency
    map's spread; ANALYSIS_VERSION is "2"."""
    key = measure((box(0, 0, 200, 200),), textured_left())
    assert ANALYSIS_VERSION == "2" and key.analysis_version == ANALYSIS_VERSION
    (face,) = key.faces
    assert (face.right, face.bottom) == pytest.approx((200 / 1440, 200 / 1080))
    assert key.interest.columns == INTEREST_COLUMNS
    assert key.spread == interest_of(textured_left())[1] > 0
