# 0026 — Calibration targets: a screen target and a paper target, read by the agent from one or two photos

**Date:** 2026-10-13 · **Layer:** system (a test pattern pair drawn by the Player, a printable sheet from Central, one more read of the picture history, and the agent's reader under `scripts/`; no table, job or worker) · **Status:** accepted design, built on the branch `feat/calibration-targets` (one pull request, after [#100](https://github.com/mcurcio/photo-wall/pull/100)). **Decision owner:** the owner, in chat on 2026-10-13 (his words below). The design's two questions (Q1, Q2) were taken as recommended when he said "build the calibration targets"; they and everything else not quoted from him are design choices he may reopen, not his rulings.

**Builds on:** [0022](0022-picture-hardware-and-software-apart.md) (the Display's two picture groups, the session, Done, Revert), [0023](0023-print-look.md) (the print look and the picture pipeline's order), [0024](0024-display-tuning.md) (the signal range, test patterns, the paper-sheet test) and [0025](0025-configuration-snapshots.md) (the sent picture entries a photo is matched against).

## Problem

Tuning a Display against a sheet of white paper ([0024](0024-display-tuning.md)'s White) was judged by eye from phone photos sent to the agent, with nothing in the photo that said what the wall was sent, where the paper was or how much of what the camera saw was the room's reflection. The owner proposed a pair of targets, one drawn by the screen and one printed, carrying a code, so the agent can read a photo of both. His Samsung QN43Q8F sits behind reflective glass, and its own controls and Photo Wall's settings can work against each other.

## Requirements (bind)

| # | Requirement | Source |
|---|---|---|
| R1 | "what if the calibration printed a black and white QR code onto the display and then used the black and white within the known QR space to measure? we could do the same with a sheet of paper -- photo-wall could give me a printable asset and i could tape that to the screen" (R5 moves the paper under the glass) | owner, chat, 2026-10-13 |
| R2 | "build the calibration targets" | owner, chat, 2026-10-13 |
| R3 | "1 is probably harder to do in a wholly automated fashion because the algorithm would have to deal with noisy pictures. its easier for you (an LLM) to deal with bad picture quality" | owner, chat, 2026-10-13 (on [0025](0025-configuration-snapshots.md)'s options) |
| R4 | "i use an iphone 14 pro"; on Apple ProRAW: "i turned it on." | owner, chat, 2026-10-13 |
| R5 | "dont forget that the TV is reflective, so its possible that light and shadows will reflect over the calibration targets that display on the TV. the algorithm needs to be prepared to deal with that. i will put the calibration target paper under the glass as well so that at least they both get the same reflections." | owner, chat, 2026-10-13 |
| R6 | "\"read from the middle\" doesnt magically solve reflections. what if the reflection is only in the middle?" | owner, chat, 2026-10-13 |
| R7 | "there is value in getting the display itself as close to the calibration target as possible independently of the software adjustments, else we risk having the TV lower some property that the software then has to boost (or vice versa). I think the calibration has two stages: pre-software-adjust and post-software-adjust. The best possible display calibration is one that doesnt require any software adjustments." | owner, chat, 2026-10-13 |
| R8 | On the two stages' order: "thats the users choice -- not something that the software enforces" | owner, chat, 2026-10-13 |
| R9 | Picture settings are the Display's; a test pattern is the session's and never stored; the black floor and the rest of the print look are the intended look; the Player is the only picture pipeline | [0022](0022-picture-hardware-and-software-apart.md), [0023](0023-print-look.md), [0024](0024-display-tuning.md), [AGENTS.md](../../AGENTS.md) (design rules, not words of this request) |

The readings that turned R5 to R8 into rules (the reflection plan, the two stages' aims, that the print look is not calibration) are the main loop's, recorded in the design and below as design choices. R5's "same reflections" holds for the Frame glass only: the TV's own surface reflects onto the screen and not onto the paper beneath the glass, which is why each surface is read against its own black (rule 1).

## Decision

- **Two targets, one layout.** The **screen target** is a test pattern the Player draws at its physical size, centred in each Frame's opening; the **paper target** is a sheet Central prints, laid uncut on the panel under the Frame's glass with a black card behind it, inside the screen target's **dark box** (raw black, so only the panel's black glow can reach the paper). Both carry ArUco markers at their corners; the screen target carries a QR naming what it was sent. The layout, its sizes, the markers, the groups and the code's text have one home, [`contracts/calibration_target.py`](../../contracts/calibration_target.py); the PDF is drawn from it (`central/displays/calibration_paper.py`, `GET /v1/operator/calibration-target/paper.pdf?paper=letter|a4`), and the Player's reference from it too (`player/geometry.py` `target_signal`).
- **Two stages, two test patterns, either in any order (R7, R8).** `contracts/tuning.py` `TestPattern` gains `target-pre-software` (the screen target drawn raw: the display's own controls are judged) and `target-post-software` (drawn through Photo Wall's settings: the residual Photo Wall should cover). Nothing remembers an earlier reading, gates one stage on the other or resets anything; the stage of a photo is the test pattern of the entry its QR names. A target is a test pattern like the others: the session's, live while the Picture tab runs, never stored, off by Done, Revert, leaving the tab or the Pi's 10-minute bound.
- **The photo names its entry.** The QR's text is the HDMI port's key in the picture history: the Pi, the port and the port's `picture_change` (`TargetCode`). The reader asks Central for that sent entry (`GET /v1/operator/players/{player_id}/outputs/{output_id}/picture-changes/{picture_change}`, `TuningHistoryQueries.sent_picture_entry`, answering 404 `unknown_entry`), so the settings on screen come from [0025](0025-configuration-snapshots.md)'s one record, with no second record and no match by time. The QR is encoded on the Pi from its own player id and the operations' picture change; no wire field carries it.
- **Each surface against itself (design rule 1; R5, R6).** Reflections only add light and are uneven. The layout is local groups round the dark box, each a screen black, white and grey within a short distance of a paper black and white, so a reflection larger than a pair adds the same light to both; each patch is read above its own surface's black in its own group. A group whose blacks disagree with the rest has a reflection edge across it and is dropped, wherever it is: the centre is not trusted more than the edges (R6; sampling a patch's middle only avoids edge blur). Each value has copies in several groups; copies that disagree are dropped, never averaged, and every drop is reported with where it was and why. With a second photo, each patch takes the darker reading. Too many groups dropped is a refusal saying where to move or what to retake, never a suggestion. A reflection perfectly even over the whole target is undetectable and harmless: both surfaces and the viewer see it.
- **Only the paper's white is matched.** The paper is read for its bare white alone; the screen's greys are read against the screen white of their own group and judged against the standard sRGB curve. Printed greys would build the print look into the display, which the look would then apply again.
- **What each stage aims at (R7).** The print look ([0023](0023-print-look.md): the LUT, saturation, the highlight rolloff, the black floor) is the intended look, not calibration: neither stage moves it.

| Property | Pre-software: the display's own controls | Post-software: Photo Wall's settings, the look on |
|---|---|---|
| White level and colour | screen white equals paper white | the same, through the chain |
| Tone | the screen greys over the screen white follow sRGB | the greys follow sRGB of the chain's output with neutral brightness, contrast, colour temperature and gamma and the look as set (`final_pixel`) |
| Black | the lowest near-black step apart from raw 0 should be 1 (after PLUGE, as [0024](0024-display-tuning.md)'s Black level) | not judged: the black floor is the look's |
| Colour of the primaries | not read (the camera's crosstalk) | not moved: saturation is the look's |

- **Suggestions.** Pre-software: a direction and a gap size per property (raise, lower, warmer, cooler or keep, in percent, kelvin, gamma or steps), never a menu value (Q2). Post-software: a software picture in which only brightness, contrast, colour temperature and gamma move, each away from neutral only when that improves the fit by more than its readings' spread, so a display right on its own yields "no change" (R7's best case). Suggestions are for the operator; nothing is applied by the agent or the reader (no automatic Apply).
- **The reader is the agent's tool, not a Central feature (R3).** `scripts/calibration_target/` (`python -m scripts.calibration_target PHOTO [PHOTO] --central URL`) decodes Apple ProRAW DNG linear (rawpy, LibRaw) and normal photos (white only), finds the markers and QR (OpenCV), reads the entry, and prints one JSON object: a reading or a refusal, and with a reading a suggestion. Its core does no I/O; its thresholds have one home (`scripts/calibration_target/model.py`). It is never shipped. How to run it is the [runbook](../runbook.md#calibration-targets); the agent's procedure is the [display-control skill](../../.claude/skills/display-control/SKILL.md#reading-the-calibration-targets); the tab's words are `PATTERN_HOW` in `central/console/src/domain/frame-picture.tsx`, described by the [console design §5](../operator-console-design.md#5-the-frame-page).
- **Schema.** Migration 097 drops and re-adds the two `test_pattern` CHECKs (`picture_sessions`, 094; `picture_sent_entries`, 096) with the five values; no row changes, so it alone changes no picture. The display line's schema major becomes 7 and picture operations v7 (two more `test_pattern` values; the codec and the Wayland messages are unchanged).

### The order in the picture pipeline

Steps as in [0023](0023-print-look.md) and [0024](0024-display-tuning.md):

| # | Step | Target: pre-software | Target: post-software |
|---|---|---|---|
| 0 | The target replaces the photos inside each opening | yes | yes |
| 1–6 | LUT, saturation, white balance × brightness, highlight rolloff, encode, contrast, gamma, black floor | skipped (raw) | applied, except inside the dark box (raw black) |
| 7 | Mat shadow | skipped | skipped (a place effect at the opening's edge, not a setting the target judges) |
| 8 | Black outside every opening | yes | yes |
| 9 | Signal range (Limited scales 0–255 into 16–235) | applies | applies |

## Alternatives considered

**Design it twice (the measurement).** Chosen (A): paper white only, the screen's own greys against sRGB. Needs bare paper and black ink; fails on a shadow over the whole paper in one photo; gives up colour and checking the look against the paper. Rejected (B, the design's previous revision): every printed patch matched to its screen twin. Needs known reflectances (a print contract, as ColorChecker's); fails silently on the TV surface's glare and on a spot over one patch, and builds the print look into the display.

**Q1 · One photo or two?** A shadow over the whole paper (the owner's own) multiplies only the paper and is invisible in one photo. Recommended: two, from either side of the Frame (a second photo each round, a retake when they disagree); the alternative was one, leaving that shadow unseen. Taken as recommended under "build the calibration targets", not his ruling, and built as one or two: the reflection plan (the main loop's reading of R5 and R6, item 4) made the second photo optional, so the reader takes one, and two whose whites disagree are refused (`photos-disagree`). The runbook and the skill ask for two.

**Q2 · Record the TV's own menu values?** Recommended and built: no; pre-software suggestions stay directions, and the owner says in chat what he changed. Rejected: a note per Display in its history (a field, a migration, values typed in). Taken as recommended, not his ruling.

Also rejected: reading from the middle of the target (R6); a hand-made bit strip for the code (the QR was his idea, R1, and has Reed–Solomon recovery); matching in the console (R3, deferred: the agent reads).

Prior art: professional display calibration (the display's own controls first, then a correction for the residual); PLUGE (ITU-R BT.814); ArUco markers and QR codes through OpenCV's detectors; the median of agreeing copies with outliers dropped; ColorChecker's known reflectances (what B would need).

## Costs

- **The camera is not the eye**, most on a quantum-dot panel like the QN43Q8F: a white the camera matches may not look matched. The first real-photo check measures the gap.
- **A shadow over the whole paper** reads the screen as too bright by its depth (15 % → about +18 % in the prototype) when only one photo is sent.
- **Opening the Frame** each session to lay the paper under the glass; an opening smaller than 38.5 × 34 cm, either way round, cannot hold the screen target. Print at 100 % on the paper the look imitates, black ink only.
- **Normal photos** (JPEG, HEIC) give the white alone (about 4 % off), no tone and no black. Panel unevenness far from the paper is unseen.
- **Two more test patterns** in the Player's reference, its shader and the pixel harness; a QR encoder on the Pi (`python3-segno`); a Pi release.
- **Deferred:** automatic Apply; colour (the primaries); a Picture-tab reading. **Not planned:** a camera on the Pi.
- **NOT RUN (bench steps after deploy):** a real ProRAW photo read end to end; the screen target's drawn size on the test Pi; post-software suggestions against real readings; the Samsung's camera-versus-eye gap. CI reads synthetic photos of the Player's reference target and the layout's paper only.

## As built

The interfaces depart from the design text in these points, each recorded in `.claude/errata.md` (calibration targets) and each a design choice, not the owner's ruling:

- The reader lives at `scripts/calibration_target/`, not a top-level package; its no-I/O core is held by a pytest import check (`tests/test_calibration_target_reader_imports.py`) rather than import-linter, which lists first-party packages only. `scripts/release_plan.py` declares it unshipped.
- The design's `readPhotos` is split: `locate(photo)` (markers, QR, scale) and `read_photos(located, entry)`; the command fetches the entry between them, so the pure core never holds Central's port.
- A post-software suggestion with a LUT on needs the LUT's table: the reader reads its paper profile by read-only SQL (`PHOTO_WALL_READ_DATABASE_URL`) and bakes it as Central does; no operator route serves a LUT's bytes and none is added for a dev tool. Without it the suggestion refuses `lut-unread` rather than predict without the look.
- Vocabulary added: the drop reason `reflection-edge` (a group whose blacks disagree) and the refusal `different-entries` (two photos whose codes differ).
- The post-software target skips the mat shadow as well as drawing the dark box raw (the pipeline table above).
- The display line's schema major becomes 7 and picture operations v7.
- The QR is encoded on the Player (segno; Debian trixie's `python3-segno`, pinned the same in the dev group) from its own player id and the operations' picture change; no wire field carries it.
- The design's tracer needs a real ProRAW photo, which CI cannot take: the CI tracer reads a synthetic photo of the Player's reference target and the layout's paper end to end; the real photo stays the bench step (NOT RUN).
- Q1 as built: the second photo is optional (the reflection plan's item 4), not required as recommended.

The slices then departed from the interfaces in these points (`.claude/errata.md`, the console, reader and Player lanes), again design choices:

- **The layout's groups.** Sixteen paired groups round the dark box, numbered clockwise from the top-left (top 0–4, right 5–7, bottom 8–12, left 13–15), then two near-black ruler groups (16 above the top row, 17 below the bottom row). A screen black is paired with its group's paper black; a screen white and grey with its paper white.
- **Rule 2, the reflection edge.** A paired group is dropped when its screen black less its paper black differs, either way, from that difference's median over the groups by more than `BLACK_EDGE_TOLERANCE` of the screen white; a ruler group (no paper black) compares its screen blacks with every group's median screen black. The interface text's "a black above the median of its surface's blacks" would drop groups under a broad reflection that lifts both blacks alike (harmless, item 1) and fails once a reflection covers more than half the groups. This is the owner's plan item 2 ("a black-to-black difference inside a group"), which binds.
- **Rule 3, copies.** Each copy is judged against the median of all its copies, not the median of the rest (with three copies and one outlier, that would drop a good copy).
- **Rule 5, the darker reading** is taken on each patch's raw light, blacks included, the second photo scaled to the first by the median ratio of the groups' white above black; each patch is then read above its own black, so a reflection landing on a black is not kept.
- **The suggestion's measures.** The white's colour gap is read as a Photo Wall colour temperature against 6500 K (the kelvin difference under 6500 K light, the same step in mireds otherwise); the tone's gap is 2.2 (q − 1) for the exponent q the greys best follow, as Photo Wall's own gamma acts. Post-software, each of the four knobs away from neutral costs `KEEP_SPREADS` squared in the fit's units rather than a penalty per step (which would stop a too-bright display short of the paper); the aim's white includes the look's own tint, so the colour temperature never fights a LUT. A display warmer than software can correct gets a near-neutral fit: that is pre-software work.
- **The console.** With six choices the Test pattern is a Select, not a SegmentedControl; the targets are named "Target: pre-software" and "Target: post-software", one home (`PATTERN_NAMES` in `frame-picture.tsx`) for the tab and the Picture History. The print links are plain links to the PDF route, which answers as a download.
- **The Player's proof.** The pixel harness (`node-pid1.yml`, 640 × 480) is too small to decode the drawn QR, so it checks the drawn modules against the code exactly; the decode itself is proven on the Player's module grid with OpenCV.

## Findings

- **The paper's ink biases every white-level suggestion** (reader lane R2; a gap in this design, not the owner's ruling). Rule 1 reads each patch above its own surface's black, and the paper's black is printed ink, which reflects some of the room's light (matte black about 2–5 % of the paper's white). A display whose white equals the bare paper's therefore reads about the ink's share too bright: pre-software says "lower about 3 %", post-software dims brightness about 3 steps, with 3 % ink. Built as specified; the tests define "right" the same way. The fix is a choice to make after the first real photo measures the ink: a nominal ink correction (one more threshold), the look's black floor as the ink's estimate, or a measured reference. Until then the agent treats a white-level gap of about 3 % or less as within the ink's error.
