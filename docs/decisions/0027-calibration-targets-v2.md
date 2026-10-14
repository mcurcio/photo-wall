# 0027 — Calibration targets v2: two OpenCV ChArUco boards, read for what one photo shows

**Date:** 2026-10-14 · **Layer:** system (the layout in `contracts/`, the Player's drawing, the printed sheet, the agent's reader under `scripts/`; no table, migration or wire change) · **Status:** built on the branch `feat/calibration-targets-v2` (one pull request). **Decision owner:** the owner, in chat on 2026-10-14 (his words below). Everything not quoted from him is a design choice he may reopen, not his ruling.

**Supersedes in part:** [0026](0026-calibration-targets.md): its layout, its sheet and its reader. Its two stages and what each aims at, the suggestions, the entry lookup (the QR names the sent picture entry) and the minute-long pattern stand as 0026 decided them.

## Problem

v1 ([#101](https://github.com/mcurcio/photo-wall/pull/101)) read a photo only when it met a list of conditions: the whole Frame in view, the uncut Letter or A4 sheet printed at 100 % in black ink, centred and square in a raw-black area the screen drew for it, a Frame opening of at least the screen target's size, the photo square-on. Each unmet condition was a refusal or an unflagged error. The owner read the instructions and asked for a target that is not so picky, and for one that scales to displays smaller than a sheet of paper.

## Requirements (bind)

| # | Requirement | Source |
|---|---|---|
| R1 | "Wow, that’s a ridiculous number of disclaimers. Start working on a v2 that isn’t so picky" | owner, chat, 2026-10-14 |
| R2 | "Some of my displays are smaller than a sheet of paper. This model simply doesn’t scale" | owner, chat, 2026-10-14 |
| R3 | "a “shadow over the whole picture” shouldn’t require a second picture. The point of using two calibration targets is that we are comparing them relatively." | owner, chat, 2026-10-14 |
| R4 | "Getting into the blackness of ink and the whiteness of paper is a level of nuance that probably doesn’t matter in practice" | owner, chat, 2026-10-14 |
| R5 | On the two stages' order: "thats the users choice -- not something that the software enforces" | owner, chat, 2026-10-13 |
| R6 | "i will put the calibration target paper under the glass as well so that at least they both get the same reflections"; "what if the reflection is only in the middle?" | owner, chat, 2026-10-13 |
| R7 | "the only aspect that matters is making sure that the target is in the same plane as the display, either with or without glass" | owner, chat, 2026-10-14 |
| R8 | "Make sure that you are using standard libraries and not building the algorithm" | owner, chat, 2026-10-14 |
| R9 | "I also expect hand rolled code that went in the previous PR (if any) to be replaced with standard libraries" | owner, chat, 2026-10-14 |

He also asked, on 2026-10-14, "My black ink is running low and printing gray-ish. What can we do?": nothing now assumes the ink is black. Design rules carried from 0026 and [AGENTS.md](../../AGENTS.md) (not his words): one home for the layout (`contracts/`), the Player is the only picture pipeline, the QR names the sent picture entry. The black card behind the paper is the main loop's suggestion and stays as a design choice.

## Decision

- **Both targets are OpenCV ChArUco boards** (`cv2.aruco.CharucoBoard`, `DICT_4X4_250`, marker share 0.75): a chessboard whose white squares each hold a marker unless the layout leaves it out, which the detector treats as a covered square. Each marker names its square, so any part of either board locates itself: the photo's framing, the piece cut, the print scale, the paper's place and turn never matter (R1, R2). The constants, which square is what (`square`, `grey_code`), the QR copies and their rulers (`qr_copies`, `ruler_steps`), how many columns an opening shows (`screen_columns`) and the code's text have one home, [`contracts/calibration_target.py`](../../contracts/calibration_target.py).
- **The rasters are drawn by OpenCV at build time.** The Pi and `contracts` never import OpenCV: [`scripts/calibration_boards.py`](../../scripts/calibration_boards.py) builds each board (`scripts/calibration_target/boards.py` `charuco_board`), draws it with `generateImage` at `SQUARE_PX` a square, paints the squares left out (whites, greys, rulers) and writes both into `contracts/calibration_boards.py` as stdlib data (`board_raster` reads it). A test regenerates both and compares the bytes.
- **The screen board** is `SCREEN_ROWS` (10) squares across the opening's short side and as many of its 25 columns as fit along the long side, so any opening holds it, a 5-inch display's included. The Player crops the raster to those columns, scales it into the opening and draws the QR modules into every copy (`player/geometry.py` `board_draw`, `board_point`, `board_signal`; the QR encoded by segno in `player/calibration_target.py` `target_modules`). Pre-software it is raw everywhere; post-software it runs through every Photo Wall step but the mat shadow, everywhere. No area is drawn raw for the paper.
- **The paper board** (17 × 13 squares) is printed by Central as a PDF (`central/displays/calibration_paper.py` `paper_pdf`, the Letter and A4 route unchanged) on any printer, in any ink, at any size; a cut piece serves a screen smaller than the sheet, as long as one of its markers is found (squares about 20 px or more in the photo). It lies on the screen, anywhere and at any turn, under the glass when there is one, with a black card behind it (R7).
- **The reader reads what is visible and says how sure** (`scripts/calibration_target/`, the agent's tool, never shipped). Every step is a library call (R8), except the rules no library models, each with its reason in `read.py`: each white over its own black neighbours, a spread across them marking a reflection edge (R6); the ink's level, measured once per photo against the screen blacks near the paper, so grey or colour ink reads like black, with the paper's own unevenness under reflections added to the white's spread (Findings); one radial lens falloff shared by both surfaces (fitted by `scipy.optimize.least_squares` with a robust loss); each paper white paired with the median of its nearest screen whites, copies beyond a physical tolerance dropped with their places; the near-black ruler's significance test against its own noise (PLUGE). Every drop is reported with where it was and why. The viewing angle is measured (`cv2.solvePnP` with the EXIF focal length, or `cv2.calibrateCamera` from the one view without it) and bounds the reading one-sided: a too-dim screen may be the panel's own off-axis dimming, so "raise" is said only beyond that bound, and otherwise the suggestion says unsure (take it squarer). The thresholds have one home, `scripts/calibration_target/model.py`.
- **One photo (R3)**, the two targets compared with each other. ProRAW reads white, tone and black; a normal photo (JPEG, HEIC, read through its own colour profile) reads the white alone.
- **The code** is `PWCT2 <player> <output> <picture change>`, repeated in QR copies every 8 columns, so a partial photo holds one. v1 sheets and photos stop reading: no compatibility.
- **The words shrink to three sentences**, whose one home is the Picture tab (`TARGET_HOW` in `central/console/src/domain/frame-picture.tsx`); the [runbook](../runbook.md#calibration-targets) and the [display-control skill](../../.claude/skills/display-control/SKILL.md#reading-the-calibration-targets) link it.

### The refusals that remain, each for a real failure

| Refusal | When | What would go wrong without it |
|---|---|---|
| `target-not-found` | under 2 screen markers or no paper marker found (the detail gives the squares' size when they are under about 20 px: come closer or print larger) | nothing locates that surface's squares |
| `paper-off-screen` | the paper's middle lies off the screen board | the paper misses the screen's plane and glass (R7) |
| `code-unreadable` | no QR copy decodes; the detail names the cause: the screen's squares under about 30 px in the photo (`QR_SQUARE_PX`, the code's need, above the boards' 20), or no whole copy clear of the paper | the stage and the settings on screen are unknown |
| `unknown-entry` | Central has no sent entry for the code | a guessed entry misleads |
| `not-a-target` | the entry's test pattern is no calibration target | the photo is of something else |
| `white-unread` | under 2 agreeing whites on a surface after the drops; says where and why | there is no white to compare |

A suggestion may still be refused (`lut-unread`, `tone-unread`) as in 0026. v1's refusals for a photo that cut part of a target off, a sheet not printed at 100 %, a sheet not laid where the screen expected it, and a clipped white are gone: a cut-off part, a scale, a place and a clipped square are now drops or nothing at all.

### Replaced from v1, as built

Everything hand-written that #101 added (R9), and what replaced it:

| v1 (#101) | What it did by hand | v2, as built |
|---|---|---|
| `contracts/calibration_target.py` `MARKER_BITS`, `_marker_white`, `_corner_markers`, `_quiet_zone` | ArUco bits typed in, modules and quiet zones placed by hand | `CharucoBoard.generateImage` in `scripts/calibration_boards.py`; `contracts` keeps constants and the rasters as data |
| same file: `_paired_groups`, `_ruler`, `calibration_target`, `screen_point`, `paper_ink`, the raw-black area under the paper, `PAIR_MM` and the millimetre sizes; `locate.py` `_paper_scale`, `_paper_misplaced`, `_outside` | a millimetre layout, a point rasteriser, v1's placement rules | gone: board squares by row and column (`square`, `grey_code`, `qr_copies`, `ruler_steps`) |
| `player/calibration_target.py` `target_texture`, `_texel_span`; `player/geometry.py` `target_point`, `target_signal` | texels rasterised from rectangles; the opening mapped to millimetres | the generated raster cropped and scaled: `board_draw`, `board_point`, `board_signal`; segno's QR kept (`target_modules`) |
| `central/displays/calibration_paper.py` `_edges`, `_module_lines`, `_dots` | markers rasterised into print dots | `paper_raster` scales the generated paper raster to the print DPI; Pillow's PDF writer kept (`paper_pdf`) |
| `locate.py` `_homography` (`findHomography(…, 0)`), `_markers_found` | plain least squares, no outlier rejection; the marker detector alone | `CharucoDetector` + `matchImagePoints` + `findHomography(USAC_MAGSAC)` (`find_board`, `locate`) |
| `locate.py` `_view`; `read.py` `_linear`; `suggest.py` `_decoded` | the sRGB curve written out by hand | colour-science's `cctf_encoding` and `cctf_decoding` (`detector_view`, `photos.py`, `suggest.py` `_decoded`) |
| `photos.py` `_normal` | the file's colour profile ignored (an iPhone JPEG or HEIC is Display P3) | Pillow ImageCms to sRGB first; rawpy and pillow-heif kept |
| `locate.py` `_qr_text` stretch | a percentile stretch | `cv2.normalize`; `QRCodeDetector` kept |
| `read.py` `_sample` | a polygon mask per patch; a hand MAD | `cv2.warpPerspective` into squares (`_rectified`); scipy's `median_abs_deviation` |
| `read.py` `_edges`, `_vote`, `_lowest_distinct` | domain rules | kept as the bespoke rules above (`_whites`, `_ink`, `_falloff`, `_vote`, `_lowest_distinct`) |
| `suggest.py` `_gamma_fit`, `_Display.fitted` | grid searches over exponents | `scipy.optimize.minimize_scalar` (bounded) |
| `suggest.py` the knob search | coordinate descent over whole-step knobs | kept (`_searched`): whole steps with a cost per step from neutral; no library integer search fits |
| `suggest.py` `_kelvin_of` | nearest point in a fine table | kept: it inverts Photo Wall's own kelvin table (`contracts/kelvin.py`); a library's colour temperature is a different scale |

Kept from before #101: `contracts.node_frame.srgb_to_linear`, `player/geometry.encode_srgb` and the shader's curve are the Pi's own copies, which cannot import a library; a test holds them equal to colour-science. The synthetic scenes (`tests/calibration_target_scenes.py`) use colour-science's curves too.

## Alternatives considered

**Design it twice.** Chosen (A): two OpenCV ChArUco boards. Fits any display; needs 2 screen markers and a QR copy in view; the paper anywhere on the screen at any turn, size or ink. Gives up v1's fixed pairs across the box edge (now formed by nearness, which needs the falloff fit), about 30 % of the screen for QR copies, and adds colour-science and scipy to the reader (never shipped). Rejected (B): v1 drawn to each opening's size with the paper printed to match: a sheet at an odd scale per display, centred in its raw-black area, black ink, all four corners in view, so every relaxation R1 asks for except size.

Prior art: OpenCV's ChArUco board, detector and interpolation; USAC/MAGSAC; IPPE and Zhang's calibration through `calibrateCamera`; colour-science; LittleCMS through Pillow; the DNG standard's FixVignetteRadial model; PLUGE (ITU-R BT.814).

## Costs

- **Unchanged from 0026:** the camera is not the eye; a shadow over the paper alone is unseen (R3), so stand aside of the light; panel unevenness far from the paper is unseen.
- **Uneven reflections cost accuracy through the ink measurement** (Findings below).
- **The angle is bounded, not corrected:** a too-dim screen photographed off axis reads "unsure" rather than "raise"; the bound's size is a placeholder.
- **Normal photos give a rough white only** (the synthetic phone JPEGs: up to 18 % off).
- **The code needs about 30 px a square in the photo** to decode, so a small display is photographed close.
- **About 30 % of the screen board is QR copies and rulers.** The reader gains colour-science and scipy (the agent's tool only). The Player changes, so a Pi release follows; v1 sheets and photos stop reading.

## Deferred

Correcting for the angle; automatic Apply; colour of the primaries; checking that the paper lies flat (a tilted sheet is not caught); a vote over the greys' copies for the tone (two heavy-reflection synthetic photos read the exponent far off).

## NOT RUN

Bench steps, after a release with it is on the test Pi; CI reads synthetic photos only:

- a real ProRAW photo read end to end (and so whether Apple applies its own lens-shading correction);
- the angle bound's size (`PANEL_DIMMING_AT_35`, a placeholder for the Samsung QN43Q8F's off-axis loss);
- a colour printer's composite black (simulated as grey ink only), and light through the paper without the card;
- lens distortion;
- the screen board on a low-resolution small panel;
- the near-black ruler on small squares (under about 40 px a square in the photo the camera's blur lifts its raw-0 bars; every ruler case is a 43-inch Frame);
- `paper-off-screen` on a real photo.

## Findings

Recorded by the slices in `.claude/errata.md` (calibration targets v2); each is for the owner or the bench, not a change made here:

- **The ink's level takes a reflection the paper and its nearest screen blacks do not share as ink**, and the white follows it. On the committed synthetic scenes with uneven reflections, 9 of 32 uncut-sheet photos and 7 of 22 piece readings are over 3.5 % off (worst 44 %); one soft blob in the middle of the sheet ("what if the reflection is only in the middle?") gives 7.6 %. The design's proof table (worst 3.1 %) came from scenes where no reflection edge ran along the paper's border. The photo cannot tell such a reflection from greyer ink: the ink is the one unknown the screen must supply, and its blacks lie a card's width and up to a screen square away. As built (a stop-gap, not the fix): the range of the paper's own black levels, over its white above them, is added to the white's spread, so every reading over 3.5 % off in the tested population (`heavy`, 16 seeds, black and grey ink) states a spread over 3.5 % rather than about 1 %; a reflection lying evenly over the paper alone is still unseen. The estimator itself is an architecture choice for the owner (for example a reference that shares the paper's reflection, or the paper's ink taken from the print look's black floor).
- **"Paper anywhere" reads 18 of 24 synthetic photos, not 20 of 20:** the other six hold no whole QR copy clear of the paper (copies sit at fixed places, 4 on a 17-column board); each is refused `code-unreadable` naming that cause. More copies would cost more of the screen.
- **The angle bound makes a 10 % dim screen "unsure" in an ordinary photo.** The angle is the median over every screen white read, and a 1x phone photo of a 43-inch Frame sees those whites 20–24° off axis, where the placeholder bound is 9–12 %. Below about a 10 % shortfall, "raise" needs a squarer photo or the 2x lens until the bench sizes the bound.
- **The code reads from about 30 px a square, not 12:** a QR copy is 3 squares for about 41 modules.
