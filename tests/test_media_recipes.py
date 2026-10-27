"""One recipe per kind (render planes S4, frozen page .claude/runs/render-planes/s4-page.md).

Owner, gate answer Q5 (2026-10-18): "HEVC now, one 1080p copy per video"; "1080p at 30 is
fine". A photo's copy follows the wall need; a video's is named by the video recipe alone, so
neither the wall's Frames nor the photo copy ladder renames it, and a change to how videos are
encoded never renames a photo. Pure: no database, no ffmpeg.
"""

from __future__ import annotations

import pytest
from support.frame_outputs import frame_output_of
from support.frames import Screen

from central.installation_models import FrameOutput
from central.media_repository import StoreLimits, variant_recipe
from central.photo_library.rendition import FrameNeed
from central.wall_plan.fit_settings import FitSettings
from media.prepare import (
    HouseRecipes,
    PreparationLimits,
    StillRecipe,
    VideoRecipe,
    recipe_id,
    stored_recipe,
)

NEED = FrameNeed(aspect=16 / 9, width=1920, height=1080)
P1080 = Screen(1920, 1080, inches=24)
P4K = Screen(3840, 2160, inches=55)
# The one recipe both kinds shared before S4 (its version 3 dump), as `media_recipes` holds it.
PRE_S4 = {"version": 3, "image_long_edge": 1920, "jpeg_quality": 3, "video_long_edge": 1920,
          "video_max_pixels": 2_073_600, "video_fps": 30.0, "video_crf": 23,
          "video_preset": "medium", "gop_seconds": 2.0}


def frame(frame_id: str, profile: Screen) -> FrameOutput:
    return frame_output_of(frame_id, (profile.width_px, profile.height_px),
                           inches=profile.inches)


def test_house_recipes_give_each_kind_its_own_and_refuse_any_other():
    recipes = HouseRecipes(still=StillRecipe(long_edge=1920), video=VideoRecipe(crf=30))
    assert recipes.of("image") == StillRecipe(long_edge=1920)
    assert recipes.of("video") == VideoRecipe(crf=30)
    with pytest.raises(ValueError, match="recipe_kind"):
        recipes.of("audio")  # type: ignore[arg-type]


@pytest.mark.parametrize("recipe", [StillRecipe(), StillRecipe(long_edge=1920), VideoRecipe(),
                                    VideoRecipe(fps=24, crf=20, preset="slow")])
def test_a_stored_recipe_reads_back_as_the_model_it_was_written_by(recipe):
    assert stored_recipe(recipe.model_dump(mode="json")) == recipe


@pytest.mark.parametrize("value", [
    PRE_S4,
    # A dict of the other kind's fields.
    {**VideoRecipe().model_dump(mode="json"), "kind": "image"},
    {**StillRecipe().model_dump(mode="json"), "kind": "video"},
    "image", None, ["image"],
])
def test_a_recipe_the_current_models_refuse_is_none(value):
    assert stored_recipe(value) is None


@pytest.mark.parametrize("video", [VideoRecipe(crf=18), VideoRecipe(fps=24),
                                   VideoRecipe(long_edge=1280), VideoRecipe(preset="slow"),
                                   VideoRecipe(gop_seconds=4)])
def test_a_photos_recipe_id_does_not_change_with_the_video_recipe(video):
    still = HouseRecipes().still
    assert recipe_id(HouseRecipes(video=video).of("image"), NEED) == recipe_id(still, NEED)
    assert recipe_id(still, NEED) != recipe_id(HouseRecipes(video=video).of("video"), NEED)


@pytest.mark.parametrize(("model", "values"), [
    (StillRecipe, {"long_edge": 0}), (StillRecipe, {"jpeg_quality": 32}),
    (VideoRecipe, {"long_edge": 1921}), (VideoRecipe, {"max_pixels": 2_073_601}),
    (VideoRecipe, {"fps": 0}), (VideoRecipe, {"fps": 31}), (VideoRecipe, {"crf": -1}),
    (VideoRecipe, {"gop_seconds": float("nan")}), (VideoRecipe, {"preset": "ultrafast"}),
])
def test_each_recipe_refuses_values_out_of_its_range(model, values):
    with pytest.raises(ValueError):
        model(**values)


@pytest.mark.parametrize("frames", [(), (frame("living", P1080),),
                                    (frame("living", P1080), frame("hall", P4K))])
@pytest.mark.parametrize("ladder", [(1920, 2560, 3840), (640,), (3000, 4096)])
def test_a_videos_recipe_and_need_follow_neither_the_wall_nor_the_copy_ladder(frames, ladder):
    """Mutation probe (2): give a video the wall need (`wall_need` over no Frames, the ladder's
    first step) and a changed `copy_ladder_px` renames it."""
    recipes = HouseRecipes()
    recipe, need = variant_recipe(recipes, "video", (3840, 2160), frames,
                                  FitSettings(copy_ladder_px=ladder))
    assert recipe == recipes.video
    assert (need.width, need.height) == (1920, 1080)


def test_a_photos_recipe_follows_the_wall_need():
    recipes, size = HouseRecipes(), (6000, 4000)
    small, small_need = variant_recipe(recipes, "image", size, (frame("living", P1080),),
                                       FitSettings())
    large, large_need = variant_recipe(recipes, "image", size,
                                       (frame("living", P1080), frame("hall", P4K)),
                                       FitSettings())
    assert (small.long_edge, large.long_edge) == (1920, 3840)
    assert (max(small_need.width, small_need.height),
            max(large_need.width, large_need.height)) == (1920, 3840)
    assert small == recipes.still.model_copy(update={"long_edge": 1920})
    stepped, _ = variant_recipe(recipes, "image", size, (frame("living", P1080),),
                                FitSettings(copy_ladder_px=(2560, 3840)))
    assert stepped.long_edge == 2560


def test_a_prepared_photos_byte_cap_has_one_home():
    """A copy kept at its readable size may be up to `max_pixels` (64 MP): preparation and the
    media store take one cap. Mutation probe AC8 (a 16 MiB literal in StoreLimits): this fails."""
    assert StoreLimits().max_image_bytes == PreparationLimits().max_image_bytes == 48 * 1024**2
