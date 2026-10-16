# 0028 — Room light on the wall: the physics of light on virtual paper

**Date:** 2026-10-15 · **Layer:** system (Central, the Pi, the console, the Node bus and the Frame's binding) · **Status:** accepted design, built on the branch `feat/room-light` (one pull request). **Decision owner:** the owner, in chat and on two gate pages on 2026-10-15 (his words below). His answers are current choices he may reopen; everything not quoted from him is a design choice, not his ruling.

**Pages:** the overview, [Room light on the wall](https://claude.ai/artifact/KcbuqYpqF9SVZJbZbsqBjL); the system design, [who owns each light fact](https://claude.ai/artifact/FW7e6jSaENNiZL7v68JCgS).

**Builds on and supersedes in part:** [0022](0022-picture-hardware-and-software-apart.md) (the Display's software group loses brightness), [0023](0023-print-look.md) (its sliders, its "the print look belongs to the Display" and its "relighting: not planned" are replaced here), [0024](0024-display-tuning.md) (the paper test becomes the Display's matched light), [0025](0025-configuration-snapshots.md) (history entries carry the new values) and the [roadmap's step 3](../roadmap.md#3-home-assistant) (its broker link is pulled forward for light).

## Requirements (bind)

| # | Requirement | Source |
|---|---|---|
| R1 | "at all times, i want the display to appear as a natural printed photo" | owner, chat, 2026-10-15 |
| R2 | A fake light from the bookshelf spotlight he will mount: "a bright cone (the direct light) that feathers out to dimmer edges (the ambient light)"; "i like the shader/cone option A" | owner, chat, 2026-10-15 |
| R3 | "i want home assistant to trigger "the spotlight turned on" or "the overhead lights turned off" and leave it to the node renderer to adjust the picture/shaders accordingly" | owner, chat, 2026-10-15 |
| R4 | "each frame. the whole wall might share a trigger, but each configuration will be unique" | owner, chat, 2026-10-15 |
| R5 | "the Frame configuration as a set of physical properties and let the shader act as a sort of "physics engine""; "real units"; "3d space might be too complex without a full 3d model of the space"; "overhead lights are RGB" | owner, chat, 2026-10-15 |
| R6 | "live on the wall. and the more we can automate, the better"; the lights' numbers typed in, then matched by eye ("A is probably the simplest option to start"); calibration photos are "only useful for getting the raw TV settings" | owner, chat, 2026-10-15 |
| R7 | "we should assume that the hardware backlight cannot be controlled"; the display is configured "for the _maximum lighting_ scenario so that the software can artificially dim later" | owner, chat, 2026-10-15 |
| R8 | "factor in external light measurements. A light sensor connected to a player, or values that home assistant publishes" | owner, overview page note, 2026-10-15 (saved 2026-10-16 UTC) |

## His answers on the system page (current choices)

| Question | His answer | What it means here |
|---|---|---|
| Q1: the room darker than the faithful floor (about 100 lx on the placeholder numbers) | **B. Follow the room down** | No floor: the light on the Frame is shown as it is. The brightness stays true; shadows go grey and skies band below the floor, so dithering before the 8-bit signal is required. |
| Q2: the print look's sliders | **B. The paper replaces them** | Brightness, black floor, soft highlights and saturation leave the Display. Ink density sets the black; highlights above the TV's white roll off by one fixed rule; saturation comes only from a paper profile (LUT). |
| Q3: where the paper lives | **A. On the Frame** | The paper (whiteness, ink density, an optional paper profile) is part of the Frame's lighting on its binding; the LUT picker moves from the Display to the Frame. |

## Decision

- **Room lights, observed, never driven.** A room light is a real light in the house (daylight, the overheads, the bookshelf spotlight) with exactly one reporter: its Home Assistant light, one Sensor, or nobody (fixed: always on at full). Central keeps them (`central/room_light/`, pure) and writes their colours and last states as one **room-lights document** on the hub's WALL stream (`contracts/room_light.py`), the same for every Pi; every Node keeps a mirror of WALL, so a Pi keeps the last states while Central is away. The Player reads it through `nodeapi` (a read-only `WallReader`); it never rides the Player's control State.
- **A Frame's lighting replaces its mat** (`contracts/frame_lighting.py`, on `OutputBinding.lighting`): its mat depth, up to four Frame lights, each one Frame's view of a room light (wash, far, close; lux at the Frame with the reference it was read at) and its paper. It is edited live on the wall through Position's trial.
- **The Display's light replaces the sliders** (`contracts/node_picture.py` `DisplayLight` on the picture document): the matched lux (the light at the Frame when the TV's full white matched a sheet of paper) and the panel's black. Contrast, colour temperature, gamma, the signal range and the display's own settings stay as [0022](0022-picture-hardware-and-software-apart.md) and [0024](0024-display-tuning.md) hold them.
- **One physics path** on the Pi (`player/geometry.py` is the scalar reference, `player/native.py` its shader): E(x) summed per channel over the Frame's lights (a close light's inverse square and cone, a far or close light's mat shadow), Lambertian paper ρ = whiteness × (10^−ink + (1 − 10^−ink) × photo), the signal ρE ÷ (0.9 × matched) fitted between the panel's black and white, the fixed highlight rolloff, then dither. Every Frame always goes through it.
- **The highlight rolloff keeps each pixel's colour.** Above the knee (0.8 of the TV's white) a pixel is scaled as a whole by its largest channel's rolloff, so a print under one warm light keeps one colour from its shadows to its paper white; the curve's peak is the paper white's brightest signal on the opening, sampled with every light's cone and the mat's shadow.
- **The Display's colour temperature makes the TV's white neutral, never warm or cool.** The paper match sets it with the White pattern beside a sheet lit by daylight or a 6500 K lamp only, and reads the matched lux at the same time; a warm room is its room lights' kelvin (measured against 6500 K), so the warmth is never counted twice.
- **A Frame light always fits its room light.** Every writer of a Frame's lighting passes one check (`central/room_light/policy.py` `check_frame_lighting`): the Position trial's begin and edit, a Restore from history (refused with the edit's codes), and a new Frame, which views the first always-on room light there is, or none. A room light a Frame names cannot be removed, nor change between a dimmer and a Sensor's reading (`named-by-a-frame`).
- **Home Assistant through MQTT, Central's worker only.** An automation in Home Assistant publishes each light's and illuminance sensor's full state, retained, to `photowall/light/<entity>` and `photowall/illuminance/<entity>`; the worker subscribes to the house's broker (`PHOTO_WALL_MQTT_URL`, set in the deployment). A Pi never talks to Home Assistant. A light meter wired to a Pi is read by HostCore (Linux IIO) and reported on its bus line.
- **Upgrade data:** every Display gets placeholder light (1,000 lx matched, black 0.03 % of white); one fixed room light "Room" is created; every Frame gets one wash of it at its Display's matched lux times its Display's software brightness (which multiplied the linear picture, so the picture keeps it) and a paper of whiteness 0.9 and ink −log10(black), so its picture equals today's with the other sliders neutral. Mat lights' relative strengths have no lux and are not carried: they are entered again in lux.

## Design rules

1. **One writer per value.** A room light's state has one reporter; its colour is configured or reported, never both. A state stands until its reporter writes again: nobody judges a reading's age, so no two clocks judge one fact.
2. **Light facts never ride the media State, and order never matters.** A Frame light whose room light is absent, not yet reported or unavailable adds no light: a total rule, so arrival order needs no invariant.
3. **One render path, in real units.** Every Frame always goes through the same physics; there is no mode chosen by missing data.

## Costs and what is not done

- Below about 100 lx (a placeholder until the bench measures the Samsung QN43Q8F) shadows lift to the panel's glow and grey steps run short (Q1 B); dithering softens the banding, nothing removes the glow (R7).
- A Sensor reads the sum of every light at its point, so it belongs where its light dominates and must not face the wall; a reporter that dies without saying so freezes its light at its last state (House › Lights shows when it was last heard).
- Until a Pi's first room-lights document arrives (a fresh boot), its Frames' lights add nothing and the picture is the panel's black, **and the Player still tags that draw presented**: the witness claims content the wall does not show for those seconds. The WALL mirror keeps the document across a Player restart. Likewise a Frame whose paper profile LUT is still being fetched is drawn without it and tagged presented. Holding the presented claim back until both are drawn is the alternative (readiness would then wait on the hub); not chosen here, for the owner to weigh.
- One rolloff curve per Frame: its peak is the Frame's brightest paper white, so switching on a light that makes a hot spot brighter also compresses highlights the light does not reach (paper white elsewhere on the Frame drops below the TV's white). A fixed curve, the same whatever the lights, would avoid it but would also compress today's paper white below the TV's white; the owner's "one fixed rule" (Q2 B) is read here as one rule per Frame. Between the peak's samples a brighter point may lie; it is drawn at the TV's white.
- The Display's LUT is not carried by the upgrade (its name needs the recipe Central bakes with): a Display that had a print-look LUT loses that look until a paper profile is chosen on its Frames. Its other sliders (saturation, soft highlights, black floor) are gone by Q2 B. Check the deployed Displays' values before the release.
- A Home Assistant light or Sensor added after Home Assistant published its retained state stays "not reported yet" until its entity changes or the worker reconnects: the link reads retained messages only when it subscribes.
- Not done: subtracting known lights from a Sensor's reading, learning a Sensor's view, daylight from house time without a meter, a 3D room, glass reflections, the TV's backlight (a plus if found, R7), named light profiles (replaced by R3's additive lights), the Player reporting which document it drew with.

## Not run

Physical Pi, HDMI and the Samsung bench are not run in this delivery: the placeholders (matched lux, black, the faithful floor) wait for the Grey ramp and a photo at 1,000, 100, 20 and 5 lx read with the calibration reader after a release.
