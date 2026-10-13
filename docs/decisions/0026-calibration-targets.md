# 0026 — Calibration targets: a screen target and a paper target, read by the agent from one photo

**Date:** 2026-10-13 · **Layer:** system (a test pattern pair drawn by the Player, a printable sheet from Central, one more read of the picture history, and the agent's reader under `scripts/`; no table, job or worker) · **Status:** accepted design, built on the branch `feat/calibration-targets` (one pull request, after [#100](https://github.com/mcurcio/photo-wall/pull/100)). **Decision owner:** the owner, in chat on 2026-10-13 (his words below). The design's two questions (Q1, Q2) were taken as recommended when he said "build the calibration targets"; on 2026-10-14 he answered Q1 himself (one photo) and ruled out an ink correction (both below). Everything else not quoted from him is a design choice he may reopen, not his ruling.

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
| R10 | "a “shadow over the whole picture” shouldn’t require a second picture. The point of using two calibration targets is that we are comparing them relatively." | owner, chat, 2026-10-14 (on [#101](https://github.com/mcurcio/photo-wall/pull/101)) |
| R11 | "Getting into the blackness of ink and the whiteness of paper is a level of nuance that probably doesn’t matter in practice. If we are at that level of precision, only professional level gear is going to move the needle" | owner, chat, 2026-10-14 (on #101) |
| R12 | "It would be nice if the testing screen persisted, even for a minute. The current timing is so tight that switching from my phone browser to my phone camera is enough to drop the white overlay" | owner, chat, 2026-10-14 (on #101) |

The readings that turned R5 to R8 into rules (the reflection plan, the two stages' aims, that the print look is not calibration) are the main loop's, recorded in the design and below as design choices. R5's "same reflections" holds for the Frame glass only: the TV's own surface reflects onto the screen and not onto the paper beneath the glass, which is why each surface is read against its own black (rule 1).

## Decision

- **Two targets, one layout.** The **screen target** is a test pattern the Player draws at its physical size, centred in each Frame's opening; the **paper target** is a sheet Central prints, laid uncut on the panel under the Frame's glass with a black card behind it, inside the screen target's **dark box** (raw black, so only the panel's black glow can reach the paper). Both carry ArUco markers at their corners; the screen target carries a QR naming what it was sent. The layout, its sizes, the markers, the groups and the code's text have one home, [`contracts/calibration_target.py`](../../contracts/calibration_target.py); the PDF is drawn from it (`central/displays/calibration_paper.py`, `GET /v1/operator/calibration-target/paper.pdf?paper=letter|a4`), and the Player's reference from it too (`player/geometry.py` `target_signal`).
- **Two stages, two test patterns, either in any order (R7, R8).** `contracts/tuning.py` `TestPattern` gains `target-pre-software` (the screen target drawn raw: the display's own controls are judged) and `target-post-software` (drawn through Photo Wall's settings: the residual Photo Wall should cover). Nothing remembers an earlier reading, gates one stage on the other or resets anything; the stage of a photo is the test pattern of the entry its QR names. A target is a test pattern like the others: the session's, live while the Picture tab runs, never stored, off by Done, Revert, leaving the tab or the Pi's 10-minute bound; hiding the browser tab (the phone switching to its camera) keeps it up a minute (R12; the session's rule is [0024](0024-display-tuning.md)'s).
- **The photo names its entry.** The QR's text is the HDMI port's key in the picture history: the Pi, the port and the port's `picture_change` (`TargetCode`). The reader asks Central for that sent entry (`GET /v1/operator/players/{player_id}/outputs/{output_id}/picture-changes/{picture_change}`, `TuningHistoryQueries.sent_picture_entry`, answering 404 `unknown_entry`), so the settings on screen come from [0025](0025-configuration-snapshots.md)'s one record, with no second record and no match by time. The QR is encoded on the Pi from its own player id and the operations' picture change; no wire field carries it.
- **Each surface against itself (design rule 1; R5, R6).** Reflections only add light and are uneven. The layout is local groups round the dark box, each a screen black, white and grey within a short distance of a paper black and white, so a reflection larger than a pair adds the same light to both; each patch is read above its own surface's black in its own group. A group whose blacks disagree with the rest has a reflection edge across it and is dropped, wherever it is: the centre is not trusted more than the edges (R6; sampling a patch's middle only avoids edge blur). Each value has copies in several groups; copies that disagree are dropped, never averaged, and every drop is reported with where it was and why. Too many groups dropped is a refusal saying where to move or what to retake, never a suggestion. A reflection perfectly even over the whole target is undetectable and harmless: both surfaces and the viewer see it.
- **One photo (R10).** The two targets in one photo are compared with each other; the reader reads one photo and never asks for a second.
- **Only the paper's white is matched**, as read, with no correction for the paper's ink (R11). The paper is read for its white alone; the screen's greys are read against the screen white of their own group and judged against the standard sRGB curve. Printed greys would build the print look into the display, which the look would then apply again.
- **What each stage aims at (R7).** The print look ([0023](0023-print-look.md): the LUT, saturation, the highlight rolloff, the black floor) is the intended look, not calibration: neither stage moves it.

| Property | Pre-software: the display's own controls | Post-software: Photo Wall's settings, the look on |
|---|---|---|
| White level and colour | screen white equals paper white | the same, through the chain |
| Tone | the screen greys over the screen white follow sRGB | the greys follow sRGB of the chain's output with neutral brightness, contrast, colour temperature and gamma and the look as set (`final_pixel`) |
| Black | the lowest near-black step apart from raw 0 should be 1 (after PLUGE, as [0024](0024-display-tuning.md)'s Black level) | not judged: the black floor is the look's |
| Colour of the primaries | not read (the camera's crosstalk) | not moved: saturation is the look's |

- **Suggestions.** Pre-software: a direction and a gap size per property (raise, lower, warmer, cooler or keep, in percent, kelvin, gamma or steps), never a menu value (Q2). Post-software: a software picture in which only brightness, contrast, colour temperature and gamma move, each away from neutral only when that improves the fit by more than its readings' spread, so a display right on its own yields "no change" (R7's best case). Suggestions are for the operator; nothing is applied by the agent or the reader (no automatic Apply).
- **The reader is the agent's tool, not a Central feature (R3).** `scripts/calibration_target/` (`python -m scripts.calibration_target PHOTO --central URL`) decodes Apple ProRAW DNG linear (rawpy, LibRaw) and normal photos (white only), finds the markers and QR (OpenCV), reads the entry, and prints one JSON object: a reading or a refusal, and with a reading a suggestion. Its core does no I/O; its thresholds have one home (`scripts/calibration_target/model.py`). It is never shipped. How to run it is the [runbook](../runbook.md#calibration-targets); the agent's procedure is the [display-control skill](../../.claude/skills/display-control/SKILL.md#reading-the-calibration-targets); the tab's words are `PATTERN_HOW` in `central/console/src/domain/frame-picture.tsx`, described by the [console design §5](../operator-console-design.md#5-the-frame-page).
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

**Design it twice (the measurement).** Chosen (A): paper white only, the screen's own greys against sRGB. Needs bare paper and black ink; fails on a shadow over the whole paper alone; gives up colour and checking the look against the paper. Rejected (B, the design's previous revision): every printed patch matched to its screen twin. Needs known reflectances (a print contract, as ColorChecker's); fails silently on the TV surface's glare and on a spot over one patch, and builds the print look into the display.

**Q1 · One photo or two?** Recommended in the design: two, from either side of the Frame, to catch a shadow over the whole paper; first built as one or two, two whose whites disagreed refused. **The owner's answer (R10, 2026-10-14): one.** The reader takes one photo; the second-photo path (the darker reading per patch, `photos-disagree`, `different-entries`) is gone, and the shadow is a stated cost (below), not a refusal.

**Q2 · Record the TV's own menu values?** Recommended and built: no; pre-software suggestions stay directions, and the owner says in chat what he changed. Rejected: a note per Display in its history (a field, a migration, values typed in). Taken as recommended, not his ruling.

Also rejected: reading from the middle of the target (R6); a hand-made bit strip for the code (the QR was his idea, R1, and has Reed–Solomon recovery); matching in the console (R3, deferred: the agent reads).

Prior art: professional display calibration (the display's own controls first, then a correction for the residual); PLUGE (ITU-R BT.814); ArUco markers and QR codes through OpenCV's detectors; the median of agreeing copies with outliers dropped; ColorChecker's known reflectances (what B would need).

## Costs

- **The camera is not the eye**, most on a quantum-dot panel like the QN43Q8F: a white the camera matches may not look matched. The first real-photo check measures the gap.
- **A shadow over the whole paper.** The photographer standing between the room light and the paper darkens the paper only (the screen emits its own light), so the screen reads too bright by the shadow's depth (15 % → about +18 % in the synthetic photos); stand aside of the light. Nothing detects it.
- **The paper's ink is not corrected for (R11).** The paper's white is read above its printed black, which reflects a little of the room's light (matte black about 2–5 % of the white), so a display matched to the bare paper reads that much too bright. The owner judged that below what matters without professional gear.
- **Opening the Frame** each session to lay the paper under the glass; an opening smaller than 38.5 × 34 cm, either way round, cannot hold the screen target. Print at 100 % on the paper the look imitates, black ink only.
- **Normal photos** (JPEG, HEIC) give the white alone (about 4 % off), no tone and no black, and no post-software suggestion. Panel unevenness far from the paper is unseen.
- **The viewing angle.** A panel's light changes with the angle it is seen from (most on a VA panel, as the QN43Q8F's likely is) while the paper's does not, so a photo off square-on reads the white and the greys off. The photo is taken square-on. The synthetic photos have no angle term: the bench check measures it.
- **Two more test patterns** in the Player's reference, its shader and the pixel harness; a QR encoder on the Pi (`python3-segno`); a Pi release.
- **Deferred:** automatic Apply; colour (the primaries); a Picture-tab reading. **Not planned:** a camera on the Pi.
- **NOT RUN (bench steps after deploy):** a real ProRAW photo read end to end; the screen target's drawn size on the test Pi; post-software suggestions against real readings; the Samsung's camera-versus-eye gap. CI reads synthetic photos of the Player's reference target and the layout's paper only.

## As built

The interfaces depart from the design text in these points, each recorded in `.claude/errata.md` (calibration targets) and each a design choice, not the owner's ruling:

- The reader lives at `scripts/calibration_target/`, not a top-level package; its no-I/O core is held by a pytest import check (`tests/test_calibration_target_reader_imports.py`) rather than import-linter, which lists first-party packages only. `scripts/release_plan.py` declares it unshipped.
- The design's `readPhotos` is split: `locate(photo)` (markers, QR, scale) and `read_photo(located, entry)`; the command fetches the entry between them, so the pure core never holds Central's port.
- A post-software suggestion with a LUT on needs the LUT's table: the reader reads its paper profile by read-only SQL (`PHOTO_WALL_READ_DATABASE_URL`) and bakes it as Central does; no operator route serves a LUT's bytes and none is added for a dev tool. Without it the suggestion refuses `lut-unread` rather than predict without the look.
- Vocabulary added: the drop reason `reflection-edge` (a group whose blacks disagree).
- The post-software target skips the mat shadow as well as drawing the dark box raw (the pipeline table above).
- The display line's schema major becomes 7 and picture operations v7.
- The QR is encoded on the Player (segno; Debian trixie's `python3-segno`, pinned the same in the dev group) from its own player id and the operations' picture change; no wire field carries it.
- The design's tracer needs a real ProRAW photo, which CI cannot take: the CI tracer reads a synthetic photo of the Player's reference target and the layout's paper end to end; the real photo stays the bench step (NOT RUN).
- Q1 as built: one photo (the owner's answer, R10).

The slices then departed from the interfaces in these points (`.claude/errata.md`, the console, reader and Player lanes), again design choices:

- **The layout's groups.** Paired groups round the dark box, numbered clockwise from the top-left, then two near-black ruler groups; the numbering and places are [`contracts/calibration_target.py`](../../contracts/calibration_target.py)'s (`_paired_groups`, `_ruler`). A screen black is paired with its group's paper black; a screen white and grey with its paper white. The greys are 8-bit codes (`GREY_CODES`), so the Player's texture, its reference and the reader take one exact value.
- **Rule 2, the reflection edge.** A paired group is dropped when its screen black less its paper black differs, either way, from that difference's median over the groups by more than `BLACK_EDGE_TOLERANCE` of the screen white; a ruler group (no paper black) compares its screen blacks with every group's median screen black. The interface text's "a black above the median of its surface's blacks" would drop groups under a broad reflection that lifts both blacks alike (harmless, item 1) and fails once a reflection covers more than half the groups. This is the owner's plan item 2 ("a black-to-black difference inside a group"), which binds.
- **Rule 3, copies.** Each copy is judged against the median of all its copies, not the median of the rest (with three copies and one outlier, that would drop a good copy).
- **The suggestion's measures.** The white's colour gap is read as a Photo Wall colour temperature against 6500 K (the kelvin difference under 6500 K light, the same step in mireds otherwise); the tone's gap is 2.2 (q − 1) for the exponent q the greys best follow, as Photo Wall's own gamma acts. Post-software, each of the four knobs away from neutral costs `KEEP_SPREADS` squared in the fit's units rather than a penalty per step (which would stop a too-bright display short of the paper); the aim's white includes the look's own tint, so the colour temperature never fights a LUT. A display warmer than software can correct gets a near-neutral fit: that is pre-software work.
- **The console.** With six choices the Test pattern is a Select, not a SegmentedControl; the targets are named "Target: pre-software" and "Target: post-software", one home (`PATTERN_NAMES` in `frame-picture.tsx`) for the tab and the Picture History. The print links are plain links to the PDF route, which answers as a download.
- **The Player's proof.** The pixel harness (`node-pid1.yml`) is too small to decode the drawn QR, so it checks the drawn modules against the code exactly; the decode itself is proven on the Player's module grid with OpenCV.
- **The code waits for the look.** While a post-software target's LUT is not drawn (its bytes not yet held on the Pi), the QR's square draws light, so a photo is never read against a picture change whose look was not on screen (rule "the photo names its entry").
- **More refusals.** `outside-photo` (a patch or the QR beyond the photo's edge, though three markers were found), `paper-misplaced` (the sheet, as the larger of Letter and A4 each way, would reach over the screen's patches; the detail says which way it is off) and, for a suggestion, `tone-unread` (post-software with no grey read: a normal photo or every grey dropped; never a fit on an assumed display).
- **The reader's imports.** Beyond `contracts` and `player.geometry`, the reader's core imports `central.displays.views` (the sent entry's shape, as the operator route answers it), `appliance.display_host.picture` (`operations_for`, the one rule from software picture to operations) and, in its I/O edge only, `central.displays.lut` (the LUT's bake): each is the one home of what the reader would otherwise copy. `tests/test_calibration_target_reader_imports.py` holds the core free of I/O, not of these packages.

## Findings

- **The paper's ink biased the white level** (reader lane R2). The reader first treated any white between the paper less its ink and the bare paper as matched (a 3 % band). Answered by the owner (R11, 2026-10-14): removed; the paper's white is matched as read, and the bias is a cost (above).
- **The pre-software black only says raise or keep.** "The lowest near-black step apart from raw 0 should be 1" (the stage table) finds crushed steps but never a black too high: a black that glows, the owner's complaint that started display tuning (owner, chat, 2026-10-13: "Even the dark areas of the screen clearly glow."), reads "keep", and under a signal range the display does not expect the near-black ruler's steps are not the steps drawn. Saying "lower" needs a design choice (for example a PLUGE "just visible" margin for step 1, or raw 0's light against the white) and the reading should state the entry's signal range; until then the agent reads the signal range from the entry and uses [0024](0024-display-tuning.md)'s Black level for it.
- **A target ended when the tab was hidden.** The Picture tab hidden ended the session with Revert, so opening the camera on the phone the tab ran on took the target down. Answered by the owner (R12, 2026-10-14): a session with a pattern now lives a minute without the tab's keepalives, and the hidden tab no longer reverts it ([0024](0024-display-tuning.md)).
