# 0029 — Render planes: finish each picture once, let the display controller show it

**Date:** 2026-10-17 to 2026-10-18 · **Layer:** system (the Player, Display Host's shell, Central's Frame and Output document, the console, the media worker) · **Status:** accepted design, built on the branch `feat/render-planes` ([pull request #115](https://github.com/mcurcio/photo-wall/pull/115), one pull request), after [#114](https://github.com/mcurcio/photo-wall/pull/114) (Photo Wall's Weston). **Decision owner:** the owner, in chat and on the gate page [Pictures on the Pi](https://claude.ai/artifact/QCAypbDziqVMxh7t5DEVHT) (design v2 with Q1-Q3, then the build plan v3 with Q4-Q6). His answers are current choices he may reopen; everything not quoted from him is a design choice, not his ruling.

**Supersedes:** the GTK GLArea renderer and its frame pass (`player/native.py`, the fitted pictures of PR 110, the pacing of PR 112, as the old `module-native-renderer.md` described them); the Frame fit of 1c (Cover and Corners, any rotation, four corners as the opening: [operator console design §3](../operator-console-design.md#3-new-terms) as it stood, [architecture](../architecture.md#player-and-rendering)); H.264 video copies ([media preparation](../module-media-preparation.md)). **Builds on:** [0024](0024-display-tuning.md) (test patterns), [0025](0025-configuration-snapshots.md) (Position history), [0027](0027-calibration-targets-v2.md) (the screen board), [0028](0028-room-light.md) (the look), which keep their rules; only where the look is drawn moved.

## Requirements (bind)

| # | Requirement | Source |
|---|---|---|
| R1 | "The pi hardware is very graphics oriented, and we should be using it to its full advantage. How should the node architecture change to optimize rendering?" | owner, chat, 2026-10-17 |
| R2 | "We should unify the concepts as much as possible. Photos and video should share the same scale and crop pipeline" | owner, chat, 2026-10-17 |
| R3 | "I'm ok with simplifying how frames can be positioned on the display. We can make the position pure crop and scale. Rotation is really just for the case where a display is turned sideways, so if we can handle that via some native config (hdmi/display orientation?) then we can drop arbitrary rotation" | owner, chat, 2026-10-17 |
| R4 | "Patch Weston, make it part of our build pipeline" (Weston 14's 32-bit plane masks kept the Pi 5's HDMI primary plane from scan-out; built in #114) | owner, chat, 2026-10-17 |
| R5 | The Player makes no content decision: it draws the alphas the plan carries | owner, 2026-10-10 ([execution contract](../execution-contract.md#a-scenes-transition-when-a-photo-starts-to-show)) |
| R6 | A 4 GB Pi 5 is supported; no backwards compatibility | owner, 2026-10-03; global rules |

## His answers (current choices)

| Question | His answer | What it means here |
|---|---|---|
| Q1: must video carry the same look as photos? | **Yes** (2026-10-17, saved 17:28 UTC) | Every video frame goes through the same fit pass as a photo: the paper, the room's light, the LUT and the Display's settings. Costs about 8 ms of GPU per 1080p frame. |
| Q2: the mid-fade look | **The display controller mixes** (2026-10-17, answered before the bench photos) | 60 fades a second with no GPU; a cross-fade is darker mid-way (below). |
| Q3: is "turned sideways" a fact about the spot on the wall or the screen? | **The spot on the wall** (2026-10-17) | The turn is a Frame setting (`frames.turn`); Central sends it in the Output document; Display Host turns the screen. It stays through a Pi swap or a port change. |
| Q4: several Frames per screen? | **Later** (2026-10-18, saved 04:57 UTC) | One Frame per screen for now; the Player still builds each screen as a list of planes, so more Frames need no new shape. |
| Q5: video codec and size | **HEVC now, one 1080p copy per video**; in chat: "1080p at 30 is fine" (2026-10-18) | One HEVC 1080p30 copy per video, decoded in hardware; H.264 copies and software decode are deleted. |
| Q6: a second screen on the test Pi? | **No** (2026-10-18) | Two-screen limits stay estimates. |
| The plan | **Go** (2026-10-18) | |

## Decision

- **Finish once, show as a plane.** Each Frame's picture is drawn once by the GPU at the Frame's own pixels, already turned for its screen, with the whole look (`player/fit_pass.py`); the display controller shows it as its own display plane and fades it by the plane's alpha, every refresh, with no GPU. A video's frame is finished per decoded frame. [Plane renderer](../module-plane-renderer.md) is the module.
- **Design rule 1: the Player decides every pixel; the base applies.** Display Host's shell serves standard Wayland pieces (sub-windows, window alpha, the output transform) and computes nothing about a Frame; Display Host never imports the Frame's geometry (import-linter). The fade curve keeps its one home (`contracts/models.py` `fade_share`).
- **Design rule 2: a Frame is an upright rectangle** of its profile's aspect on its screen as it hangs (`contracts/frame_fit.py` `FrameRectangle`, `place`; the Position form `RectangleFit`: centre and width as fractions of the hung screen). Every Frame on one screen shares that screen's turn: 0, 90, 180 or 270 degrees clockwise (`contracts/display_turn.py`). No corners, angles, perspective or masks.
- **Design rule 3: a composition is built only within the controller's budget.** The Pi 5 glitches rather than refuse an overload, so a pure load rule (`player/controller_budget.py`) is checked before every screen update; over it, the bottom-most picture gives up its place (readiness failure `capacity`, as before).
- **A fade's strength is the controller's encoding of the plan's alpha.** The controller mixes stored sRGB values, not light. Over black (a fade through black) the plane's alpha is `encode_srgb(alpha)`; over a picture (a cross-fade, or a Run playing beneath) the alpha itself. The Player reads which from what it shows beneath, by its one rule of what can be seen (`player/visibility.py`), so Central gains no field and no copy of that rule. R5 is kept in letter: the alpha drawn is the plan's.
- **The screen's ground.** Each screen's own surface shows an opaque black picture of the whole screen beneath the planes (`player/plane_renderer.py`), because Weston never puts a single-pixel buffer on a plane and, without a real buffer under translucent pictures, falls back to compositing everything (tracer 2, finding A). Test patterns, calibration targets and Position's grid are drawn into the ground.
- **The turn is Weston's.** Display Host sets the Output's transform from the Output document (`appliance/display_host/screen_turn.py`, the shell's `turn` op); the Player draws pictures already turned and marks them with the matching buffer transform, which Weston shows on a plane with no copy (bench T5). The controller cannot turn a picture a quarter turn itself.
- **Video is HEVC 1080p30, decoded in hardware, with the photo look** (Q1, Q5): `media/prepare.py` `VideoRecipe` (HEVC Main 8-bit, level 4.1, `hvc1`, at most 1920 on the longer side and 30 frames a second); the Player's `player/decode.py` plays it through the V4L2 stateless decoder as SAND128 dmabufs the GPU imports. Photos and videos have one recipe each, so a video change never renames a photo's copy again.
- **The amber tint keys on the health client being alive**, not on a mapped health surface: the health layer unmaps when clear, so the Player's pictures can stay on planes.
- **Stored Positions converted once** (migration 102): every Position (Frames, Players' last configurations, plan offers, Position history, both copies in trials; trials in flight end) becomes the cover rectangle it drew, upright on its screen as it hangs, shrunk to lie on it; a quarter turn of the old rotation becomes the Frame's turn. The screen's size is the mode the Pi enrolled, else the preferred mode of the display's EDID report (every Pi before this release enrolled 0 x 0; Weston, given no mode, runs the preferred one). A Frame whose conversion is exact (its size known, upright at its shape) stays ready; every other Frame's Position is to check; a Frame with neither size known is centred at full width and to check. Migration 101 added `frames.turn` and the turn to every stored Output document (the display line's schema 9).
- **A Position history entry records the turn it was made for** (`position_entries.turn`, `MadeFor.turn`): a rectangle is fractions of the screen as it hangs, so Restore refuses an entry made for the screen hung another way (`made_for_other_turn`), and a turn appends a kept entry (`kept-turn-changed`), as a profile change does. Migration 102 writes each converted entry's own turn.

## Measured limits (test Pi 5, 4K monitor, 2026-10-18)

Bench evidence under `/Volumes/Dock/tmp/pi-render-bench/` (T2-T5) and the tracer runs under `/Volumes/Dock/tmp/node-render-arch/` (tracer, tracer2, tracer3, m2-pi).

| What | Measured | Used as |
|---|---|---|
| Planes | 16 overlay planes for the HDMI ports; Photo Wall's Weston (#114) gives all 56 vc4 planes their own bit | `PI5.planes` 16 |
| Source pixels per output line | about 15,360 (four full-width 4K layers clean, five glitch) at the fixed 500 MHz controller clock; 4K downscaled glitches at four; at most eight 4K-wide planes | `PI5.line_pixels`; pictures finished at the Frame's size, never scaled by the controller |
| Overload | never refused; glitches; the kernel's underrun counter is dead on this chip, the pixel valve's sticky status bits (0xA00) see it | the budget; the bench's register watch |
| Fade mix | stored values: a cross-fade up to 73-74 levels of 256 darker mid-way, 8-11 on average on real photos; a dip within 6 with `encode_srgb` | `plane_strength`; Q2's cost |
| HEVC hardware decode | 12 x 1080p30, 8 x 1080p60, 3 x 4K30, 2 x 4K60; H.264 is CPU-only on the Pi 5; 10-bit not supported by GStreamer 1.26.2 | one 1080p30 HEVC copy per video |
| The look on video | about 8 ms of GPU per 1080p frame; 10-12 ms for any 4K source | Q1's cost |
| The turn | a buffer transform matching the output's is shown on a plane with no copy | the Player draws turned |
| Targets | LINEAR renders at the same cost as tiled and scans out unconverted; scan-out buffers in ordinary memory, not CMA | `player/gpu.py` targets |
| Before | about 11 fades a second at 4K, GPU 94 % busy, 82-86 °C | |
| After (tracer 3, milestone 2) | every picture on a plane, overload included; plane alphas as computed; no glitch in 400 s; GPU idle while pictures hold; turned screen with no copy; 60.0-60.4 fades a second unloaded | |

## What it gives up, costs and defers

- **Gives up:** off-axis turns, perspective, and odd-shaped or skewed openings; a crooked mat now shows the whole rectangle. A cross-fade is darker mid-way (Q2). The identify banner and the Player's Central-link notice on an unbound Output: neither was ever visible on the node path (the shell's curtain covers an Output not handed off, and an unbound Output is never granted), so they are deleted and the Player no longer offers Identify (Central answers `identify_unsupported`). Whether Identify moves to Display Host's page or its button goes is open for the owner.
- **Costs:** new C in the shell (window alpha until Weston 16 brings it upstream; then it is deleted, never kept beside it) and the frame client; every video and every photo was prepared once more (the recipes split per kind); four encoder threads while a video prepares. 33 MB per 4K Frame picture, twice while a still changes, three for a playing video. The health tint brings back Weston's GPU while it shows; a hung but connected health client shows no amber. A turn makes the Frame's Position one to check. After migration 102, a Frame whose screen size is known neither from its Pi's enrollment nor from its display's EDID report is centred, full width and to check until its Position is kept once. The console's editor places and checks a rectangle with its own copy of `place`, `hanging` and the rectangle check (`central/console/src/placement.js`), held to Central's pixel for pixel only by a parity test (`tests/test_console_placement.py`): a convention, kept because a drag asks no round trip; Central's Save check stays the authority.
- **Seen only on a Pi:** planes, plane alpha in the kernel, the glitch bits, GBM targets, the hardware decoder and the turn without a copy. CI checks the finishing passes on software GL and the shell and screens under a headless Weston; a change to this path runs the render bench on the test Pi before merge.
- **Open:** a photo fading into a playing video runs at 56-57 fades a second, not 60 (the Player's main loop late by about 15 ms with no GPU in the gaps). A video longer than the preparation budget allows (well under `max_video_seconds`) fails and is retried as transient. For the owner: migration 102's rule when no screen size is known (centred, full width, to check; the other choice was to take the Frame's profile as the hung screen); whether Identify moves to Display Host or goes.
- **Not yet built (found by the end review, each needs the test Pi to prove):** starting a video still waits on the main loop for the decoder's preroll (up to 5 s; typically the decoder's first frame), so a fade on another layer pauses meanwhile; the fix is a preroll polled like a still's decode. A pending GPU fence is asked again each millisecond rather than watched as a file descriptor (`EGL_ANDROID_native_fence_sync`). The Position trial's overlay still travels as four corners of the rectangle. Display Host does not report the turn the shell confirmed, so Central cannot show a screen left unturned (Display Host asks again until the shell confirms it), and its picture send refused at startup still waits for the next request (`appliance/display_host/picture.py`, the same class as the turn's).
- **Deferred:** several Frames per screen (Q4: Central, console and grants per Frame); 10-bit video; video above 30 frames a second; an hour's heat; two-screen limits measured (Q6).

## Deploying

- **Take a `pg_dump` of Central's database before deploying.** Migration 102 rewrites every stored Position in place (Frames, Position history, trials, Players' last configurations and plan offers); no Cover or Corners body is kept. `git revert` cannot undo it: the code before this release reads only Cover and Corners, so after a revert Central could not read a Frame, a configuration or an offer. The way back is that dump ([runbook](../runbook.md#deploying-the-render-planes-release)).
- **Deploy Central, then apply the Pi release at once.** Between the two, a Pi on the old release refuses every Output document Central sends (the display line's schema 9 carries `turn`, and the old decoder refuses an unknown field: no power, Night off or picture change reaches it) and every plan whose bindings carry a rectangle, so its screens keep what they show. Releases do not follow by themselves.

## Considered and not taken

- **Video straight to the controller** (the controller crops, scales and converts the decoder's output): many more video Frames, but video would skip the look and look unlike a photo beside it. Not taken (Q1).
- **The GPU mixes while a Frame fades:** today's mid-fade look at 20-40 fades a second, with GPU heat. Not taken (Q2).
- **GTK 4's video offload or GStreamer's Wayland sink:** GTK 4's offload refuses opacity, and the sink skips the look.
- **Disabling the Pi's writeback units so the HDMI planes fit 32-bit masks:** impossible as a setting (disabling both crashes vc4); Weston was patched instead (R4, #114).
- **Another compositor, or none:** not taken; the shell, the lease and the evidence stay Weston's.
