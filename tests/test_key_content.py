"""The key content analyser (roadmap 5a, run ledger .claude/runs/photo-5a.md slice M1), offline.

Face answers are shaped as Immich v2.5.6's `AssetFaceResponseDto` (parsed into FaceBox by
tests/test_immich_library.py); thumbnails are drawn here with Pillow, no binary fixtures.
"""

from __future__ import annotations

import io
import threading

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw
from support.subject_model import FakeSubjects, even_subjects, real_subjects

from central.wall_plan.fit import interest_centre, interest_judged
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


def red_disc_left(width: int = 320, height: int = 240) -> bytes:
    """Grey, with one red disc in its left third (a subject on a plain ground)."""
    image = Image.new("RGB", (width, height), (128, 128, 128))
    radius, x, y = 60, width // 6, height // 2
    ImageDraw.Draw(image).ellipse((x - radius, y - radius, x + radius, y + radius),
                                  fill=(220, 30, 30))
    return encoded(image, "PNG")


def left_third(height: int, width: int) -> np.ndarray:
    """A map where only the left third stands out."""
    stands_out = np.zeros((height, width))
    stands_out[:, :width // 3] = 1.0
    return stands_out


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


# 3. the interest grid: where the photo's subjects stand out (media/subject_map.py) ---------------


def test_a_map_standing_out_in_the_left_third_holds_most_of_the_interest():
    """Subject detection: the grid sums the map at the thumbnail's own size, rows by its aspect.
    Mutation probe: sum the map at the network's 320 x 320 -> 16 rows."""
    subjects = FakeSubjects(left_third)
    grid, spread = interest_of(encoded(Image.new("RGB", (320, 240), (90, 90, 90))), subjects)
    assert subjects.calls == 1
    assert (grid.columns, grid.rows) == (INTEREST_COLUMNS, 12)
    assert sum(grid.weights) == pytest.approx(1.0, abs=1e-6)
    assert all(weight >= 0 for weight in grid.weights)
    assert column_share(grid, 0, 5) + column_share(grid, 5, 6) / 3 > 0.9
    assert spread == 1.0


def test_a_map_with_nothing_standing_out_gives_the_uniform_grid():
    """A map that is zero everywhere has no mass to share: the uniform grid, spread 0.
    Mutation probe: divide by the zero total -> the grid fails validation."""
    grid, spread = interest_of(encoded(Image.new("RGB", (320, 240), (90, 90, 90))),
                               FakeSubjects(lambda height, width: np.zeros((height, width))))
    assert grid.weights == pytest.approx((1 / 192,) * 192)
    assert spread == 0.0


def test_rows_follow_the_aspect_within_the_grid_bounds():
    subjects = even_subjects()
    assert interest_of(encoded(Image.new("L", (320, 12), 90)), subjects)[0].rows == 1
    assert interest_of(encoded(Image.new("L", (40, 400), 90)), subjects)[0].rows == 64


def test_a_red_disc_on_the_left_of_a_grey_ground_stands_out_on_the_real_model():
    """The pinned U-2-Netp through cv2.dnn: the disc holds nearly all of the interest, its
    centre where the disc is (a sixth across, half down).
    Mutation probe: nearest-neighbour resampling into the network -> the centre moves down
    0.001."""
    grid, spread = interest_of(red_disc_left(), real_subjects())
    assert (grid.columns, grid.rows) == (INTEREST_COLUMNS, 12)
    assert column_share(grid, 0, 5) + column_share(grid, 5, 6) / 3 == pytest.approx(.9557,
                                                                                    abs=.005)
    assert interest_centre(grid, "x") == pytest.approx(.1721, abs=3e-4)
    assert interest_centre(grid, "y") == pytest.approx(.5019, abs=3e-4)
    assert spread > .99


@pytest.mark.parametrize("kind", ["JPEG", "PNG", "WEBP"])
def test_a_flat_grey_thumbnail_has_almost_no_spread_on_the_real_model_and_is_judged_as_even(
        kind):
    """Follow-ups B1: the spread is measured, the threshold applied when judged; on U-2-Netp a
    flat frame spreads 0.0015, under `min_salience_spread` 0.05 as the saliency map's did.
    The value pins the rule (media/subject_map.py): mutation probes: no divide by the maximum,
    or no ImageNet mean and deviation -> 0.0017; nearest-neighbour resampling either way
    -> 0.0022; the second output -> another spread."""
    grid, spread = interest_of(encoded(Image.new("RGB", (320, 180), (128, 128, 128)), kind),
                               real_subjects())
    assert (grid.columns, grid.rows) == (16, 9)
    assert spread == pytest.approx(.0015369, abs=2e-5)
    key = KeyContent(analysis_version=ANALYSIS_VERSION, interest=grid, spread=spread)
    judged = interest_judged(key, FitSettings())
    assert judged.even and judged.grid.weights == pytest.approx((1 / 144,) * 144)


def test_the_real_model_answers_concurrent_photos_each_as_it_answers_it_alone():
    """Design erratum E1: one Net behind a lock; four threads measuring four photos at once each
    get the map the photo gets alone.
    Mutation probe: drop the lock around setInput and forward -> maps of other photos."""
    subjects = real_subjects()
    images = [cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) for data in (
        red_disc_left(), red_disc_left(240, 320),
        encoded(Image.new("RGB", (320, 180), (128, 128, 128))),
        encoded(Image.new("RGB", (200, 200), (20, 160, 40))))]
    alone = [subjects.of(image) for image in images]
    together: list[list[np.ndarray]] = [[] for _ in images]
    start = threading.Barrier(len(images))

    def run(index: int) -> None:
        start.wait()
        for _ in range(8):
            together[index].append(subjects.of(images[index]))

    threads = [threading.Thread(target=run, args=(index,)) for index in range(len(images))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for index, answers in enumerate(together):
        assert len(answers) == 8
        for answer in answers:
            assert np.array_equal(answer, alone[index]), index


# 4. undecodable bytes, and a photo the model cannot read ----------------------------------------


@pytest.mark.parametrize("data", [b"", b"not an image at all", b"\xff\xd8\xff\xe0broken"])
def test_bytes_that_are_not_an_image_fail_to_decode(data):
    subjects = even_subjects()
    with pytest.raises(MeasureError) as caught:
        interest_of(data, subjects)
    assert caught.value.reason == "decode" and subjects.calls == 0


def test_a_photo_the_model_cannot_read_fails_as_decode():
    """A forward that raises on one photo is that photo's failure (`decode`, retried later),
    never the worker's. Mutation probe: let cv2.error escape -> the error, not MeasureError."""
    def broken(height: int, width: int) -> np.ndarray:
        raise cv2.error("the network refused this photo")

    with pytest.raises(MeasureError) as caught:
        interest_of(red_disc_left(), FakeSubjects(broken))
    assert caught.value.reason == "decode"


# 5. one measurement ---------------------------------------------------------------------------


def test_a_measurement_stores_unpadded_boxes_and_the_spread_at_version_three():
    """Follow-ups B1 case 10: `measure` stores the boxes as Immich found them and the map's
    spread; ANALYSIS_VERSION is "3" (subject detection: the grid is what stands out).
    Mutation probe: keep the constant at "2" -> fails."""
    key = measure((box(0, 0, 200, 200),), red_disc_left(), FakeSubjects(left_third))
    assert ANALYSIS_VERSION == "3" and key.analysis_version == ANALYSIS_VERSION
    (face,) = key.faces
    assert (face.right, face.bottom) == pytest.approx((200 / 1440, 200 / 1080))
    assert key.interest.columns == INTEREST_COLUMNS
    assert key.spread == 1.0
