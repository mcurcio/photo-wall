# Run ledger: roadmap 5a, how a photo pool lands on Frames

**Branch:** `feat/photo-pool-on-frames`, from `origin/feat/photo-sources` at 1c6e4c9 (delivery 4, PR #81). ONE pull request, stacked on PR #81; it retargets to `main` when #81 merges (owner, 2026-10-10: one PR per delivery, no split).
**Rules:** `~/.claude/skills/implementation-workflow/SKILL.md` (binding). An implementer reads **its lane's page below, the owner answers, and the design sections its page names**, nothing else.
**Design (accepted, r2):** `/Volumes/Dock/tmp/photo-5a/design-draft.md`; owner gate page https://claude.ai/artifact/LgXHkE9hyev7Y2bSn7z6kk. Its "For the code architects" section holds parked notes, settled under "Decisions" below. Review: `/Volumes/Dock/tmp/photo-5a/review-r1.md`. Owner answers: `/Volumes/Dock/tmp/photo-5a/answers.md` (binding, copied below).
**Freeze (F0):** 18b981c, 4952b81, 1cdcdfa, 4503474. Interfaces as compiling code; every new body raises `NotImplementedError`; additive, so every existing consumer stays green.
**Errata:** `grep -a 'E-P5A-' .claude/errata.md`. A page changes only through an erratum and a re-cut: an implementer who hits a wall it cannot resolve inside its owned files appends `E-P5A-<lane>-<n>` and STOPS (status blocked). One it can resolve inside its owned files: resolve it, append the erratum, continue.

## The problem in plain words

Today every Frame of a Scene works out its photo on its own, from the same list in the same order, so Frames of the same shape all show **the same photo at the same time**. Compatibility is two labels, landscape and portrait: a portrait photo is allowed on a landscape Frame, and the Player then shows a narrow strip from its centre, often cutting off faces. Order is newest first, always, with no memory of what was shown.

Delivery 5a, in the owner's words, is "how to apply that pool onto frames, making sure that photos are only assigned to compatible frames and there are never duplicates down at the same time":

- **Fill or don't show.** A photo goes only on a Frame it fills, judged by the ratio of its sides, not by labels. There are no mats, no blur and no pairs: one photo per Frame.
- **Smart crop.** Where a photo is wider or taller than the Frame, the part that is cut away is chosen by what the photo holds: Immich's face boxes stay whole, and a coarse map of where the eye goes (the interest grid) decides the rest. A photo whose cut would lose a face or its interesting part does not go on that Frame. The Player draws the chosen part; preparation never crops.
- **Never on two Frames at once.** Central plans every Frame together, an hour ahead, and never puts one photo on two Frames whose time on screen overlaps, kept stills and hand-overs included.
- **Order and no repeats.** A Scene shuffles by default (or goes oldest first, newest first, or On this day), reshuffling once every photo has had its turn. A photo shown recently ranks lower, but a Frame never goes blank because of it.

## Owner answers (binding; copied verbatim from `/Volumes/Dock/tmp/photo-5a/answers.md`)

> # 5a owner answers (gate page https://claude.ai/artifact/LgXHkE9hyev7Y2bSn7z6kk), 2026-10-11, verbatim
>
> Binding over any older doc text (roadmap 5a/D3, console rows #47-#52, #102, #105).
> - "a portrait photo should narrow land on a landscape frame. And there is no “blur” or “color mat” because a photo either fills the frame or it doesn’t show (it’s incompatible with that frame)" ("narrow" = "never")
> - "It’s about the ratio, not strict labels of “landscape”, “square” or “portrait”"
> - "Correct. One photo per frame."
> - "Never show the same photo on two frames at the same time"
> - Crop: key content decides (see design R5); "Smart crop sounds reasonable"; "Use immich face detection"
> - Span: "Yes" (delete #105 / roadmap C5)
> - "Frames are not running independently, and it’s up to Central to “plan” where the photos are assigned. I suggest an algorithm that computes all frame+photo assignments well in advance"
> - "“Shown” means it was displayed on a frame. Also note that this “no repeat” idea is secondary to always ensuring that frames have content"
> - "If immich face detection is unavailable, then smart crop doesn’t take into account faces. The “different copy” idea shouldn’t matter, because we only ever scale those images — never anything that would change the proportions like crop — because crop happens directly on the Player."
> - Confirmation of the r2 page (plan horizon 1 h default; the wall shows Immich edits): "That all sounds correct. I’m hoping for one clean overnight build —plan the whole batch"
>
> Design accepted: design-draft.md (r2) in this folder; "For the code architects" section = parked notes. Review: review-r1.md. Facts: facts.md.

Also binding from earlier deliveries: breaking changes are "trivially cheap" and there is no backwards compatibility (owner, 2026-10-11); one PR per delivery (2026-10-10); Players are Immich-unaware; V2 node path only; use Immich's downsampled copy only if it is at least the visible Frame size, and prepare lazily ahead of display (2026-10-09).

## Findings for the owner (open; each restated in the PR body)

- **F-1 (departure: no zoom field in the crop).** The design's crop carried an offset and a scale, with 5a always planning scale 1. With a scale below 1 there are two free axes and one offset, so the field had no defined meaning. `PhotoCrop` carries the offset alone. Slow pan and zoom (#60, deferred) will define its own shape.
- **F-2 (departure: "enough interest" is a density test).** The design parked "keep 90 % of the interest grid". An evenly spread photo loses exactly its cut share of interest, so 90 % refused every 4:3 photo on a 16:9 Frame (a 25 % cut), the case the design says goes on to content scoring. The rule now: a cut may remove at most 1.25 times as much interest as area. A plain side may go; a side holding a face or the subject may not. 1.25 is a placeholder to tune on the owner's library.
- **F-3 (the pixel floor is strict).** A photo whose kept window has fewer pixels than the Frame does not go on it (design rule 2). An 8 MP 4:3 photo cropped for a 4K 16:9 Frame keeps 3,264 × 1,836 and is refused. Older libraries show fewer photos on 4K Frames.
- **F-4 (dates are UTC).** "On this day" uses today's date in UTC until roadmap 2b gives the wall a time zone, as the photo library's relative dates do.
- **F-5 (the new dependency's size, measured).** `opencv-contrib-python-headless` 5.0.0.93 (the only OpenCV build with the saliency module) installs 138 MB, plus numpy's 24 MB (macOS arm64, measured 2026-10-11), in the Central and media worker image. The alternative is about 20 lines of numpy implementing the same published method (Hou and Zhang's spectral residual). The design accepted OpenCV; reopen it if the image size matters.
- **F-6 (the crop pixel test's real home).** The design named `tests/test_native_crop_pixels.py` on the `linux-media` leg. The renderer pixel harness runs only in `node-pid1.yml`'s `display-harness` job, so the crop case joins `tests/native_renderer_pixel_harness.py` there.
- **F-7 (history kept).** Shown rows are kept 400 days (longer than the 365-day maximum no-repeat window). A Scene whose pass lasts longer than that starts the rest of its pass over. What each offered layer shows is kept a day after it ends.
- **F-8 (Scenes interleave by position).** When Frames of different Scenes start at one instant, their photos are taken position by position: each Scene's first photo before any Scene's second.
- **F-9 (gap: covered kept stills).** A layer covered by another Scene is never reported presented, so a photo it keeps underneath is not known to be kept. That photo can appear on another Frame later while it is still kept underneath. Rare: it needs a Scene beneath that keeps its photo.
- **F-10 (a new pool starts after measuring).** A photo goes on a Frame only once its key content is measured and its copy is prepared. A brand-new pool shows nothing for the seconds that takes; Frames keep what they show meanwhile.
- **F-11 (cost: the hour is re-planned every second).** Central's scheduler advances every second, and each advance now plans an hour across every Frame. Slice W1 measures the time on a reference wall and reports it. The plan's far end is tentative until its photos are prepared.
- **F-12 (crop limit range).** The Scene's crop limit is 5 % to 60 %, default 35 %.
- **F-13 (hand-placed photos wait their turn too).** A hand-placed photo that is on screen on another Frame (an already locked layer) waits until it is off that Frame. "Never on two Frames at once" holds even for hand-placed photos.
- **F-14 (measurements are asked for in batches).** At most 64 photos are asked to be measured per advance, highest-ranked first, one queued measurement per photo.
- **F-15 (rollout, restated).** No backwards compatibility: a Player before 5a refuses plans that carry a crop. Frames show nothing new between the Central deploy and each Pi's reboot (design "Costs").
- **F-16 (roadmap Developed column).** Not on `main`, so the docs page adds no Developed row.
- **F-17 (face constants are placeholders).** Each face box is padded by 20 % of its size on each side; a face smaller than 0.2 % of the photo's area (about 4.5 % × 4.5 %) is left out.
- **F-18 (V1-named route).** "Presented" reaches Central only on `/v1/player/observations`, a V1-named route. Shown history depends on it; if the V2 clean-up moves it, shown history follows (design parked note).

## Decisions (the design's parked notes, settled here)

| Parked note | Settled |
|---|---|
| Faces: Immich boxes relative to `imageWidth × imageHeight`; store fractions | `media/key_content.py` `faces_of`; `LibraryClient.faces` returns `FaceBox` (pixel box and the size it was measured on). A 403 (no `face.read`) answers no faces. |
| Face padding, minimum face size | 20 % each side; 0.2 % of the area (F-17) |
| Interest grid method | OpenCV static saliency, spectral residual, on the 320 px thumbnail 4 already fetches; 16 columns, rows by aspect |
| "Enough interest" threshold | density rule, 1.25 (F-2) |
| Augmenting-path pick, least-crop placement, batch size | `central/wall_plan/assignment.py`: Kuhn's augmenting paths, then Kuhn-Munkres on `crop_share`; each slot offers (slots at the instant + 4) photos |
| Scenes interleaving | position by position (F-8) |
| Shown-history retention | 400 days; selections a day (F-7) |
| Re-plan budget | `PlannerLimits` and `WallLimits`; W1 measures (F-11) |
| The presented report's route | unchanged (F-18) |
| Plan reacting to "failed" reports | not in 5a (design "Still open") |
| Preparation's 3,840 px long edge vs a panorama's window | not in 5a: the pixel floor is judged on the original; a wide panorama prepared at 3,840 px can show below its Frame's pixels. Stated in the docs page. |
| One health note when face detection is off | `NoFaceDetection` (`no_face_detection`), from Immich's `server/features` `facialRecognition` |

## Run plan

### Lanes (code lanes plus one docs lane; the run merges and integrates after them)

| Lane | Branch | Starts from | After | Page | Expertise |
|---|---|---|---|---|---|
| rules | `lane/photo5a-rules` | `lane/photo5a-rules` (F0) | none | R1 | pure Python geometry and pydantic models |
| media | `lane/photo5a-media` | `lane/photo5a-media` (F0) | none | M1 | async httpx and the Immich API, OpenCV, PostgreSQL store, Procrastinate, the media worker |
| order | `lane/photo5a-order` | `lane/photo5a-order` (F0) | none | O1 | pure domain ranking, PostgreSQL store, Central coordination and Runtime |
| console | `lane/photo5a-console` | `lane/photo5a-console` (F0) | none | K1 | React, the console design system, Playwright |
| docs | `lane/photo5a-docs` | `lane/photo5a-docs` (F0) | none | X1 | owning documents, plain language |
| player | `lane/photo5a-player` | `lane/photo5a-rules` (its tip once R1 lands) | rules | P1 | the Player's GL renderer and executor, the display harness |
| wall | `lane/photo5a-wall` | `feat/photo-pool-on-frames` once rules, media and order are merged into it | rules, media, order | W1 | algorithms (bipartite matching), the Planner, coordination |
| reasons | `lane/photo5a-reasons` | `feat/photo-pool-on-frames` once wall and console are merged into it | wall, console | K2 | React, the console design system, Playwright |

Every lane branch is created at the ledger commit (a descendant of F0). A lane that starts from another ref first runs `git fetch origin && git switch --detach origin/<start ref>`, then pushes `HEAD:lane/photo5a-<own lane>` (a fast-forward: F0 is an ancestor of every start ref).

**Integrate order** (the run merges each lane branch into `feat/photo-pool-on-frames` with a merge commit): rules, media, order (then the wall lane starts), console, docs, player, wall (then the reasons lane starts), reasons, then the INTEGRATE page on top. A conflict in `.claude/errata.md` (two lanes appending) keeps both entries.

**Why this order.** Tracer first, package depth first: R1's rules (the crop window, the on-screen extent, the fit rule) are what the Player, the Planner and the candidates route all import, so they land first and are short. M1 and O1 build the wall plan's inputs (key content, shown history, Scene selection) beside R1, in disjoint files. W1, the Planner itself, consumes all three and so starts once they are merged. The tracer (INTEGRATE I1) drives the whole path once W1 and P1 are in.

**Disjoint files.** Lanes running at once (rules, media, order, console, docs; then player beside wall) own disjoint files, listed on each page. Files two lanes touch are touched in sequence only: `central/media_repository.py` and `central/app.py` (M1, then W1), `central/coordination.py` (O1, then W1), the console (K1, then K2).

**Critical path:** max(R1 90, M1 150, O1 150 min) → W1 180 min → K2 90 min → INTEGRATE 120 min, about 9 h. P1 (90 min) runs beside W1.

### Land command (every lane)

Run it from the lane's own worktree. `<owned>` is the page's owned paths that exist.
```
git add -A -- <owned> && git commit -m '<msg>' -- <owned>
git push origin HEAD:lane/photo5a-<lane>
```
After the lane's last commit, `git status --porcelain` is empty; anything left is a write outside an owned list and stops the run. Lane agents do not edit this ledger; the run's closing step fills in the outcome table.

### Environment and gates

- Each lane runs in its own worktree. Shell setup for every command: `export UV_CACHE_DIR=/Volumes/Dock/tmp/uv-cache TMPDIR=/Volumes/Dock/tmp/pt PATH=$HOME/.nvm/versions/node/v22.23.2/bin:$PATH; mkdir -p $TMPDIR`. `uv sync --frozen`, then `.venv/bin/python`; revert `uv.lock` if anything rewrites it. No lane changes a dependency (F0 added OpenCV).
- pytest `--basetemp=/tmp/pw5a/<lane>/<gate>` (short, for AF_UNIX socket paths). Every test, build or watch runs in the foreground, through `~/.claude/verify-quiet.sh` for long output; take the exit status from the command itself.
- `PHOTO_WALL_NATS_SERVER=/Volumes/Dock/tmp/nats/nats-server-v2.15.0-darwin-arm64/nats-server` for the DB tier's `test_central_*`.
- Real Immich (`immich` marker): `PHOTO_WALL_IMMICH_FIXTURE=<state dir>` from `.venv/bin/python -m scripts.immich_fixture run --keep --setup-only --publish --state-dir /Volumes/Dock/tmp/immich-p5a-<lane>` (builds the fixture image; minutes). Without it these tests skip; list them NOT RUN; CI's `photo-sources` leg judges them.
- Test files: a test module never imports another lane's test module. Shared builders live in `tests/support/` (R1 owns `tests/support/key_content_builders.py`).
- Console: `npm ci` in `central/console` if `node_modules` is missing; the node-run `test_console_*` need it.

| Gate | Command |
|---|---|
| static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` |
| unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal <paths>` |
| db | `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup <paths>` |
| immich | `PHOTO_WALL_IMMICH_FIXTURE=<dir> .venv/bin/python scripts/test_local.py -q -m immich -n 4 --dist loadgroup <paths>` |
| browser | `PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser/<file> -n 4 --browser chromium` (built console) |
| console | `npm --prefix central/console run typecheck`, `run lint`, `run build` |

**Preflight at F0 (2026-10-11).** The tiers ran on 18b981c; 4952b81 (a note kind) re-ran the photo library and Immich unit tests (135 passed); 1cdcdfa changes a constant and docstrings only.
- Static: ruff 0; lint-imports 24 kept, 0 broken (new contract "The wall plan is pure"); `check_docs` 0; console typecheck 0 and lint 0.
- Unit tier, whole: 4,018 passed, 137 skipped, 1 failed: `tests/node/apps/test_player_device_grants.py::test_start_spawns_the_player_with_this_nodes_grants` (macOS has no `time.CLOCK_BOOTTIME`; environmental, it runs on Linux in CI).
- DB tier, whole: 825 passed, 18 skipped (fixtures not set), 1 xfailed (known).
- NOT RUN at F0, because F0 changes no behaviour: browser, console build and catalog, node-bus, Immich fixture, images, software e2e, node-pid1 and the display harness.

**Budget gates.**
- Per lane: 90 min wall-clock (R1, P1, K2, X1), 150 (M1, O1, K1), 180 (W1); at most 8 agents.
- Feature: 12 h wall-clock and 9 M tokens; warning at 80 %; stop after the current lane at 100 %.
- At most two fix cycles per gate, then a pushed `wip/photo5a-<lane>` branch and a stop.
- A course-correction architect pass after the fifth implementer and before the end review.
- Models pinned by id: `claude-opus-5-5` for every code lane; `claude-sonnet-5-5` allowed for X1 (owner, 2026-10-09).

**End (once, over BASE..HEAD):** CI watcher on the PR; verifier (each page's mutation probes, conformance to the pages); review lenses in parallel: **domain** (the wall assignment's rules: no overlap, liveness, order, fit), **correctness of migrations 082 and 083**, **console UX** (`.claude/skills/console-ux/SKILL.md`); one fix agent. The PR goes ready with before and after screenshots, dark and light, of the Scene editor's new rows, the Frame page's photo reasons and Settings › Photo library with the new note.

---

## F0: the freeze (code architect)

| File | What is frozen |
|---|---|
| `contracts/models.py` | `PhotoCrop(offset: Fraction)`; `Layer.crop: PhotoCrop \| None = None` (additive until INTEGRATE) |
| `contracts/photo_crop.py` (new) | `Window`, `CENTRED`, `crop_window(crop, source_width, source_height, frame_aspect) -> Window` |
| `contracts/on_screen.py` (new) | `Showing` (protocol: start, end, fade_in, after_end), `Extent(start, end \| None)` + `overlaps`, `kept_from`, `replaces_kept_at`, `on_screen_extent(showing, later_on_same_frame, max_clock_uncertainty) -> Extent` |
| `central/wall_plan/__init__.py` (new) | the package's docstring (pure) |
| `central/wall_plan/key_content.py` (new) | `ANALYSIS_VERSION` = "1", `INTEREST_COLUMNS` = 16, `UnitBox`, `InterestGrid`, `KeyContent`, `Measured`, `MeasureFailure`, `MeasureFailed`, `Analysis` |
| `central/wall_plan/fit.py` (new) | `DEFAULT_CROP_LIMIT` 0.35, `MAX_LOST_INTEREST_DENSITY` 1.25, `WINDOW_STEPS` 64, `LARGE_FRAME_INCHES` 40, `LARGE_FRAME_VIDEO_SHORT_SIDE` 1080, `FitRefusal`, `MediaKind`, `Fits`, `DoesNotFit`, `FitVerdict`, `crop_share`, `shape_fit`, `interest_kept`, `judge` |
| `central/wall_plan/selection.py` (new, complete) | `Order`, `DEFAULT_NO_REPEAT_DAYS` 7, `MAX_NO_REPEAT_DAYS` 365, `MIN_CROP_LIMIT` 0.05, `MAX_CROP_LIMIT` 0.60, `SceneSelection(order, no_repeat_days, crop_limit)` |
| `central/wall_plan/order.py` (new) | `PassNumber`, `PhotoShowing`, `KeptStill`, `WallHistory(shown, selected, kept)`, `ScenePass`, `scene_passes`, `last_shown_at`, `pass_sequence`, `SceneLine(...)` + `pass_number`, `ranked`, `pick` |
| `central/wall_plan/assignment.py` (new) | `PlanWindow`, `WallLimits`, `Slot`, `FixedShowing`, `Placed`, `UnfilledReason`, `Unfilled`, `PrepareNeed`, `WallPlan`, `plan_wall(...)` |
| `central/wall_plan/ports.py` (new) | `ShownHistory` (record_selected_in, record_presented_in, wall_history_in, prune_in), `KeyContentReader` (analyses_in, request_in), `KeyContentWriter` (asset_to_measure, record_measured, record_failed) |
| `central/infra/shown_history_store.py` (new) | `SHOWN_KEEP_SECONDS`, `SELECTION_KEEP_SECONDS`, `PgShownHistory` |
| `central/infra/key_content_store.py` (new) | `MEASURE_RETRY_SECONDS` 3600, `PgKeyContent(db, *, times, queue=None)` |
| `central/catalog.py` | `Candidate.analysis: Analysis \| None = None` |
| `central/runtime.py` | `Scene.selection: SceneSelection = SceneSelection()`, `Intent.selection` (same default), `Runtime.planning_selections() -> dict[str, SceneSelection]` |
| `central/planner.py` | `PlannerLimits.max_measurements` 64; `MeasureRequest(asset_id)`; `AssignmentSelection.scene_id`, `.pass_number`; `PlannedState`, `PlannedPhoto`; `Projection.measurements`, `.plan` (default empty); `eligible(candidate, profile, crop_limit=DEFAULT_CROP_LIMIT)`; `project(..., history=WallHistory(), horizon_seconds=300, plan_seconds=None, max_clock_uncertainty=0.1, limits=...)` |
| `central/media_queue.py` | `MEASURE_KEY_CONTENT_TASK` = "photo_wall.media.measure", `KEY_CONTENT_QUEUE_PREFIX`; `MediaTaskQueue.enqueue_measure_in`; `ProcrastinateMediaQueue.enqueue_measure_in` |
| `central/media_repository.py` | `MediaRepository.request_measurements(requests) -> int` |
| `central/media_ports.py` | `MediaApplication.request_measurements` |
| `central/photo_library/model.py` | `NoFaceDetection` (`no_face_detection`) in `LibraryNote`; `ContactAnswer.face_detection: bool \| None = None` |
| `media/library_client.py` | `FaceBox`; `LibraryClient.faces(upstream_id) -> tuple[FaceBox, ...]` |
| `media/immich.py` | `ImmichClient.faces` |
| `media/key_content.py` (new) | `FACE_PADDING` 0.2, `MIN_FACE_SHARE` 0.002, `MeasureError`, `faces_of`, `interest_of`, `measure` |
| `media/worker.py` | `WorkerLimits.measure_seconds` 30; `MediaWorker(..., key_content=None)`; `MediaWorker.measure(asset_id)` |
| `player/geometry.py` | `photo_rect(frame_aspect, source_width, source_height, crop=None)` |
| `pyproject.toml`, `uv.lock` | `opencv-contrib-python-headless==5.0.0.93` (brings numpy 2.5.4); import contract "The wall plan is pure"; `central.wall_plan` joins the no-persistence domains |
| `central/console/src/authoring.js`, `tests/test_operator_runtime.py`, `tests/test_authored_media.py` | co-changes: `SCENE_DEFAULTS.selection`; the defaults pin compares a model default as JSON; the candidates route's served keys gain `analysis` |

## Shared primitives (each with one responsibility; placed by the altitude tree)

| Primitive | Responsibility | Consumers now | Scheduled |
|---|---|---|---|
| `contracts/photo_crop.py` `crop_window` | The window a crop names, in fractions | R1's fit rule; P1's `photo_rect` | #60 pan and zoom |
| `contracts/on_screen.py` | When a photo may be on screen; when a kept still takes effect | W1's wall assignment; P1's executor (`kept_from`) | 3b-ii's reaction to failed draws |
| `central/wall_plan/fit.py` | Whether a photo fills a Frame with its key content kept, and its crop | W1 (Planner, `eligible`, candidates route), O1 (authoring check through `eligible`) | K2 reads its refusals through the route |
| `central/wall_plan/key_content.py` | The key content vocabulary | M1 (writes), R1 (judges), W1 (hydrated candidates) | none |
| `central/wall_plan/selection.py` | A Scene's selection settings and their bounds | Runtime, O1, W1, K1 (mirrors the bounds) | #45, #46 when built |
| `central/wall_plan/order.py` | Each Scene's line across passes; the history's shapes | O1 (store), W1 (Planner) | none |
| `central/wall_plan/assignment.py` | The wall assignment | W1 (Planner) | none |
| `central/wall_plan/ports.py` | What depends on the two stores | coordination (O1), MediaRepository (M1), the worker (M1) | none |
| `tests/support/key_content_builders.py` (R1) | Test builders: uniform and banded interest grids, a face box, a measured candidate | R1, W1, INTEGRATE's tracer | none |

## Test architecture

- **Pure rules (R1, O1's order, W1's assignment; unit leg).** Through their public functions only, with builders from `tests/support/key_content_builders.py`: `tests/test_photo_crop.py`, `tests/test_on_screen_extent.py`, `tests/test_key_content_model.py`, `tests/test_photo_fit.py`, `tests/test_planner_order.py`, `tests/test_planner_wall_assignment.py` (Hypothesis property tests for the no-overlap and liveness rules).
- **Analyser (M1, unit leg).** `tests/test_key_content.py`: offline `/faces` answers shaped exactly as Immich v2.5.6's `AssetFaceResponseDto`, and thumbnails drawn in the test with Pillow (no binary fixtures).
- **Stores (M1, O1, db leg).** Through the ports: `tests/test_key_content_store.py`, `tests/test_shown_history.py`; coordination's hooks through `Coordinator` with the Planner's projection scripted by `monkeypatch` of `central.coordination.project` (O1), then through the real Planner (W1).
- **Against real Immich (M1, `immich` marker, CI `photo-sources` leg).** `tests/test_immich_faces.py`: the fixture's manual face comes back as a `FaceBox` on the size it was posted with.
- **Player (P1).** `tests/test_native_math.py` (unit) and case 9 of `tests/native_renderer_pixel_harness.py` (CI `node-pid1.yml` `display-harness`).
- **Console (K1, K2; browser leg, catalog leg).** Browser tests drive the real app and database, seeding pools through the photo library store's worker side and key content through `PgKeyContent.record_measured`.
- **End to end (INTEGRATE, unit leg).** `tests/test_wall_plan_tracer.py`: the design's tracer through the real Planner, the fit rule and the Player's scalar reference.
- **Conformance boundaries.** `KeyContentReader`/`KeyContentWriter` and `ShownHistory` have one implementation each (PostgreSQL) and are tested through the port; the Player and Central meet at `crop_window`, one function both call.

## Maintenance risks

- **Per-second hour plan (F-11).** The Planner now runs an hour-wide matching every second. Its inputs (the whole pool, the history window) grow with the library. W1 reports the time; a residual can cache the far plan between changes if it grows.
- **Two clocks in one rule.** Layer times are Central's clock; the guard uses the Pis' reported uncertainty bound (`CoordinationLimits.max_uncertainty`). Never compare Pi clocks across Pis; the extent widens instead.
- **Scene settings mirrored in the console.** `SCENE_DEFAULTS.selection` and the console's bounds mirror `central/wall_plan/selection.py`; `tests/test_operator_runtime.py` pins the defaults, K1 pins the bounds.
- **The interest constants.** `MAX_LOST_INTEREST_DENSITY`, `FACE_PADDING`, `MIN_FACE_SHARE` are placeholders (F-2, F-17). Changing a measurement constant must bump `ANALYSIS_VERSION`; changing a fit constant must not (fit is judged at read time).
- **OpenCV upgrades.** `cv2.saliency` exists only in the contrib build; a pin bump must keep it (M1's test calls it).

---

## R1 (lane rules): the crop window, the on-screen extent and the photo fit rule

**Owned:**
- `contracts/photo_crop.py`, `contracts/on_screen.py`
- `central/wall_plan/key_content.py`, `central/wall_plan/fit.py`
- `tests/support/key_content_builders.py` (new)
- `tests/test_photo_crop.py`, `tests/test_on_screen_extent.py`, `tests/test_key_content_model.py`, `tests/test_photo_fit.py` (new)

**Design:** "Design rules" 1 and 2, "Contracts" (Photo fit rule, On-screen extent), "Crop limit", R2, R3, R5, R12; findings F-1, F-2, F-3, F-12 here.

**Frozen page.** Every F0 signature in the four source files exactly as frozen; bodies as their docstrings say. Validators (pydantic `model_validator`, no new public name):
- `UnitBox`: `left < right` and `top < bottom`.
- `InterestGrid`: `len(weights) == columns * rows`; each weight finite and >= 0; `abs(sum - 1) <= 1e-6`.
- `Extent`: `end` None or `end > start`.

`tests/support/key_content_builders.py` (new, test-only):
```python
def uniform_grid(columns: int = 16, rows: int = 9) -> InterestGrid: ...
def banded_grid(*, columns: int = 16, rows: int = 9, left: float = 0, right: float = 0,
                top: float = 0, bottom: float = 0, weight: float = 0.9) -> InterestGrid:
    """`weight` of the interest spread evenly over the named edge bands (each a fraction of the
    width or height), the rest spread evenly over the whole image."""
def face(left: float, top: float, right: float, bottom: float) -> UnitBox: ...
def measured(asset_id: str, width: int, height: int, *, faces: tuple[UnitBox, ...] = (),
             grid: InterestGrid | None = None, kind: str = "image",
             captured_at: float = 0, variant: Variant | None = None) -> Candidate:
    """A hydrated candidate with `Measured` key content at ANALYSIS_VERSION."""
```

**Acceptance (unit leg):**
- `tests/test_photo_crop.py`:
  1. `crop_window(CENTRED, w, h, a)` equals the pre-5a `photo_rect` for (1, 200, 100), (2, 200, 100), (4, 200, 100), (.5, 200, 100): (.25, 0, .75, 1), (0, 0, 1, 1), (0, .25, 1, .75), (.375, 0, .625, 1).
  2. A 2:1 photo on a 1:1 Frame: offset 0 → (0, 0, .5, 1); offset 1 → (.5, 0, 1, 1).
  3. A 1:2 photo on a 1:1 Frame: offset 0 keeps the top (0, 0, 1, .5).
  4. Equal aspects: (0, 0, 1, 1) for offsets 0, .3 and 1.
  5. Aspect 0, NaN or inf, and a zero size raise ValueError.
- `tests/test_on_screen_extent.py`:
  1. A "leave_as_is" layer [10, 40] with uncertainty .1: Extent(9.8, 40.2).
  2. A kept still [10, 40] with no later layer: open-ended. With a later "keep_this_photo" layer starting 40 with fade-in 2: end 42.2. With a later "keep_nothing" layer at 40: end 40.2. With a later "leave_as_is" only: open-ended.
  3. A replacement that starts before the kept layer ends (a Scene on top at 20): the end is the kept layer's own end plus the guard.
  4. `overlaps`: touching extents do not overlap; an open one overlaps every later one.
  5. Negative or NaN uncertainty raises ValueError. `kept_from` of a layer at 10 with fade-in 3 is 13.
- `tests/test_key_content_model.py`: each validator refuses its case; `Analysis` round-trips through JSON for both arms.
- `tests/test_photo_fit.py` (Frames 3840 x 2160 at 55" unless named):
  1. **The boundary:** a 4:3 photo (4000 x 3000) with a uniform grid fits a 16:9 Frame: crop share .25, offset .5.
  2. A 4:3 photo with 90 % of its interest in the top quarter fits with offset 0 (the top kept).
  3. A 4:3 photo with interest in both the top and bottom bands is refused `interest_cut`.
  4. A 2:3 photo on 16:9 is refused `beyond_crop_limit`; a 1:1 photo is refused at .35 and fits by shape at .5.
  5. **Ratio, not labels:** a 1.05:1 photo fits a 0.95:1 Frame (the crop is about 9.5 %).
  6. **Faces:** a 2:3 photo on a 9:16 Frame (1080 x 1920) with a face at the left edge fits with offset 0; faces at both the left and right edges are refused `face_cut`.
  7. **Pixel floor:** 4000 x 3000 fits a 3840 x 2160 Frame; 3264 x 2448 is refused `below_frame_pixels`.
  8. **Videos:** a video on a profile with `video` off is `no_video_here`; a 1280 x 720 video on a 40" Frame is `below_frame_pixels`; a 1920 x 1080 video on 16:9 fits with offset .5 and `interest_kept` 1.
  9. `judge` of an image with `key` None raises ValueError; `shape_fit` agrees with `judge` on every size-only refusal of cases 4, 7 and 8.

**Mutation probes:**
- Compare orientation labels instead of the ratio → fit case 5 fails.
- Use "keep 90 % of the interest" → fit case 1 fails.
- Skip the face check → fit case 6's both-edges assertion fails.
- Drop the guard's factor of two → extent case 1 fails.
- End a kept still at its own end → extent case 2 fails.

**Land:** `feat(planner): the crop window, the on-screen extent and the photo fit rule (5a R1)`.

## M1 (lane media): key content, measured

**Owned:**
- `media/key_content.py`, `media/library_client.py` (`faces` only), `media/immich.py` (`faces`, and `contact`'s `face_detection`), `media/worker.py` (`measure`, and `_entry` passing `PgKeyContent`), `media/task_queue.py` (the measure task)
- `central/media_queue.py` (`enqueue_measure_in`), `central/infra/key_content_store.py`, `central/migrations/082_photo_key_content.sql` (new), `central/media_repository.py` (hydration, `request_measurements`, owning a `PgKeyContent`), `central/app.py` (the scheduler line only), `central/photo_library/policy.py` (the note in `contact_health`)
- `tests/test_key_content.py`, `tests/test_key_content_store.py`, `tests/test_immich_faces.py` (new); `tests/test_immich_library.py`, `tests/test_media_worker.py`, `tests/test_media_queue.py`, `tests/test_photo_library_policy.py`, `tests/test_media_repository.py` (each only for the cases below)

**Design:** "Faces: designed twice" (chosen row), "Data flow" (measured ahead, never stalling), R12, the parked notes on faces; Decisions above; F-5, F-14, F-17.

**Frozen page.** Every F0 signature in `media/key_content.py`, `media/library_client.py` (`FaceBox`, `faces`), `central/infra/key_content_store.py`, `central/media_queue.py` (measure), `MediaRepository.request_measurements`, `MediaWorker.measure`, exactly as frozen; bodies as their docstrings say, plus:
- **Migration 082** (`central/migrations/082_photo_key_content.sql`):
  ```sql
  CREATE TABLE photo_key_content (
      asset_id TEXT PRIMARY KEY REFERENCES asset_revisions(asset_id) ON DELETE CASCADE,
      analysis_version TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('measured', 'failed')),
      key JSONB,
      failure TEXT CHECK (failure IN ('decode', 'timeout')),
      measured_at DOUBLE PRECISION NOT NULL,
      retry_at DOUBLE PRECISION,
      CHECK ((status = 'measured') = (key IS NOT NULL)),
      CHECK ((status = 'failed') = (failure IS NOT NULL AND retry_at IS NOT NULL)));
  ```
  One row per asset; a new measurement replaces it (`ON CONFLICT (asset_id) DO UPDATE`).
- **`PgKeyContent`** is the only reader and writer of the table. `analyses_in` reads rows at `ANALYSIS_VERSION`: measured → `Measured(key=...)`; failed with `retry_at > now` → `MeasureFailed`; otherwise absent. `request_in` keeps the asset ids that have an `asset_revisions` row and no current analysis, and calls `queue.enqueue_measure_in` for each (coalescing); without a queue it raises `RegistryError("media_queue_unconfigured", 503)`. `record_failed` sets `retry_at` = database now + `MEASURE_RETRY_SECONDS`; `measured_at` is database now. `asset_to_measure` validates the `asset_revisions` row's metadata as `LibraryAsset` (the rule `media_repository.lease_asset_of` states; `central.infra` cannot import `media_repository`, which builds this store, so the one-line rule is repeated with a comment naming its home, and a residual moves `lease_asset_of` beside `LibraryAsset`).
- **`ProcrastinateMediaQueue.enqueue_measure_in`**: `_defer_coalescing`'s pattern with no `lock`, `queueing_lock` = `KEY_CONTENT_QUEUE_PREFIX + asset_id`.
- **The task** (`media/task_queue.py`): `@app.task(name=MEASURE_KEY_CONTENT_TASK, queue=MEDIA_QUEUE, pass_context=True, retry=MediaRetryStrategy())`, body `await context.additional_context["media_worker"].measure(asset_id)`. `tests/test_media_queue.py`'s registered-task set gains it.
- **`ImmichClient.faces`**: `GET faces?id=<upstream_id>`; each item's `boundingBoxX1/Y1/X2/Y2`, `imageWidth`, `imageHeight` into `FaceBox`; 403 → `()`; an item missing a field or with x2 <= x1 or y2 <= y1 is left out; other failures map as the client's other calls do (`LibraryUnavailable`).
- **`ImmichClient.contact`** reads `GET server/features` and sets `ContactAnswer.face_detection` = its `facialRecognition` (None when the answer lacks it). **`policy.contact_health`** appends `NoFaceDetection()` after the existing notes when `face_detection is False`.
- **`MediaWorker.measure(asset_id)`**: `self.key_content` None → `MediaError("media_config")`; the asset (`asset_to_measure`; None → return); its client (`self._client(asset.connection_ref)`); within `limits.measure_seconds`: `client.faces(asset.upstream_id)` and `client.thumbnail(asset.upstream_id, asset.revision)`; `media.key_content.measure` through `_blocking` (CPU off the loop); `record_measured`. `MeasureError` → `record_failed(reason)`; timeout → `record_failed("timeout")`; `LibraryUnavailable` or `MediaError("asset_changed")` → `RetryableMediaTask`. `_entry` passes `key_content=PgKeyContent(db, times=DatabaseTransactionClock())`.
- **`media/key_content.py`**: as its docstring. `interest_of` decodes with OpenCV (`cv2.imdecode`, grayscale), computes `StaticSaliencySpectralResidual_create().computeSaliency`, sums the map into the grid by area (`cv2.resize` with `INTER_AREA` to columns x rows, then normalise). A map summing to 0 gives the uniform grid.
- **Hydration**: `MediaRepository.__init__` builds `self.key_content = PgKeyContent(db, times=times, queue=queue)`. `_hydrate_candidates` sets `analysis` from `analyses_in(conn, asset_ids, now=now)` (absent → None). `authored_candidates_in`'s stored rows never carry `analysis` (it is reset to None before the immutability compare in `author_candidates_in`).
- **`request_measurements`**: one `self.transaction()`, `self.key_content.request_in(conn, [r.asset_id for r in requests], now=self.times.now_in(conn))`.
- **`central/app.py` scheduler**: after `request_acquisitions`, `await asyncio.to_thread(media_application.request_measurements, projection.measurements)`.

**Acceptance:**
- `tests/test_key_content.py` (unit):
  1. Offline v2.5.6 `/faces` answers (two faces, one 40 x 40 at the top-left of a 1440 x 1080 preview) give `UnitBox`es in fractions, padded 20 %, clamped; the same box on a 720 x 540 answer gives the same fractions.
  2. A face under 0.2 % of the area is left out; a degenerate box is left out.
  3. A thumbnail drawn with Pillow (grey, one bright textured square on its left third) gives a grid with more than half its weight in the left third; a flat grey thumbnail gives the uniform grid.
  4. Bytes that are not an image raise `MeasureError("decode")`.
  5. `measure` stamps `ANALYSIS_VERSION`.
- `tests/test_immich_library.py` (unit): `faces` parses the answer, answers `()` on 403, raises `LibraryUnavailable` on 500; `contact` reads `facialRecognition`.
- `tests/test_photo_library_policy.py` (unit): `face_detection=False` gives the note after the others; True and None give none.
- `tests/test_key_content_store.py` (db): `request_in` queues once per asset (a second call coalesces) and skips a measured asset; a failed asset reads as failed while its `retry_at` is ahead, is absent after it, and `request_in` then queues it again; a row at an older `analysis_version` is absent; deleting the asset row deletes its key content.
- `tests/test_media_worker.py` (db): `measure` over a fake `LibraryClient` records `Measured`; a corrupt thumbnail records `failed/decode`; a client raising `LibraryUnavailable` raises `RetryableMediaTask` and records nothing.
- `tests/test_media_repository.py` (db): `source_pools_in` hydrates `analysis` for a measured pool photo and leaves None for an unmeasured one.
- `tests/test_immich_faces.py` (`immich` + db, CI `photo-sources` leg): on the published fixture, `ImmichClient.faces` for a photo with a manual face (scripts/immich_actions.py `add_face`) returns one `FaceBox` whose image size is the posted one; `measure` over the real thumbnail records `Measured` with one face in the top-left quarter.

**Mutation probes:**
- Divide face boxes by the original's size instead of `image_width` x `image_height` → key content case 1's second answer fails.
- Return failed analyses after their retry is due → store case "absent after" fails.
- Treat 403 as `LibraryUnavailable` → the immich library 403 case fails.

**Land:** `feat(media): measure each photo's faces and interest grid ahead of planning (5a M1)`.

## O1 (lane order): Scene selection, order and shown history

**Owned:**
- `central/wall_plan/order.py`
- `central/runtime.py` (`timeline` copies the Scene's selection onto each Intent)
- `central/infra/shown_history_store.py`, `central/migrations/083_shown_photos.sql` (new)
- `central/coordination.py` (the hooks below)
- `tests/test_planner_order.py`, `tests/test_shown_history.py` (new); `tests/test_runtime.py`, `tests/test_coordination.py` (each only for the cases below)

**Design:** "Contracts" (Order, Coordination rows), "Shown means displayed", "No-repeat only ranks", R11, the TypeScript `SceneSelection` and `ShownEntry`; findings F-4, F-7, F-8, F-9.

**Frozen page.** Every F0 signature in `central/wall_plan/order.py`, `central/wall_plan/ports.py` (`ShownHistory`) and `central/infra/shown_history_store.py`, exactly as frozen; bodies as their docstrings say, plus:
- **Runtime**: `Intent(..., selection=run.scene.selection)` in `timeline` (a child Scene's Intent carries the child's); `planning_selections` as its docstring (the same Scenes `planning_source_refs` walks).
- **Migration 083** (`central/migrations/083_shown_photos.sql`):
  ```sql
  CREATE TABLE photo_selections (
      assignment_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, frame_id TEXT NOT NULL,
      scene_id TEXT NOT NULL, pass_number INTEGER NOT NULL CHECK (pass_number >= 1),
      starts_at DOUBLE PRECISION NOT NULL, ends_at DOUBLE PRECISION NOT NULL,
      fade_in DOUBLE PRECISION NOT NULL CHECK (fade_in >= 0),
      after_end TEXT NOT NULL CHECK (after_end IN ('leave_as_is', 'keep_this_photo', 'keep_nothing')));
  CREATE INDEX photo_selections_ends ON photo_selections(ends_at);
  CREATE TABLE shown_photos (LIKE photo_selections INCLUDING CONSTRAINTS,
      presented_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (assignment_id));
  CREATE INDEX shown_photos_ends ON shown_photos(ends_at);
  CREATE INDEX shown_photos_pass ON shown_photos(scene_id, pass_number);
  CREATE INDEX shown_photos_kept ON shown_photos(frame_id, starts_at DESC) WHERE after_end <> 'leave_as_is';
  ```
- **`PgShownHistory`**: `record_selected_in` inserts `ON CONFLICT DO NOTHING`. `record_presented_in` is `INSERT INTO shown_photos SELECT *, %s FROM photo_selections WHERE assignment_id=%s ON CONFLICT DO NOTHING`, returning whether a selection existed. `wall_history_in`: shown rows with `ends_at >= now - window_seconds`, plus every row whose (scene, pass) is that Scene's highest pass for the named Scenes; selections with `ends_at > now`; for each Frame its latest shown row with `after_end <> 'leave_as_is'`, a `KeptStill` when that row keeps (`since` = `starts_at + fade_in`). `prune_in` deletes selections with `ends_at < now - SELECTION_KEEP_SECONDS` and shown rows with `ends_at < now - SHOWN_KEEP_SECONDS`.
- **`Coordinator`** gains `shown: ShownHistory | None = None` (default `PgShownHistory()`), and:
  - `advance`: before `project`, with `selections = runtime.planning_selections()`, `history = self.shown.wall_history_in(conn, selections, now=now, window_seconds=86400 * max(s.no_repeat_days for s in selections.values()))` (0 when there are none), and `project(..., history=history)`. After each successful `_offer`, `record_selected_in` the plan's new layers whose selection (`projection.selections` by assignment id) has `asset_id`, `scene_id` and `pass_number` all set, as `PhotoShowing` from the layer's frame and times. `_cleanup` calls `prune_in`.
  - `observe`: after recording the event, when `observation.status == "presented"`, `record_presented_in(conn, layer.assignment_id, self.clock.utc())`.
  - `_validate_authored_scene`: `eligible(candidate, profile, scene.selection.crop_limit)`.
- **Order, pure** (`central/wall_plan/order.py`): as its docstring, exactly. Shuffle keys are `hashlib.sha256(f"{scene_id}\x00{pass_number}\x00{asset_id}".encode()).hexdigest()`.

**Acceptance:**
- `tests/test_planner_order.py` (unit):
  1. Shuffle: a pass's sequence holds every candidate once; pass 2's differs from pass 1's; adding a photo keeps the others' relative order.
  2. `SceneLine.pick` rolls to pass 2 exactly when the last pickable asset is picked; an unpickable asset never holds a pass open.
  3. Tiers: a photo shown 2 days ago with `no_repeat_days` 7 ranks after every photo not shown; with `no_repeat_days` 0 it does not; a photo picked in this pass ranks last.
  4. **A repeat rather than nothing:** a line whose only photos were all shown yesterday still ranks them (none is removed).
  5. Chronological continues after the last shown photo and wraps; newest first is descending; On this day puts 11 October photos of earlier years first, newest year first, then the shuffle.
  6. `scene_passes`: the highest pass across shown rows and live selections; `picked` = that pass's assets.
- `tests/test_runtime.py` (unit): an Intent carries its Scene's selection; a child Scene's Intent carries the child's; `planning_selections` names a configured Scene, its child, and a running Run's admitted Scene.
- `tests/test_shown_history.py` (db):
  1. `record_selected_in` twice keeps the first row.
  2. `record_presented_in` copies the selection once (twice is idempotent) and answers False for an unknown assignment.
  3. `wall_history_in` returns rows in the window, the current pass's older rows, live selections, and each Frame's kept still (a later "keep_nothing" row clears it).
  4. `prune_in` keeps a row 399 days old and removes one 401 days old.
  5. Through `Coordinator` (with `central.coordination.project` scripted to return selections with scene and pass): an offer records its new layers' selections; a "presented" observation records shown; a "failed" observation records nothing; a re-offered locked layer records nothing new.
- `tests/test_coordination.py` (db): a Scene with a non-default crop limit is checked at that limit on authoring (a photo passing at 50 % and refused at 35 % is authored on a Scene with 50 %).

**Mutation probes:**
- Record shown on "failed" observations too → shown case 5 fails.
- Drop the current pass's older rows from `wall_history_in` → shown case 3 fails.
- Remove a recently shown photo instead of ranking it lower → order case 4 fails.
- Hash without the pass number → order case 1 fails.

**Land:** `feat(planner): Scene order, passes and shown history (5a O1)`.

## P1 (lane player, after rules): the Player draws the crop

**Owned:**
- `player/geometry.py`, `player/native.py`, `player/executor.py`
- `tests/test_native_math.py`, `tests/test_executor.py` (only the cases below), `tests/native_renderer_pixel_harness.py` (case 9)
- `scripts/release_plan.py` (only if `tests/test_release_plan.py` asks for a path)

**Design:** "Contracts" (Player renderer row), R7, R12; F-6. Starts from `lane/photo5a-rules` (R1's bodies of `crop_window` and `kept_from`).

**Frozen page.**
- **`photo_rect(frame_aspect, source_width, source_height, crop=None)`** returns `crop_window(crop or CENTRED, source_width, source_height, frame_aspect)` (one home for the window; its own centred arithmetic goes). The module docstring's "Photo fit" paragraph says: the window `Layer.crop` names, cut from the Frame profile's aspect (contracts/photo_crop.py), the one crop in either fit; it no longer names #48 or #102.
- **`player/native.py`**: the draw passes `local.layer.crop` to `photo_rect`.
- **`player/executor.py` `_apply_after_end`**: the kept still takes effect at `contracts.on_screen.kept_from(layer)` (replacing `layer.start + layer.fade_in`).
- **Pixel harness case 9**, `crop_offset_keeps_the_left_edge`: the 64 x 48 four-quadrant picture on a 9:16 binding profile in Cover at 0 degrees, a layer with `PhotoCrop(offset=0)`: sampled pixels equal the reference (`picture_source_uv` with that crop) and the picture shows only the left quadrants' colours; the same with offset 1 shows only the right ones.
- `picture_source_uv` gains the same `crop: PhotoCrop | None = None` parameter, passed to `photo_rect`.

**Acceptance:**
- `tests/test_native_math.py` (unit): the old centred cases hold with `crop=None` and with `CENTRED`; offset 0 and 1 give the edge windows on a wide and a tall source; `picture_source_uv` with offset 0 samples u in [0, window width].
- `tests/test_executor.py` (unit): a "keep_this_photo" layer with fade-in 2 is kept from its first draw at or after start + 2, not before (unchanged behaviour, now through `kept_from`).
- Pixel harness case 9 (CI `node-pid1.yml` `display-harness`; NOT RUN locally unless the arm64 harness image is at hand).

**Mutation probes:**
- Ignore `crop` in `photo_rect` → the offset cases fail.
- Pass `crop` but swap the axes in native's draw → harness case 9 fails.

**Land:** `feat(player): draw each photo's crop window (5a P1)`.

## K1 (lane console): Scene editor › Order, no repeats and Crop limit; the face note

**Owned:**
- `central/console/src/SceneSettings.jsx`, `central/console/src/authoring.js`, `central/console/src/sceneFlowModel.js`, `central/console/src/SceneSteps.jsx`, `central/console/src/SceneFlow.jsx` (each only for these rows and Review)
- `central/console/src/domain/library-health-card.tsx`, `central/console/src/libraryApi.js` (the typedef), `central/console/src/domain/library.fixtures.ts`, `central/console/src/domain/library-connection.stories.tsx` (the note's story)
- `tests/browser/test_scene_flow_browser.py`; `tests/test_console_flow.py` (only if it pins the draft)
- The catalog baselines the new story writes; the console `dist` (rebuilt)

**Design:** "Text this corrects" (#43, #44, #48–#52 → one Crop limit row), `SceneSelection` in the TypeScript sketches; `.claude/skills/console-ux/SKILL.md` and `docs/design-language.md` for the controls; F-12.

**Frozen page.**
- **The draft** (`authoring.js` `SceneDraft`) gains `order` ("shuffle" | "chronological" | "newest_first" | "on_this_day"), `noRepeatDays` (integer 0–365) and `cropLimit` (a fraction, 0.05–0.60). The Scene sent on Save carries `selection: {order, no_repeat_days, crop_limit}`; a Scene read for Edit fills the draft from its `selection` (missing: `SCENE_DEFAULTS.selection`). `SCENE_SETTING_DEFAULTS` and `SETTING_RANGES` gain the three, equal to `central/wall_plan/selection.py` (pinned by a test in `tests/test_console_flow.py` or the existing defaults test).
- **The rows** (Playback step, each a `SettingRow` with its default and Reset, as 1x's rows): "Order" (a `SegmentedControl`: Shuffle, Oldest first, Newest first, On this day); "Don't repeat within" (days; 0 reads "Off"; help: "A photo shown recently waits its turn, but a Frame never goes blank because of it."); "Crop limit" (percent; help: "The most of a photo a Frame may cut away to fill itself. Faces and the interesting part are always kept."). Review lists all three in these words.
- **The face note**: `LibraryNote` gains `{kind: "no_face_detection"}`; `noteWords` answers "Immich's face detection is off, so crops ignore faces." for it (a switch over the three kinds, no else-arm that words an unknown kind as another).

**Acceptance:**
- `tests/browser/test_scene_flow_browser.py` (browser leg): create a Scene, set Order to Oldest first, Don't repeat within to 3 days and Crop limit to 30 %, Save; the stored Scene (`GET /v1/operator/runtime` or the route the flow reads) has `selection` {"order": "chronological", "no_repeat_days": 3, "crop_limit": 0.3}; Edit shows the three values and Review's rows; reopening an unchanged Scene shows no unsaved change; Reset puts each default back.
- The library connection story with the new note renders and passes the catalog leg (axe, screenshot).
- Console typecheck, lint and build pass.

**Mutation probes:**
- Send `crop_limit` as the percent number (30) → the stored-Scene assertion fails (and Central refuses it 422).
- Leave `selection` out of Edit's fill → the "no unsaved change" assertion fails.

**Land:** `feat(console): Scene order, no-repeat days and crop limit; the face detection note (5a K1)`.

## W1 (lane wall, after rules, media, order): the whole wall planned ahead

**Owned:**
- `central/wall_plan/assignment.py`, `central/planner.py`
- `central/coordination.py` (`advance`'s horizons and `CoordinationLimits`), `central/media_repository.py` (`source_candidates`), `central/app.py` (the candidates route's `crop_limit` parameter)
- `tests/test_planner_wall_assignment.py` (new); `tests/test_planner.py`, `tests/test_coordination.py`, `tests/test_authored_compatibility.py`, `tests/test_authored_media.py`, `tests/test_media_repository.py` (each only for the cases below and for codes this page renames)

**Design:** "Design rules" 1–3, "Contracts" (Order, Wall assignment), the walk paragraph under the contracts table, "Recommended as shown", R4, R6, R10, R11; Decisions above; F-8, F-10, F-11, F-13, F-14. Starts from `feat/photo-pool-on-frames` with R1, M1 and O1 merged.

**Frozen page.** Every F0 signature in `central/wall_plan/assignment.py` and the 5a surface of `central/planner.py`, exactly as frozen; bodies as their docstrings say, plus:
- **`plan_wall`** follows its module docstring's five steps exactly. "Previous photo" in step 5 is the Frame's latest placed or fixed photo before the slot (in this plan, or its kept still). The minimum-cost matching (Kuhn-Munkres) and the augmenting paths are hand-written in `assignment.py` (no new dependency); at most (slots at one instant) x (offers) per matching.
- **`eligible(candidate, profile, crop_limit)`** is `fit.shape_fit(candidate.kind, candidate.original_width, candidate.original_height, profile, crop_limit) is None`. `_variant_usable` keeps only its media-type, duration and :186 checks (no orientation test).
- **`project`**:
  1. `plan_end = now + (plan_seconds or horizon_seconds)` (bounded by `limits.max_horizon_seconds`); the timeline runs to `plan_end`; `PlanWindow(now, preparation_end=now + horizon_seconds, plan_end)`.
  2. Each media Intent becomes a `Slot` (its `selection.crop_limit`; `pool` = the contribution's Source refs joined; `authored` = its asset refs); black Intents become layers as today inside the preparation horizon; locks become `FixedShowing`s, their photo from `history.selected` by assignment id.
  3. One `SceneLine` per Scene with media slots: the union of its contributions' pools; `pickable` = its candidates measured and fitting at least one of its slots' profiles at that slot's crop limit; `ScenePass` from `scene_passes(history)`; `today` = the UTC date of `now`.
  4. `plan_wall(...)`; then: a `Placed` whose slot starts before `preparation_end` becomes a layer with `crop`; `AssignmentSelection(asset_id, scene_id, pass_number)` for each new placement (an authored one takes its Scene's current pass, 1 when the Scene has no line); `prepare` → `AcquisitionRequest`s; `measure` → `Projection.measurements`; `Unfilled` → diagnostics `no_eligible_candidates` (no_photo_fits), `no_photo_free`, `authored_no_eligible_alternative`; `preparation_pending` and `variant_incompatible` are retired; `Projection.plan` lists every slot.
- **`CoordinationLimits.plan_seconds`**: `Field(default=3600, gt=0, le=3600)`. `advance` passes `plan_seconds=self.limits.plan_seconds`, `max_clock_uncertainty=self.limits.max_uncertainty` and `PlannerLimits(max_horizon_seconds=max(self.limits.plan_seconds, self.limits.horizon_seconds + quantum))`.
- **The candidates route** (`GET /v1/operator/sources/{source_ref}/candidates?frame_id=&crop_limit=`): `crop_limit` optional, `MIN_CROP_LIMIT`–`MAX_CROP_LIMIT`, default `DEFAULT_CROP_LIMIT` (422 outside). With a Frame: `candidates` are those `judge` accepts (or, unmeasured, `shape_fit` passes), each with `standing` and `fit` ("fits" | "not_measured"); the answer gains `skipped: {<FitRefusal>: count}` with every `FitRefusal` key (0 included) plus `"not_measured": count` of the candidates listed as not measured. Without a Frame it is as today.

**Acceptance:**
- `tests/test_planner_wall_assignment.py` (unit):
  1. **The whole hour in one call:** two Frames (16:9 and 9:16), 30 s cycles: `project(plan_seconds=3600)` gives a `plan` slot for every cycle of both Frames for the hour.
  2. **Property (Hypothesis), across re-plans:** random pools, Frame shapes, fades, after-states, a kept still, locks from an earlier projection, and a pool refresh between projections: no photo has overlapping extents (`on_screen_extent`) on two Frames anywhere in the plan, fixed showings included; a re-plan never changes a locked layer.
  3. **Liveness:** a pool where every photo fits some Frame and no-repeat is 0: within enough cycles every photo is placed at least once.
  4. **Content first:** a pool of one photo and one Frame keeps the photo on that Frame every slot (repeat); two Frames and one photo: one Frame shows it, the other is `no_photo_free`, and the photo never sits on both.
  5. **Order, then least crop:** with two Frames (16:9 and 4:3) and two photos (16:9 and 4:3) ranked either way, each photo lands on the Frame it crops least.
  6. **Hand-placed:** an authored photo stays on its Frame; a Scene line on another Frame never takes it in the hour.
  7. **Preparation:** inside the preparation horizon only prepared photos become layers; the unprepared photo the line ranks first is requested (`AcquisitionRequest`) and a prepared one fills the slot; beyond it the plan is tentative.
  8. **Measurement:** unmeasured photos are never placed and are requested in rank order, at most `max_measurements`.
  9. **Crop on the layer:** a 2:3 photo with faces on its left on a 9:16 Frame becomes a layer with `crop.offset` 0.
- `tests/test_planner.py` (unit): existing cases pass with the renamed diagnostics; 50,000 candidates on 4 Frames plan an hour with no budget error.
- `tests/test_coordination.py` (db): through `Coordinator.advance` with seeded pools and key content, two Frames of one Scene of one shape show different photos; the offered layers carry crops; `photo_selections` holds their scene and pass.
- `tests/test_authored_compatibility.py`, `tests/test_authored_media.py` (db): the candidates route with a Frame lists only photos that fit or are unmeasured, with `skipped` counts by reason, at the given `crop_limit`; 0.9 answers 422.
- **Reported, not gated:** the wall time of `project` for 8 Frames, an hour, 10,000 measured photos, on the lane's machine, in the lane's report and the PR body.

**Mutation probes:**
- Check extents only within one instant (forget earlier picks' extents) → property case 2 fails.
- Skip the augmenting-path check (keep photos greedily per Frame) → case 5 or case 4 fails.
- Place unprepared photos inside the preparation horizon → case 7 fails.
- Fill an empty slot with nothing instead of the Frame's previous photo → case 4's repeat fails.

**Land:** `feat(planner): plan the whole wall an hour ahead, one photo per Frame, never on two at once (5a W1)`.

## K2 (lane reasons, after wall and console): why a photo is skipped on a Frame

**Owned:**
- `central/console/src/pages/frame-page.tsx`, `central/console/src/domain/frame-photo-fit.tsx` and `.stories.tsx` (new), `central/console/src/candidatesApi.js`, `central/console/src/useCandidates.js`, and the hand-placed chooser's call site (it passes the Scene draft's `cropLimit`)
- `tests/browser/test_frame_photo_fit_browser.py` (new); the catalog baselines its story writes; the console `dist` (rebuilt)

**Design:** "Data flow" (Skipped here and why), "Text this corrects" (#102 and the Frame page's "wrong orientation"); W1's candidates route.

**Frozen page.**
- `readCandidates(sourceRef, frameId, cropLimit?)` adds `&crop_limit=` when given and returns `{ready, candidates, skipped}`.
- The Frame page's Overview shows a "Photos" section for the Sources of the Scenes that target this Frame (from the snapshot's runtime document): "N photos fit this Frame", then one line per non-zero reason: "doesn't fit this Frame's shape" (`beyond_crop_limit`), "too small for this Frame" (`below_frame_pixels`), "a face would be cut" (`face_cut`), "the interesting part would be cut" (`interest_cut`), "this Frame shows no videos" (`no_video_here`), "not measured yet" (`not_measured`), each with its count. No orientation words.
- The hand-placed chooser passes the Scene draft's crop limit.

**Acceptance:**
- `tests/browser/test_frame_photo_fit_browser.py` (browser leg): a 16:9 Frame whose Scene's Source holds a 16:9 photo, a 2:3 photo and a 4:3 photo with interest in both bands, all measured (`PgKeyContent.record_measured`): the Frame page reads "1 photo fits this Frame", "doesn't fit this Frame's shape: 1" and "the interesting part would be cut: 1".
- The story renders and passes the catalog leg; console typecheck, lint and build pass.

**Mutation probes:**
- Drop `crop_limit` from the chooser's request → a chooser assertion on a 40 % Scene fails.
- Word `interest_cut` as the shape reason → the browser test's line fails.

**Land:** `feat(console): the Frame page says why photos are skipped there (5a K2)`.

## X1 (lane docs): the corrections and the module docs (DOCS page)

**Owned:** `docs/roadmap.md`, `docs/operator-console-design.md`, `docs/requirements.md`, `docs/module-planner.md`, `docs/design-decisions.md`, `docs/design-language.md`, `docs/execution-contract.md`, `docs/module-media-preparation.md`, `docs/module-media-worker.md`, `docs/module-media.md`, `docs/architecture.md`, `docs/player-architecture.md` (only the crop), `AGENTS.md` (the code map row for `central/wall_plan/`).

**Design:** "Text this corrects" (every row), "Requirements" (R1–R12 wording), "Costs and deferrals"; this ledger's Findings and Decisions.

**Frozen page.** Every change below states what 5a does in the owner's terms; each rule has one home, and the others link to it. Write against the pages above; INTEGRATE's I3 reads them against the landed code.
- **Roadmap** (`docs/roadmap.md`):
  - D3 "Photo fit": now "Every photo fills its Frame: smart crop keeps faces and the interesting part; a photo that would lose them does not go on that Frame. No mats, blur or pairs." Today column: "A centred crop" (the old "Nothing" was wrong). D4 "Order": shuffle (default), oldest first, newest first, On this day; no-repeat ranks only.
  - C5 Span and its "Deferred" row: deleted (owner, 2026-10-11: "Yes").
  - The delivery table's 5a row and the "5a. Photo fit and order" section: outcome, scope (D3, D4, #43, #44, the Crop limit row; #47–#52, #102 and #105 deleted), acceptance (the named tests of this ledger), risks (F-3, F-5, F-11), "moment it proves" ("two Frames of one shape never show the same photo; a portrait never shows as a strip on a landscape Frame"); the split line removed (one PR). The D13 gate row: "shown" means displayed and no-repeat only ranks (decided 2026-10-11); the rest of D13 stays open. The moments-of-truth row 4 rewritten to the fit rule.
  - Built and Not built for 5a (Not built: favourite weighting #45, keep events together #46, key content in videos, slow pan and zoom #60, a per-Frame crop limit, the plan reacting to failed draws). Settings counts updated. **No Developed row** (F-16).
- **Console design** (`docs/operator-console-design.md`): #43 and #44 as built; #47, #48, #49, #50, #51, #52, #102 and #105 deleted and replaced by one Crop limit row (35 %, 5–60 %); the Frame page's Photo fit tab and "wrong orientation" become K2's "Photos" reasons; the §12 row rewritten to the fit rule; "photo fit" in the stored-on-Frame list goes (nothing is stored per Frame).
- **Requirements** (`docs/requirements.md`): :185 becomes "A photo is assigned only to a Frame it fills with its key content kept; cropping that would cut a face or the interesting part is not an exception." Add: one photo per Frame; never the same photo on two Frames at once; "shown" means displayed; no-repeat never leaves a Frame without content. Each with the owner's words and date.
- **Module docs**: `module-planner.md` (the wall plan, the fit rule's home, order and passes, the extent, the hour horizon; :23 and :25 rewritten); `execution-contract.md` (`Layer.crop`, the on-screen extent and the guard); `module-media-preparation.md` (unchanged rule restated: preparation never crops; the 3,840 px edge vs a panorama, from the Decisions table); `module-media-worker.md` and `module-media.md` (the measure task, `photo_key_content`, the face note, F-17); `player-architecture.md` (the Player draws `Layer.crop`); `architecture.md` (the wall plan as a pure context); `design-decisions.md` D13 (as the roadmap); `design-language.md` (the D3 SegmentedControl rows go; the Crop limit percent field and the Order control); `AGENTS.md` code map: a row for `central/wall_plan/` (pure rules: key content, fit, selection, order, assignment).
- `python3 scripts/check_docs.py` is green.

**Land:** `docs: one photo per Frame, smart crop and the wall planned ahead (5a X1)`.

## INTEGRATE (the run, after every lane is merged): retire the additive shapes, the tracer, the seams

*Runs on `feat/photo-pool-on-frames` once every lane is merged. I1 lands first (the tracer), so I2's tripwire must keep it green; I3 reads the docs against the code as landed.*

**I1, the end-to-end tracer.** Owned: `tests/test_wall_plan_tracer.py` (new, unit leg).
- One Scene, two Frames (16:9 at 3840 x 2160 and 9:16 at 1080 x 1920), 30 s cycles, after-state "keep_this_photo"; a pool of a 16:9, a 4:3, a 2:3 and a 1:1 photo, the 2:3 one with faces on its left; every photo measured and prepared.
- Through the real `project` over an hour: every placed photo fits its Frame (`judge`); the 1:1 photo is never placed (44 % on 16:9 and on 9:16); no photo's extents overlap on the two Frames; the 2:3 photo on the 9:16 Frame carries offset 0, and the Player's scalar reference (`player.geometry.picture_source_uv` with that crop) samples only its left part.
- Then the same through `Coordinator.advance` (db): offered plans carry the crops; a "presented" observation writes shown history and the next advance ranks that photo after the others.
- Mutation probes: drop the extent check → the overlap assertion fails; centre every crop → the offset assertion fails.

**I2, retire the additive shapes (the tripwire).** Owned: every file naming the shapes below.
- `Layer.crop` becomes required on a media layer and absent on a black one (a `Layer` validator); `photo_rect`'s and `picture_source_uv`'s `crop` become required; the Player's `crop or CENTRED` goes.
- `eligible`'s `crop_limit` loses its default; every caller passes the Scene's (or the route's) limit.
- `project`'s `plan_seconds` default becomes the coordination value's (3600) and `history` stays an argument with no default.
- Every test building a media `Layer` passes a crop (a helper in `tests/support/` if more than two modules need one).
- Grep proof: `photo_rect(` without a crop, `crop=None` on a media layer, and "orientation" in `central/planner.py`, `central/wall_plan/`, `player/geometry.py` are empty.

**I3, docs read against the landed code.** Owned: X1's list. Each statement X1 wrote checked against the code as landed (task names, table names, route parameters, console words as K1 and K2 landed them, the measured planning time from W1). `python3 scripts/check_docs.py` green.

**Seams (each has a named assertion; the PR body lists where):**
- (a) **Crop.** W1 sets `Layer.crop` from R1's `judge`; P1 draws it through `crop_window` → I1.
- (b) **Extent.** W1's no-overlap uses R1's `on_screen_extent`; P1's executor uses `kept_from` → W1 case 2, P1's executor case.
- (c) **Key content.** M1 writes `photo_key_content`; `_hydrate_candidates` sets `Candidate.analysis`; W1 judges it → W1's coordination case, K2's browser test.
- (d) **Measurement requests.** W1's `Projection.measurements` → M1's scheduler line → `request_in` → the measure task → I1's coordination half (an unmeasured photo is measured and then placed, with a fake client).
- (e) **Shown history.** O1's hooks read W1's selections (scene, pass) → I1's coordination half.
- (f) **Scene selection.** K1 sends `selection`; Runtime carries it (O1); W1 reads its crop limit and order → K1's browser test plus W1 case 5.
- (g) **The candidates route.** W1's `crop_limit` and `skipped`; K2 reads them → K2's browser test.
- (h) **The face note.** M1 emits `no_face_detection`; K1 words it → M1's policy case plus K1's story.

**Land:** I1 `test(planner): the wall plan tracer, end to end (5a I1)`; I2 `refactor(planner): a media layer always carries its crop (5a I2)`; I3 `docs: 5a read against the landed code (5a I3)`.

## Outcome (filled by the run's closing step)

| Slice | Outcome | sha |
|---|---|---|
| F0 | landed | 18b981c, 4952b81, 1cdcdfa, 4503474 |
