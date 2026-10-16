# 0023 — The print look: photos that read as prints behind glass

**Superseded in part by [0028](0028-room-light.md) (2026-10-15):** Saturation, Soft highlights, Black floor and the Display's LUT are gone (the Frame's paper replaces them); the Frame's Mat becomes its lighting; "relighting: not planned" is replaced by room light.

**Date:** 2026-10-12 · **Layer:** system (what the Display's Photo Wall (software) picture group holds, what a Frame's binding carries, the Player's final pass, and where a LUT comes from) · **Status:** accepted design, built on the branch `feat/print-look` (one pull request, after [#94](https://github.com/mcurcio/photo-wall/pull/94)). **Decision owner:** the owner, in chat on 2026-10-12 (his words below). Everything not quoted from him is a design choice he may reopen, not his ruling.

**Builds on:** [0022](0022-picture-hardware-and-software-apart.md) (the Display's two picture groups). It fixes 0022's cost "brightness above 100 % clips" with the highlight rolloff.

## Problem

The Player's final pass applied the Display's Photo Wall (software) values (white balance, brightness, contrast, gamma), so a photo looked like a lit screen: the screen's black, its purest colours, and above 100 % brightness the brightest parts clipped flat. The panel's own light washed out the real mat's shadow.

## Requirements (bind)

| # | Requirement | Source |
|---|---|---|
| R1 | "how to make the photos look like they are printed and not digital displays? … It should look like a printed paper photo, unless I intentionally break the deception through movement." | owner, chat, 2026-10-12 |
| R2 | On the black floor, highlight rolloff and saturation: "something that we could do -- today -- via configurable software enhancement during the Player rendering" | owner, chat, 2026-10-12 |
| R3 | "i want #3 and #7. i like the idea of the LUT but I probably cant generate it, so sliders are going to be the baseline experience and a LUT is an enhancement" | owner, chat, 2026-10-12 |
| R4 | "the print look belongs to the Display more than the Frame. It's an extension of the look-and-feel configuration" | owner, chat, 2026-10-12 |
| R5 | "the mat shadow is definitely a Frame quality. I imagine that being configured as a directional light source (or multiple) with distance and intensity…?" | owner, chat, 2026-10-12 |
| R6 | "the shadow wont always be on the top because the lighting doesnt always come from the top. some of my frames sit next to a window, so they will have some cool white light coming from the side AND warm white coming from the top" | owner, chat, 2026-10-12 |
| R7 | "i put glass over the top to make it look like a normal framed photo, so the glossy is part of illusion"; "i have a beveled mat flush against the display surface" | owner, chat, 2026-10-12 |
| R8 | Out of scope: "1&2 probably require hardware that i dont have right now" (brightness and white matched to the room); "a future concern" (changing photos unseen); "another good note for later" (the display's own processing) | owner, chat, 2026-10-12 |
| R9 | Asked whether the LUT ships now or later: "build it all" | owner, chat, 2026-10-12 |
| R10 | "everything the player software supports gets its configuration (standard set)" | owner, chat, 2026-10-12 ([0022](0022-picture-hardware-and-software-apart.md) R2) |

The owner's "distance" (R5) is read as what a light's distance changes, its softness and strength, so each light has those; the short view put that reading to him and the lights' shape below is the recommended option, built under "build it all".

## Decision

- **Two owners, never coupled.** The print look (saturation, soft highlights, black floor and a LUT with its amount) joins the Display's Photo Wall (software) group (R4, R10): the same picture document, session, Done, Revert and Copy as 0022's four, in `contracts/node_picture.py` `SoftwarePicture`. The **Mat** (its depth and up to four lights) is the Frame's (R5), on its binding beside profile and calibration (`contracts/models.py` `Mat`, `Light`, `OutputBinding.mat`), edited live and saved through Position's trial; a mat-only Save never changes the Frame output, so it never re-plans photos. Neither reads the other.
- **One order, neutral skipped.** Per pixel inside a Frame's opening, after the Frame's layers blend in linear light. A step at its neutral value is skipped, never computed, so neutral settings draw exactly the pixels of picture operations v4. The scalar reference `player/geometry.py` `final_pixel` states each step once; the shader in `player/native.py` mirrors it, and the pixel harness holds them equal.

| # | Step (owner) | Acts in | Skipped when |
|---|---|---|---|
| 1 | LUT, mixed in by its amount (Display) | screen-encoded in and out, decoded back to linear light | no LUT, amount 0, or the Pi does not hold it yet |
| 2 | Saturation about Rec. 709 luminance (Haeberli's matrix, CSS `saturate()`) (Display) | linear light | 100 % |
| 3 | White balance × brightness (Display); **while step 4 acts, brightness before it and white balance after it** | linear light | as 0022 |
| 4 | Highlight rolloff (Display): **replaces the clamp after step 3** | linear light, on the brightest channel | 0 %, or brightness at most 100 % (below) |
| 5 | Encode, contrast, gamma, clamp (Display) | as 0022 | as 0022 |
| 6 | Black floor (Display): **the last tone step** | the panel's light: floor + (1 − floor) × light (black point compensation) | 0 |
| 7 | Mat shadow (Frame) | the panel's light × shade, per channel | no light of strength above 0, or depth 0 (`Mat.shades`) |
| 8 | Black outside every opening | — | never (owner, 2026-10-10: "outside the configured frame is "someone else's territory"") |

- **The highlight rolloff.** The peak is white's level after brightness: the brightness (the largest white-balance multiplier is 1). At a peak of 1 or less (brightness at most 100 %) white cannot pass white and the step is the old clamp, whatever the saturation, so paper white stays exactly white. Otherwise the step acts on the brightness-scaled colour, before white balance: values below the knee pass unchanged; above it, extended Reinhard on the brightest channel with the peak as its white brings the peak exactly to white, scales every channel alike (hue kept) and fades toward white near the top (the path to white of darktable's filmic and AgX); a channel saturation pushed past the peak is held at it, so it ends at white too. White balance then multiplies, so the rolloff's white is the Display's white: the colour temperature stays the one home of white and tint, and 100 % to 101 % brightness moves a highlight by a hair, never from warm to neutral. Soft highlights 0 % puts the knee at white (the old clip); 100 % at mid-grey (0.18 in linear light); the range is a starting point for the bench. Whether it acts has one home, the Player's rule (`player/geometry.py` `rolloff_skipped`); the Picture tab says only when it can act.
- **The floor and the shadow are light on the panel.** The black floor reads in light, 0.0 to 10.0 % of white in 0.1 steps (~0.5 % is gloss ink, ~2.5 % matte), and the Pi sends that share as it is. The Player turns the output value back into the panel's light with the sRGB curve, the panel its encode is for (`contracts/node_frame.py` `srgb_to_linear`, one home with `encode_srgb`), lifts black there as black point compensation does (floor + (1 − floor) × light), multiplies a shadow's shade there, and encodes again: one panel model for the encode, the floor and the shade. Lifting the output value instead (the design's "floor + (1 − floor) × value") would wash shadows out, 1.8 times too bright at 1 % light with a matte floor. Photo Wall's gamma setting is a look applied before the floor, so using it would make "2 % of white" another light level at every gamma (a correction to the design's "using the display gamma setting", recorded in the errata).
- **The mat shadow.** Each light: where it comes from (0–359°, clockwise from the wall's up, turned with the Frame's rotation to the nearest quarter turn: the rest of a Cover rotation is the opening's own tilt on the panel, already in its edges), steepness (5–90° off the wall), softness (0–45°, the light's apparent size), strength (0.0–10.0 against the room's other light) and colour (2000–10000 K). With strength s, colour w (the kelvin table's colour at luminance 1, `player/geometry.py` `light_colour`, so strength alone says how bright a light is and its kelvin only its colour) and v how much of the light reaches the point (1 lit, 0 shadowed, eased across the soft edge), shade = (1 + Σ s·w·v) ÷ (1 + Σ s·w): fully lit is exactly 1, so lights never recolour the photo, and in one light's shadow the others' colour shows (by a window, the cool side light's shadow reads warmer, R6). How far a point lies from the edge toward a light is measured in mat space (the opening's own rectangle at its physical size, from the Frame display profile's diagonal), so a skewed Corners opening gets an even band; the shadow's width is depth × cot(steepness) and its soft edge spans depth × (cot(steep − softness/2) − cot(steep + softness/2)). One light's colour uses the same kelvin-to-white table as the Display's colour temperature, `contracts/kelvin.py` (widened to 2000–10000 K for a warm bulb and a cool window), normalised apart: the white balance's largest channel is 1, a light's luminance is 1.
- **The LUT.** The owner uploads a paper maker's free colour profile (an ICC file, R3). **The profile is the one source:** Central keeps it (migration 093's `luts`) and bakes the LUT from it on every read, in milliseconds, so nothing stored can disagree. The recipe (`central/displays/lut.py`, id `lut33-perceptual-relcol-bpc-1-lcms<LittleCMS version>`: the LittleCMS that bakes is part of the recipe, so an upgrade names a new table): sRGB into the paper's own device values perceptually (as a print is made, held to their range as ink is), then back to sRGB relative colorimetric, black point compensation both ways, at each point of a 33 × 33 × 33 grid, one byte per channel. So paper white lands on the screen's white and paper black on the screen's black: the Display's colour temperature and brightness stay the one home of white and tint, and the black floor the one home of black, so a LUT and the floor never stack. Two LittleCMS transforms, not one proof transform, because the one-step proof runs unbounded and handed a matrix-shaper paper's out-of-gamut colours back unchanged (errata). A LUT is **named by its inputs**, profile digest and recipe (`contracts/lut.py` `LutRef`): a new recipe is a new name. The Player fetches it from Central only, with its own bearer, and checks the body against the served `Repr-Digest` and `Content-Length`, as for a Variant. Upload checks and bakes once, so a listed LUT always works: no "making" or "unusable" state.
- **Nothing waits on a LUT but Done.** A LUT the Pi does not hold yet draws the sliders alone at once; it joins when fetched, and the Player names a picture change as presented only once that change's LUT is drawn, so Done waits for it and nothing else does.
- **Picture operations v5, no protocol bump.** The operations gain `saturation`, `highlight_knee`, `black_floor` and `lut`; no frame-protocol message changes shape, only the `picture` event's operations text grows (512 to 1024 bytes), so the Wayland interface stays version 4. A display host and a Player of different releases refuse each other's operations through the strict codec. The display line's schema major becomes 5, the binding carries the mat: Central and the Pis ship together, with no second writer (as 0022 R7).
- **Data on upgrade.** Every new value starts at no change, so the upgrade alone changes no picture. Open picture sessions are dropped and open Position trials ended (their drafts and hashes lack the new fields).

## Alternatives considered

| Alternative | Why not |
|---|---|
| **The print look and the shadow both on the Frame** (the first draft) | The owner placed the look on the Display (R4) and the shadow on the Frame (R5); one per-Display session carrying a Frame's look mixed two owners |
| **The Player bakes the whole colour chain into one 3D LUT** (as OpenColorIO bakes a processor) | Each formula would exist once and cost one lookup per pixel, but it is approximate (trilinear error at the knee, the floor, contrast's clip), a drag re-bakes ~36,000 colours on the Pi's CPU, and it rewrites 0022's shader steps. It becomes right if the bench shows the per-step pass too slow at 4K |
| **Shadows in look terms** (per light: direction, width, softness, darkness, colour; no mat depth) | Tuned purely by eye; width and blur no longer follow each other |
| **Room lights placed once on the wall** | Needs each Frame's place on the wall in millimetres, which nothing records |

Prior art: ICC soft proofing and black point compensation (LittleCMS through Pillow ImageCms); a 3D LUT sampled trilinearly (GPU Gems 2, ch. 24); extended Reinhard with a white point; glTF `KHR_lights_punctual` (directional lights with colour and intensity); percentage-closer soft shadows (a penumbra from a light's size); the CSS `inset` box-shadow and Photoshop's Inner Shadow (an edge-distance shadow, no blur pass).

## Costs

- The look is per Display (R4): two Frames on one display share it, and a display carries its look to a spot without a mat.
- Each formula is written twice (the scalar reference and the shader), held equal by the pixel harness; per-pixel work grows by four steps, a shadow per light and a LUT lookup. Its cost with 4K video on a Pi 5 is not measured yet.
- The paper's measured black is not shown by itself: set the black floor to it. The LUT is 8-bit in and out and between its two transforms; banding on a grey ramp is a bench check.
- Lights are directional: a lamp close to a large Frame is not modelled. They shade only and never relight the photo (no lamp-warmed print). With a flush 45° bevel real shadows are millimetres; the 20 mm depth allows exaggeration.
- Contrast above 100 % still clips, as before.
- A paper profile is added on Settings › LUTs ([runbook](../runbook.md#luts-paper-profiles)); the Picture tab's LUT picker reads the list when the tab opens, so a LUT added while the tab is open shows the next time it opens.
- Deferred: copying a mat and its lights to other Frames; room lights; `.cube` import; a per-Scene switch; matching room light and white (R8). Not planned: paper texture and matte simulation (R7), the display's own processing, relighting.
