# Render planes, S4 (video as HEVC): frozen page

Branch `feat/render-planes` at 9e23d30b (S3 landed, milestone 2 green). Plan: `/Volumes/Dock/tmp/node-render-arch/plan.md` v3 §4 S4, §6, §7. Owner: gate answer Q5 (2026-10-18, saved on the plan gate page): "HEVC now, one 1080p copy per video"; chat the same day: "1080p at 30 is fine". Q1: video carries the photo look (the Player's, already built in S1/S3). Process: `~/.claude/skills/implementation-workflow/SKILL.md`. S1's and S3's pages are frozen; this page changes only what it names.

The interface commit that carries this page adds S4's NEW symbols to `media/prepare.py` as code: `StillRecipe` and `VideoRecipe` (complete models: fields are the contract), `HouseRecipes` (its `of` a stub), `stored_recipe` (a stub), and `PreparationLimits.video_threads`. Stubs raise `NotImplementedError("render-planes S4 bead B1")`. **Changes to existing signatures are stated here and made by B1 with every caller and test, in one commit.** A bead that finds a signature wrong appends to `.claude/errata.md` and STOPS.

## What S4 delivers

- The media worker prepares each video as ONE copy: HEVC Main (8-bit 4:2:0), level 4.1, `hvc1` MP4, at most 1920 on its longer side and 1920 x 1080 pixels, at most 30 frames a second, SDR BT.709 limited range, no audio. The libx264 encode is deleted. Sources (Immich originals) may still be H.264 or HEVC: only the copy the Pi plays changes.
- A video's copy is named by the video recipe alone: the same name whatever the wall's Frames, their outputs or the photo copy ladder (`FitSettings.copy_ladder_px`). Before S4 a video's name hashed the ladder's first step, so changing the ladder prepared every video again to identical bytes.
- Photos and videos have a recipe each (`StillRecipe`, `VideoRecipe`), so a change to how videos are encoded never renames a photo's copy again. The photo rules (`central/photo_library/rendition.py`, the copy ladder, the wall need) are unchanged.
- Every existing copy is prepared again, once, by **the preparation key changing**: every recipe id changes (the stored recipe gains `kind`; the video recipe is version 4). No migration: `media_recipes` keeps its shape and its old rows.

**Already landed, so not S4's** (the brief lists them; the work shows them done): the Player decodes only HEVC, in hardware (`player/decode.py` `VideoFrames`: `qtdemux ! h265parse ! v4l2slh265dec ! DMA_DRM caps`, GstVideoMeta through the pad probe, NV12 SAND128 import in `player/gpu.py`; S1 B5), and software H.264 decode and its pipeline are gone from the Player (S3 B8 deleted `player/native.py` and took `gstreamer1.0-libav` and GTK out of `debian/control`). S4 changes no Player file and no `debian/` file.

## Decisions and departures, with their cost

1. **Recipes split per kind (beyond plan v3's "media/prepare.py" line).** One `PreparationRecipe` held both kinds' values, and a variant's id hashes its whole recipe: any video change renamed every photo. That is a coupling defect (values that vary independently in one hashed record); the split removes the class. Cost: S4 owns `central/media_repository.py` too (the naming), and **every photo is prepared again once** as well as every video (plan v3 §7 named only videos). No smaller design avoids it: bumping the one recipe's version or adding a codec field changes every photo's id the same way; keeping photo ids would mean hashing a frozen copy of the old record (compatibility machinery). Preparation is just-in-time (only the photos about to show are prepared), so after deploy each Frame's next photos wait for their new copy, as at the version 2 and 3 bumps; each Pi fetches them again.
2. **The key changes; no migration marks anything stale.** Old variants keep their names and files; the Planner names only the new ones. An old name reached again (a secured assignment held across the deploy whose file was purged) has a recipe row the current models refuse: `stored_recipe` answers None, `variant_inputs` None, and `MediaMaker` its existing `TerminalFailure("variant_inputs_unknown")` instead of today's unhandled pydantic `ValidationError`. An H.264 copy still held by such an assignment fails on the Pi with `failed`/`decode` (the hardware path has no H.264); the Run moves on. Cost: that one held assignment per Frame at deploy time; nothing deploys between S1 and the merge.
3. **The encoder gets threads.** Measured on this Mac (2026-10-18, 10 s of 1080p30, `/Volumes/Dock/tmp/agent-tmp/s4/`): libx264 medium 1.6 CPU-s per second of video; libx265 medium 2.8, single-threaded; with a pool of 4, 0.8 wall-s per second. Preparation's budget (`wall_seconds` 180, `cpu_seconds` 180, 1 thread) would roughly halve the longest video that prepares. `PreparationLimits.video_threads` (default 4) is x265's pool, and the encode's CPU limit is `cpu_seconds x video_threads`, so a video's preparation keeps about today's reach. Cost: four cores busy while a video encodes. Not fixed (a finding, errata): a video longer than the budget allows (well under `max_video_seconds` 300, already before S4) fails `preparation_timeout`, which `MediaMaker` retries as transient.
4. **Level 4.1 pinned, `hvc1` tagged.** x265's `level-idc=4.1` signals level 123 for every copy (a 160 x 96 test clip too), so the output check is exact, as H.264's 4.2 was; level 4.1 holds 1080p60, above the recipe's 30. `hvc1` is the sample entry bench T4 proved on the Pi (`/Volumes/Dock/tmp/pi-render-bench/test4/src/make_clips.sh`). Pinning a level makes x265 apply that level's rate cap (20 Mbit/s) under constant quality; byte-identical output across runs is not a contract (names are by inputs).
5. **CRF 24.** x265's scale is not x264's (x265's default 28 is about x264's 23); 24 spends more bits because a 1080p copy is shown on 4K Frames. A recipe value, so changing it renames videos only.
6. **The wall e2e needs no edit.** `scripts/demo_wall.py` uploads an H.264 original (a source, still accepted), the real media worker image prepares it, and the demo's Player records presentations (`RecordingRenderer`), so its `video_not_prepared` and `media_types_not_presented` checks prove the HEVC copy is made and delivered. `scripts/demo_wall.py` stays S5's to touch if it must.

## Shared primitives (frozen now)

| Primitive | Home | Responsibility | Consumers in S4 | Scheduled |
|---|---|---|---|---|
| `StillRecipe`, `VideoRecipe`, `PreparationRecipe` (their union) | `media/prepare.py` | how each kind's one copy is made; the values its id hashes | `Preparer`, `recipe_id`, `central/media_repository.py`, `media/media_maker.py` (annotation only) | 10-bit video, video above 30 fps (plan v3 §7 deferred): a `VideoRecipe` change |
| `HouseRecipes` | `media/prepare.py` | the recipes prepared by now, one per kind | `Preparer`, `MediaRepository` | |
| `stored_recipe` | `media/prepare.py` | the one reader of a stored recipe | `MediaRepository.variant_inputs` | |
| `variant_recipe` (changed) | `central/media_repository.py` | a copy's recipe and need: a still's from the wall need, a video's from its recipe | `names_for` | |

## B1: video as HEVC, one recipe per kind (about 90 minutes)

**Owns:** `media/prepare.py`, `central/media_repository.py`, `media/media_maker.py` (expected unchanged: its annotation `PreparationRecipe | None` stays valid), `tests/test_prepare.py`, `tests/test_media_recipes.py` (new), `tests/test_media_worker.py`, `tests/test_media_variant_remake.py`, `tests/test_wall_copy_sizing.py`. Derived outputs: none in the repo (pytest `--basetemp` under `$TMPDIR/render-planes-s4-b1/`). **S5 runs at the same time on the same branch: never edit a file outside this list;** S5 must not edit these (its page says so).

**Frozen signatures** (new ones committed; the changed ones are B1's to make):

```python
# media/prepare.py
class StillRecipe(Model):            # committed: kind "image", version 3, long_edge, jpeg_quality
class VideoRecipe(Model):            # committed: kind "video", version 4, long_edge, max_pixels, fps, crf, preset, gop_seconds
PreparationRecipe = Annotated[StillRecipe | VideoRecipe, Field(discriminator="kind")]   # CHANGED: the class is deleted
@dataclass(frozen=True)
class HouseRecipes:
    still: StillRecipe = StillRecipe()
    video: VideoRecipe = VideoRecipe()
    def of(self, kind: Kind) -> StillRecipe | VideoRecipe          # ValueError("recipe_kind")
def stored_recipe(value: object) -> StillRecipe | VideoRecipe | None
def recipe_id(recipe: PreparationRecipe, need: FrameNeed) -> str  # signature and body unchanged
class PreparationLimits(Model):
    ...                                                             # every existing field unchanged
    video_threads: int = Field(default=4, ge=1, le=8, strict=True) # committed
class Preparer:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", *,
                 recipes: HouseRecipes = HouseRecipes(),          # CHANGED: was `recipe: PreparationRecipe`
                 limits: PreparationLimits = PreparationLimits(), clock: Clock | None = None) -> None
    async def prepare_rendition(self, asset: LibraryAsset, rendition: FetchedRendition,
                                destination: Path, *,
                                recipe: PreparationRecipe | None = None) -> PreparedMedia
        # None: self.recipes.of(asset.kind); MediaError("preparation_invalid", "incompatible")
        # when recipe.kind != asset.kind, before the file is read or any tool runs

# central/media_repository.py
@dataclass(frozen=True, slots=True)
class VariantInputs:                 # field types unchanged in name; `recipe: PreparationRecipe` is now the union
    asset: LibraryAsset; recipe: PreparationRecipe; need: FrameNeed; full_size_copies: bool | None = None
@dataclass(frozen=True, slots=True)
class VariantName:                   # likewise
    key: str; recipe_id: str; recipe: PreparationRecipe; step: int
def variant_recipe(recipes: HouseRecipes, kind: Kind, size: tuple[int, int],
                   frames: Sequence[FrameOutput], settings: FitSettings
                   ) -> tuple[PreparationRecipe, FrameNeed]                  # CHANGED (was (base, kind, need))
def names_for(recipes: HouseRecipes, sized: Mapping[str, tuple[str, tuple[int, int] | None]],
              frames: Sequence[FrameOutput], settings: FitSettings) -> dict[str, VariantName]   # CHANGED: first parameter
class MediaRepository:
    recipes: HouseRecipes            # CHANGED: was `recipe: PreparationRecipe`, set in __init__ to HouseRecipes()
    def variant_inputs(self, key: str) -> VariantInputs | None             # unchanged signature; None also when stored_recipe refuses the row
```

No wire shape changes: `MediaReady`, `Variant` (`media_type` stays `"video/mp4"`), `PrepareMedia`, `media_recipes` (`recipe` holds `model_dump(mode="json")` of either model, `need` `{"long_edge": <int>}`) and the media routes are as they are. No migration.

**Rules** (the module docstrings state the ones they own):

- `HouseRecipes.of`: "image" gives `still`, "video" gives `video`; any other value ValueError("recipe_kind").
- `stored_recipe`: `TypeAdapter(PreparationRecipe).validate_python(value)`, None on `ValidationError` (a pre-S4 row has no `kind`); never raises.
- `variant_recipe` (pure): a still: `need = wall_need(size, fitting_outputs("image", *size, frames, settings), settings.copy_steps)`, recipe `recipes.still` with `long_edge = need_step(need)` (as today). A video: recipe `recipes.video`, `need = need_at(size, recipes.video.long_edge)`; it reads neither `frames` nor `settings`. `names_for` keeps its rules (no Frame: no names; an unmeasured size: none; one computation per distinct (kind, size)), the id `recipe_id(recipe, need)` and `step = need_step(need)` for both kinds.
- `Preparer` sizing (`_size`, private): a still by `StillRecipe.long_edge`; a video by `VideoRecipe.long_edge` and `max_pixels`, even dimensions, display aspect kept, never upscaled (today's arithmetic).
- **The video encode** (one ffmpeg process; the shared input flags, `-noautorotate`, `-display_rotation 0` on ffmpeg 6+, `-map 0:v:0 -an -sn -dn -map_metadata -1 -map_chapters -1 -metadata:s:v:0 rotate=0` and the filters `<orientation>, scale=W:H:flags=lanczos, setsar=1, fps=<min(source, recipe.fps)>, colorspace=iall=bt709:irange=<source range>:all=bt709:range=tv:format=yuv420p` as today), then exactly: `-c:v libx265 -preset <recipe.preset> -crf <recipe.crf> -profile:v main -g <ceil(fps x recipe.gop_seconds)> -threads <limits.video_threads> -x265-params log-level=error:pools=<limits.video_threads>:level-idc=4.1:high-tier=0 -tag:v hvc1 -pix_fmt yuv420p -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv -movflags +faststart -f mp4`. Its process's `RLIMIT_CPU` is `ceil(cpu_seconds x video_threads)`; every other process keeps `cpu_seconds`. No `libx264` remains in `media/`.
- **The output check** (`_video(probe, output=True)`; the probe's entries gain `codec_tag_string`): exactly one stream; `codec_name` "hevc", `codec_tag_string` "hvc1", `profile` "Main", `level` 123, `pix_fmt` "yuv420p", `color_range` "tv", `color_space`, `color_primaries` and `color_transfer` "bt709"; then as today: fps at most `recipe.fps + .01`, square pixels, duration within `1/fps + .05` of the source's, the size and orientation asked, and a full decode with `-xerror`. Any miss: MediaError("preparation_invalid", "incompatible"), the destination removed. A source is accepted as today (`h264` or `hevc`).
- Stills: byte-for-byte today's encode (`StillRecipe.long_edge` where `image_long_edge` was).

**Acceptance.**

- Linux media (CI `linux-media`, the pinned worker image; locally with Homebrew's ffmpeg 8): `tests/test_prepare.py`'s video tests prepare to HEVC: one stream, `hevc`, `hvc1`, `Main`, level 123, `yuv420p`, square pixels, BT.709 three times, `tv`, at most 30 fps, `moov` before `mdat`, fully decodable; an H.264 source with audio, a rotated source (pixels match within today's tolerance), and an HEVC source with 2:1 pixels (160 x 48, square) all do. New: a 1 s 2560 x 1440 H.264 source prepares to exactly 1920 x 1080 under the DEFAULT `PreparationLimits` (on Linux: `RLIMIT_AS` 1 GiB, four encoder threads), proving x265 at 1080p fits the worker's limits; a video asset given a `StillRecipe` (and a photo given a `VideoRecipe`) raises `preparation_invalid` and leaves nothing at the destination. If the 1080p test fails only on `RLIMIT_AS`, raise `PreparationLimits.memory_bytes`'s default to `2 * 1024**3`, say so in the commit and append an erratum (the one sanctioned change to a frozen value). The still tests pass with only the recipe renames.
- Unit (`tests/test_media_recipes.py`, pure): `HouseRecipes().of` per kind and its refusal; `stored_recipe` round-trips both house recipes' dumps and answers None for a pre-S4 row (the version 3 `PreparationRecipe` dump with `image_long_edge` and `video_*` fields), for a dict of the wrong kind's fields and for a non-dict; `recipe_id` of a still is unchanged by any `VideoRecipe` value and differs between the two kinds; each model refuses out-of-range values (the old `test_invalid_recipe_settings` cases, per model); `variant_recipe` of a video gives the same recipe and need for any `frames` and any `copy_ladder_px`, while a still's follows the wall need.
- DB (CI `db`): `test_media_worker`, `test_media_variant_remake`, `test_wall_copy_sizing` pass with `StillRecipe(long_edge=1920)` where `PreparationRecipe(image_long_edge=1920)` was and `row["recipe"]["long_edge"]` in `step_of`. New in `tests/test_wall_copy_sizing.py`: a video in the pool is named once, the same on a 1080p wall, a 4K wall and after `copy_ladder_px` changes, its `media_recipes` row a `VideoRecipe` with need `{"long_edge": 1920}`. New in `tests/test_media_variant_remake.py`: a variant whose `media_recipes` row is a pre-S4 recipe gives `variant_inputs` None, and `MediaMaker.handle` raises `TerminalFailure("variant_inputs_unknown")` without fetching.
- Unchanged and green: the immich tier's `tests/test_preparation_rendition.py` (CI `software-e2e` photo-sources: Ubuntu's ffmpeg, libx265 3.5 knows `pools` and `level-idc`), the wall e2e (`software-e2e`), `tests/support/frame_media.py`'s fakes.
- Static: `ruff check .`, `lint-imports`, `python3 scripts/check_docs.py`; `grep -rn libx264 media/ central/` finds nothing.

**Mutation probes** (each run once, then restored): (1) swap `libx265` for `libx264` in the video encode (and nothing else): the output check refuses every video and the Linux media video tests fail. (2) make `variant_recipe` give a video the wall need (`wall_need` over no Frames, the ladder's first step): the unit test with a changed `copy_ladder_px` fails.

**Local runs:** `.venv/bin/python -m pytest -q -n 4 --dist worksteal --basetemp=$TMPDIR/render-planes-s4-b1/pt tests/test_prepare.py tests/test_media_recipes.py` (ffmpeg from Homebrew; on macOS `RLIMIT_AS` is not enforced, so the memory half of the 1080p test is CI's). The DB tests need Docker (`tests/integration/compose.test-database.yml`, `scripts/test_local.py -m db`); when `docker info` does not answer in a minute, push and let CI judge; record NOT RUN.

## Parallel groups

1. **B1** alone (one compile unit: the recipe's shape crosses `media/prepare.py`, `central/media_repository.py` and their tests). It runs beside S5's beads: disjoint files.

## Test architecture

- **Pure, unit tier:** the recipe models, `HouseRecipes`, `stored_recipe`, `recipe_id`'s independence of the other kind, `variant_recipe`. Through each function's own interface; no database, no ffmpeg.
- **Linux media tier (the conformance boundary to the Pi):** the real ffmpeg of the pinned worker image makes and checks the copy the Pi's decoder takes (HEVC Main, level 4.1, `hvc1`, BT.709 limited range, at most 1080p30), at 1080p under the default limits. The Pi's decode of such a copy is bench T4's and the tracer's (a 1080p30 HEVC `hvc1` clip, `v4l2slh265dec`, SAND128 straight to a plane); CI has no V4L2 decoder.
- **DB tier:** naming and re-making through the real `MediaRepository` and `MediaMaker`.
- **End to end:** the wall e2e prepares a video in the real worker image and delivers it to a Player.
- **Pi only (after deploy):** a prepared copy of one of the owner's videos plays on a Frame; not a bead (no deploy before merge).

## Maintenance risks

- **x265 is now on the worker's critical path:** a Debian ffmpeg update that drops libx265 or changes its parameter names fails every video (the Linux media tests catch it before merge, on the pinned image only).
- **The preparation budget is flat while videos are not.** Finding in errata: a long video fails `preparation_timeout` and is retried. A duration-scaled budget, or a lower `max_video_seconds`, is a follow-up the owner picks.
- **Two pinned facts about the Pi live in `VideoRecipe`'s docstring and the output check** (Main 8-bit, level 4.1, `hvc1`), while the Pi's decoder is `player/decode.py`'s. They meet only at the bench; a 10-bit or 60 fps change must change both (plan v3 §7 deferred both).
- **Old recipe rows stay** in `media_recipes` (no migration); harmless, read only by `variant_inputs`, which refuses them.

## Integration points with existing code

- `media/media_maker.py`: passes `inputs.recipe` to `prepare_rendition` as today; its `PreparationClient` annotation names the union.
- `media/worker.py`: builds `Preparer(clock=..., limits=PreparationLimits(...))` as today; `video_threads` takes its default.
- `central/photo_library/rendition.py`, `central/wall_plan/fit.py`: unchanged (`fitting_outputs` already gives a video no Frame; its docstring "a video's copy size is the recipe's" holds).
- `player/`: unchanged (HEVC-only since S1 B5 and S3 B8).
- Docs (S6's bead): `docs/module-media.md` (the video recipe paragraph and the FFmpeg/x264 row), `docs/module-media-preparation.md` (the qualified encoder: libx265 in the pinned Trixie ffmpeg), and the code map's `media/` row if it names H.264.
