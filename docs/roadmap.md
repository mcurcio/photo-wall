# Photo Wall roadmap

**Status:** as recorded in [decision 0019](decisions/0019-first-principles-console.md).

This is the product roadmap. It walks the whole life of a Photo Wall install the way a power user lives it, from before any hardware arrives to fixing things years later, and orders the work by what the owner can do on the real wall after each delivery. Nothing in it is built until its delivery says so.

The owner, 2026-10-09 (chat), on the first-principles console review: "I LOVE the Journey section of that doc. I want those ideas codified as the roadmap." The review page is [Console Setup Review](https://claude.ai/artifact/EwjdvEcM82HyLDb4BrDSdC).

Where things are defined:

- The console's target design (pages, the Frame page, power, Home Assistant, the settings catalogue) is the [operator console design](operator-console-design.md); the decisions and what they replace are [decision 0019](decisions/0019-first-principles-console.md).
- This roadmap owns four things the others link to: each step's status today, the delivery order with each delivery's scope and acceptance, the moments of truth, and the deferred list.
- What the owner asked for is in [requirements › Operator experience](requirements.md#operator-experience).
- The engineering slice order for the first MVP is the [implementation plan](implementation-plan.md); this roadmap orders product capabilities on top of it. Each delivery is split into pull requests when it starts. A delivery that contains a mechanism not yet designed gets its own short design pass before its first pull request; those items are marked **design pass** below.

## Words used here

| Word | Meaning |
|---|---|
| Setting #N | Row N of the [settings catalogue](operator-console-design.md#11-settings-catalogue) |
| Power request | A request to turn some displays on or off, saying who asked, which Frames, until when and why ([design](operator-console-design.md#7-power)) |
| Hold rule, guard settings | How long each power request lasts, and the few settings that limit who may turn displays on or off at night ([the hold rule](operator-console-design.md#the-hold-rule)) |
| MQTT discovery | The way devices announce themselves to Home Assistant through its message relay (the MQTT broker), so they appear with no setup there |
| Request topic | The one MQTT address Home Assistant can send a power request to |
| CI test | An automated test the pull request adds and the merge pipeline runs; scenario proof is always one of these ([evidence](../CONTRIBUTING.md#choose-the-right-evidence)) |
| Bench step | A short check an agent runs on the test Pi over SSH, only where real hardware must be proven; it is recorded as evidence, never as a substitute for a CI test |
| Spike | A short, throwaway investigation whose output is a finding (here, a table of what each display supports) |
| HDMI-CEC, DDC/CI | The two command channels inside an HDMI cable: CEC is the TV remote-control channel (power, input); DDC/CI is the monitor settings channel (brightness, contrast, often power) |
| Size | S = one pull request; M = two or three; L = must be split before it starts (the split is listed) |

How to read the journey tables. **P** (priority): M = broken without it, S = should have, N = nice. **Today** is the state on 2026-10-09: **exists** (in the console, perhaps in a poor form), **built, no controls** (Central or the Pi can do it, the console cannot reach it), **missing** (nowhere). The note says what is there now. **Delivery** is where the step is finished. Update a step's Today cell when its delivery lands.

## The journey

### A. Before any hardware

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| A1 | Install Central | Central installs through its existing deploy path (the owner runs it from his infrastructure repository; one Compose file for a single host); the first page is a checklist (database, network boot serving, boot files staged) | M | built, no controls | Checks exist in pieces; no checklist | [6](#6-setup-and-hardware-life) |
| A2 | Sign in | Create an owner account | M | missing | A server token from an environment variable | [3b-i](#3b-i-home-and-phone) |
| A3 | Connect Immich | Paste URL and API key, Test: "41,203 photos, 212 albums, 96 people" | M | exists | Settings › Photo library (4): set by the deployment (read-only, from Central's environment) or pasted there, no file and no restart; Immich's photo, video, album and people counts; Check now; Needs a look with the fix | [4](#4-real-photo-sources) |
| A4 | House basics | Timezone, location for sunrise and sunset, units | M | missing | Not settable | [2b](#2b-the-week) |
| A5 | Ready for Pis | A "network boot OK" self-test and help for the router's network-boot setting; Pi 5 with 4 GB or more stated up front | S | missing | A fresh install refuses every boot until a release is picked | [6](#6-setup-and-hardware-life) |

### B. The first Pi and display

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| B1 | Pi appears | Plugged in, it shows up within a minute and the display shows its code | M | exists | Appears under Hardware; no on-screen code | [6](#6-setup-and-hardware-life) |
| B2 | Identify | The display flashes its name and a border | M | exists | "Identify Panel" on the Binding tab | [1a](#1a-the-frame-page) |
| B3 | Name and place | "Living room left", pick the wall | M | missing | A code-style Frame id; one wall only | [2b](#2b-the-week) (names, room label); [6](#6-setup-and-hardware-life) (several walls) |
| B4 | Fit the picture | Test pattern; drag corners, rotate, nudge; live on the display; Done/Revert | M | exists | Position tab: Cover by default or Corners as an opt-in, any rotation in Cover, Trim edges removed (1c) | [1a](#1a-the-frame-page), [1c](#1c-brightness-contrast-and-colour) |
| B5 | Picture quality | Brightness, contrast, colour temperature, gamma, grey ramp | S | exists | Picture tab (1c): brightness and contrast on the display over DDC/CI or as picture adjustment, colour temperature and gamma as picture adjustment, live with Done and Revert, Copy; no grey ramp | [1c](#1c-brightness-contrast-and-colour) |
| B6 | Power | Detect how this display can be switched; Test off/on | M | exists | Power tab (1b): method (best detected: HDMI-CEC, DDC/CI or signal off), the two input switches, Test: turn off and Turn on; no smart plug; when displays are on waits for 2a | [1b](#1b-display-identity-and-power) |
| B7 | First photos | A Favourites slideshow starts by itself | M | missing | Build a Source, a Scene and a Program by hand | [6](#6-setup-and-hardware-life) |

### C. More screens

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| C1 | Wall layout | Drag scaled rectangles in real cm or inches onto each wall | S | exists | One wall; millimetres typed by hand | [6](#6-setup-and-hardware-life) |
| C2 | Rooms and groups | Every room is a group; act on groups | M | missing | No groups | [6](#6-setup-and-hardware-life) |
| C3 | Copy settings | Copy picture settings to other Frames | S | missing | Nothing | [1c](#1c-brightness-contrast-and-colour) |
| C4 | Two displays per Pi | Both HDMI ports listed, each feeding its own Frame | S | exists | Possible through Binding | already met |

### D. Content

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| D1 | Pick photos | Albums, people, places, dates, favourites; live count and thumbnails | M | exists | The Photo source editor (4): albums, people, a place, fixed or relative dates, favourites, tags, media types, partners; the pool's counts and thumbnails fill as Immich is read, before and after saving; several Sources per Scene | [4](#4-real-photo-sources) |
| D2 | Exclude | Exclude people and albums; archived, hidden and screenshots excluded by default | M | exists | Exclude albums, people, tags and screenshots (on by default) (4); archived, hidden and locked never included, one fixed line | [4](#4-real-photo-sources) |
| D3 | Photo fit | Every photo fills its Frame: smart crop keeps faces and the interesting part; a photo that would lose them does not go on that Frame. No mats, blur or pairs | M | exists | Smart crop and the Scene's Crop limit (5a); before 5a, a centred crop on every Frame (not "Nothing", as this cell said) | [5a](#5a-photo-fit-and-order) |
| D4 | Order | Shuffle (the default), oldest first, newest first, On this day; no repeats only ranks, so a Frame never goes blank because of it | M | exists | The Scene's Order and Don't repeat within (5a); newest first, fixed, before it | [5a](#5a-photo-fit-and-order) |
| D5 | Timing and transitions | Seconds per photo, crossfade, stagger between Frames | M | exists | Seconds per photo and the fade's length (through black, [1x](#1x-expose-what-already-exists)); no crossfade or stagger | [5b](#5b-video-captions-and-transitions) (type, stagger) |
| D6 | Video | Muted, length cap, share of videos | M | missing | Nothing | [5b](#5b-video-captions-and-transitions) |
| D7 | Captions | Date, place, people | S | missing | Nothing | [5b](#5b-video-captions-and-transitions) |

### E. Scheduling

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| E1 | Weekly routine | A calendar with repeating blocks | M | exists | One window; repeats make up to 60 copies | [2b](#2b-the-week) |
| E2 | Night off | Off 23:00 to 07:00, displays actually off | M | missing | Nothing | [2a](#2a-power-night-off-and-all-offon) |
| E3 | Sunrise and sunset | Times relative to the sun | S | missing | Nothing | [2b](#2b-the-week) |
| E4 | Holidays | Date ranges that override the routine | S | missing | Nothing | deferred |
| E5 | Default content | What shows when nothing is scheduled | M | missing | Nothing | [2b](#2b-the-week) |
| E6 | Why is this playing? | An explanation for every Frame | M | exists | Planned facts by internal ids | [2b](#2b-the-week) |

### F. Daily use, from a phone

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| F1 | Glance | A live map of what each Pi presents | M | missing | Planned facts only; no picture | [3b-ii](#3b-ii-the-live-map) |
| F2 | Skip and Previous | On a tile | M | missing | Nothing | [3b-i](#3b-i-home-and-phone) |
| F3 | Never show | Two taps, undoable; the photo goes on the wall's Never show list | M | missing | Re-tag the photo in Immich | [3b-i](#3b-i-home-and-phone) |
| F4 | Show now | An album or person on chosen Frames, for 1 hour | M | exists | Exists; no Frame choice, no end time | [3b-i](#3b-i-home-and-phone) |
| F5 | All off/on | One button | M | missing | Nothing | [2a](#2a-power-night-off-and-all-offon) |
| F6 | Phone layout | Bottom tabs, usable while standing at the display | M | missing | Tables scroll sideways off the screen | [3b-i](#3b-i-home-and-phone) |
| F7 | Home Assistant | Frames, power, Scenes and status in Home Assistant; motion, room light and away mode drive the wall | S | missing | Nothing | [3](#3-home-assistant) |
| F8 | Pause | Freeze a Frame on a photo for an hour; it carries on by itself | S | missing | Nothing | [3b-i](#3b-i-home-and-phone) |

### G. Fixing things

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| G1 | Health at a glance | One status word per Frame and for the house | M | exists | Repeated in five places, internal words, a manual Refresh | [3b-i](#3b-i-home-and-phone) |
| G2 | Diagnose | A plain cause naming the failing part, with a time | M | exists | Detailed but full of internal terms | [3b-i](#3b-i-home-and-phone) |
| G3 | Restart | Restart the app, reboot the Pi, with progress | M | exists | Reboot only | [3b-i](#3b-i-home-and-phone) |
| G4 | Replace a Pi | "Which Frame did you plug this into?" One tap | M | exists | Unbind and rebind by hand across pages | [1a](#1a-the-frame-page) (Replace with…), [6](#6-setup-and-hardware-life) (New Pis waiting) |
| G5 | Display changed | Picture and power follow the display; the Frame is not ready until its position is re-checked ([requirements](requirements.md#installation-model)) | M | exists | Display identity from the EDID (1b); the Hardware tab's display-changed card: confirm the Frame profile, then re-check Position | [1b](#1b-display-identity-and-power) |
| G6 | Updates | New releases pre-download; you click Apply | M | exists | Exists behind a gate the console can't open | [7](#7-peace-of-mind) |
| G7 | Backup and restore | Download, a nightly copy, restore with a preview | M | missing | Nothing | [7](#7-peace-of-mind) |
| G8 | Logs | Per-Pi logs without SSH | S | missing | SSH only | [7](#7-peace-of-mind) |
| G9 | Notifications | Told when a Frame stays Not showing, and not otherwise | S | missing | Nothing | [7](#7-peace-of-mind) |
| G10 | Retire and reset | Retire a Pi; reset or uninstall Central cleanly | S | exists | Retire a Pi exists; no reset | [6](#6-setup-and-hardware-life) (Retire), [7](#7-peace-of-mind) (reset) |
| G11 | Upgrade Central | A new Central version installs with a backup taken before its database changes | M | missing | Migrations run at start-up with no backup | [7](#7-peace-of-mind) |

G6 follows the existing release choices in [console DDD Part E](operator-console-ddd.md#part-e-v2-only-console-and-node-release-workflows-feature-layer), under [requirements › Player provisioning](requirements.md#player-provisioning).

## Delivery order

**1a → 1x → 1b → 1c → 4 → 2a → 2b → 3b-i → 3b-ii → 5a → 5b → 3 → 6 → 7.** The owner's priority is a visibly working wall: real photo sources (4) depend on nothing and give the most visible change, so they come right after the three knobs (the owner can move 4 back). Home Assistant (3) comes after the slideshow work because it needs Frame names, status words and Show now with an end time first.

**Developed** names the merged pull request that built the delivery; a dash means it is still to do. Each delivery's pull request fills in its own row.

| # | Delivery | Size | Outcome | Depends on | Developed |
|---|---|---|---|---|---|
| [1a](#1a-the-frame-page) | The Frame page | M | Set a Frame's position from its own page, with no timeout | none | [#70](https://github.com/mcurcio/photo-wall/pull/70) |
| [1x](#1x-expose-what-already-exists) | Expose what already exists | S | Every setting Central or the Pi can already do has a control | none | [#71](https://github.com/mcurcio/photo-wall/pull/71) |
| [1b](#1b-display-identity-and-power) | Display identity and power | L | Turn a display off and on from the console; its settings follow it | 1a | [#76](https://github.com/mcurcio/photo-wall/pull/76) |
| [1c](#1c-brightness-contrast-and-colour) | Brightness, contrast and colour | M | Brightness and contrast act on the display where it allows | 1b | [#79](https://github.com/mcurcio/photo-wall/pull/79) |
| [4](#4-real-photo-sources) | Real photo sources | L | Connect Immich and build a real Photo source in the console | none | [#81](https://github.com/mcurcio/photo-wall/pull/81) |
| [2a](#2a-power-night-off-and-all-offon) | Power: Night off and All off/on | L, with 2b | Displays go off at 23:00 and on at 07:00 | 1b; U1 amended (2026-10-12) | [#86](https://github.com/mcurcio/photo-wall/pull/86) |
| [2b](#2b-the-week) | The week | L, with 2a | One repeating Schedule; every Frame says why it plays | 2a | [#86](https://github.com/mcurcio/photo-wall/pull/86) |
| [3b-i](#3b-i-home-and-phone) | Home and phone | L | Check and act on the wall from a phone, signed in | 2a, 2b | — |
| [3b-ii](#3b-ii-the-live-map) | The live map | M | Each tile shows what its Pi presents | 3b-i | — |
| [5a](#5a-photo-fit-and-order) | Photo fit and order | L | Every photo fills its Frame, never on two Frames at once; Scenes shuffle | 4 | [#82](https://github.com/mcurcio/photo-wall/pull/82) |
| [5b](#5b-video-captions-and-transitions) | Video, captions and transitions | M | Muted, capped videos; captions on request | 5a | — |
| [3](#3-home-assistant) | Home Assistant | L | Frames as Home Assistant devices; motion wakes a room | 2a, 2b, 3b-i | — |
| [6](#6-setup-and-hardware-life) | Setup and hardware life | L | Fresh install to photos, and a Pi swap, without help | 1b, 2b | — |
| [7](#7-peace-of-mind) | Peace of mind | L | Backups, safe upgrades, notifications, logs | 3b-i | — |

### Owner decisions the order waits on

| Decision | Blocks | Recommendation |
|---|---|---|
| Amend U1 so intentional operational darkness (the power schedule, Pause, All off, a display powered off on purpose) counts as intended and shows as Resting | 2a, 3b-i | **Decided** 2026-10-12, owner: "Amend U1" ([system gate](https://claude.ai/artifact/HHNRy2Hs1CpEm5VVK58DWm)); the amended text is [U1](requirements.md#failure-visibility-and-recovery) |
| Allow a Scene to ask for its displays on or off (operational state in authored content) | Settings #70 and #89 only; nothing else waits | **Not now**, owner 2026-10-12: "Not now" ([system gate](https://claude.ai/artifact/HHNRy2Hs1CpEm5VVK58DWm)); the power timetable can take Scene requests later ([design §7](operator-console-design.md#7-power)) |
| D13, visible-history accounting ([design decisions](design-decisions.md)) | Nothing now: for 5a, "shown" means displayed and no-repeat only ranks (decided 2026-10-11) | The rest of D13 (partial coverage, motion limits) stays open |
| What Pause does to the screen (freeze the photo, or stand aside) | F8 in 3b-i | Freeze the current photo |

### 1a. The Frame page

- **Outcome:** from the wall plan, the owner clicks a Frame, drags a corner on its Position tab, and the change shows on the display with Done and Revert, with no 30-second timeout.
- **In scope:** B4, B2 (Identify in the page header), the existing software brightness moved to the Picture tab (B5's home), Replace with… on Hardware (G4's first half). Settings #96–#101, #108, #109 (editable while bound), #122, #123.
- **Out of scope:** hardware brightness, contrast and power (1b, 1c); the Home page (3b-i); New Pis waiting (6).
- **Acceptance:** CI tests `tests/browser/test_console_frame_page_browser.py` (open a Frame from the plan; drag and arrow-nudge a corner; Done stays disabled until the presentation acknowledgement for the latest change arrives; Revert restores the saved corners; the page keeps its session with `keepalive` while the tab is open, and a closed page's session ends within the idle window), and `tests/test_node_calibration.py` (the session's `keepalive` slides the idle deadline up to the hard one, and `renew` hands the draft to the next session). There is no separate `tests/test_calibration_session_keepalive.py`: those two cover it. Bench step: an agent drags a corner against the test Pi and reads Display Host's presentation acknowledgement; visible pixels are not claimed.
- **Depends on:** nothing.
- **Risks and unknowns:** today's expiry lives in Central and in Display Host's diagnostic (U9 lets it expire independently); replacing it with a keep-alive is a small Central change, not console-only.
- **Size:** M (Frame page shell with Position; Picture and Hardware tabs).
- **Moment it proves:** 3.

### 1x. Expose what already exists

The owner's first ask: "Central needs another UI pass to make sure that all of the features are being exposed on the UI."

- **Outcome:** every setting that Central or the Pi already supports but the console cannot reach gets a control, and a saved value reaches what the Pi plays.
- **In scope:** the eight built-but-no-controls settings: #41 transition length, #62 outro, #63 black or see-through when fading, #64 keep last photo when nothing is eligible, #65 keep Frames visible together, #67 child Scenes, #79 how a Schedule ends, #128 releases kept on Central. D5's length part.
- **Out of scope:** anything that needs new behaviour (transition type #40, stagger #42 are 5b).
- **Acceptance:** CI tests `tests/browser/test_scene_flow_browser.py` and `tests/browser/test_schedule_flow_browser.py` extended (set each value, save, reload, the value shows), and a DB test `tests/test_exposed_settings_roundtrip.py` (each value is stored and appears in the plan sent to Players). No bench step.
- **Depends on:** nothing.
- **Risks and unknowns:** the list comes from the 2026-10-09 code audit; re-check it at the start. A setting with no operator write path needs one plain endpoint, which makes this more than console-only.
- **Re-check at the start (2026-10-09):** five settings shipped on the Scene flow's Playback step: #41 (Fade between photos; the built fade passes through black, so 5b's crossfade is a new type), #62 and #63 as one choice, How it ends (Stops, Black, Fades out) with its length (the built outro is either opaque black or a fading photo, so the two rows are one axis; their catalogue defaults, "dissolve 3 s" and "black", disagree, and the built default, no ending, stays until the owner picks), #64 and #65. Three were not as listed: #67 child Scenes needs a nested Scene editor, so it is a 1x follow-up; #79 is not built (a Program's end always finishes gracefully, [requirements](requirements.md#progression-visibility-and-target-control); "stop now" is new behaviour); #128 is a fixed constant (`central/content_catalog/boot_policy.py` `WINDOW_SIZE = 3`), with no stored setting or Settings page, so it is missing and goes with #129 in [7](#7-peace-of-mind). Neither the Schedule flow nor its browser test changed. The Save model stays the flow's one Save (from the next start); **Save and apply now** arrives with `SaveBar` in 2b.
- **Size:** S.
- **Moment it proves:** none; it answers the owner's first ask.

### 1b. Display identity and power

- **Status (2026-10-10):** built in one pull request ([#76](https://github.com/mcurcio/photo-wall/pull/76); owner, 2026-10-10: one pull request, built in the order image tools, then Display identity with G5, then power methods and the Power tab). The design is [operator console design §5–§7](operator-console-design.md#6-display-identity) and the Node side [Player architecture](player-architecture.md#display-identity-and-power-1b). The owner's answers on the [gate page](https://claude.ai/artifact/DjZTF9HGKWLVy6kexikeUp): a test lasts 5 minutes, or less if Turn on is pressed, counted on the Pi (q1); a display with no usable serial is recognised by make and model, tied to its Frame (q2); the Player crash fix goes inside this pull request (q3).
- **Built:** the image's DDC/CI driver (loaded at boot), `ddcutil` and `cec-ctl`; each HDMI port's EDID identity and modes reported over the Node bus; Central's Displays, recognised as q2 says, and readiness worked out from Position commits (the stored `calibration_valid` flag is gone, migration 073); the Output document with its request stack (standing on, then a test), carried out by the Pi through HDMI-CEC, DDC/CI or signal off; the Power tab (#116 without the smart plug, #117, #118, Test off/on) and the Hardware tab's Display and display-changed card (#120). With q3: the Player's descriptor leak and its death on a signal blip are fixed, the broker restarts a dead Player on a backoff, and the Node's judge raises `app_absent` and `app_resource_exhausted` ([Player architecture, gap G13](player-architecture.md#observed-gaps)). Central already showed the silent Player; a CI test now replays the incident.
- **Not built:** setting #110 (the HDMI mode is shown, read-only; nothing applies a mode, errata E-1B-3); a power schedule, All off and holds (2a); the smart plug (3); a Pi taking a show without Central. Known gaps: signal off has run only on a headless Weston, not a DRM output (Player architecture G16); #118 cannot fire, since neither cable channel says which input is the Pi's; video playback on the Pi is likely broken by a separate GStreamer bug found here (G14). The bench step below is NOT RUN until a release with 1b is deployed.
- **The spike (done 2026-10-10):** the test monitor answers DDC/CI for power (off reads back as standby, but drops its hot-plug signal for about 250 ms), brightness and contrast, ignores input switching, has no CEC and no serial. Its runbook is the repo skill [display-control](../.claude/skills/display-control/SKILL.md).
- **Outcome:** the owner clicks Test: turn off on a Frame's Power tab and the display goes off, Turn on brings it back, and a display moved to another Frame brings its settings while that Frame stays not ready until its position is re-checked.
- **In scope:** B6, G5. Display identity from the EDID. Settings #110, #116 (without the Home Assistant smart plug), #117, #118, #120.
- **Out of scope:** when displays turn on and off (2a); brightness on the hardware (1c); the smart-plug method (3).
- **Acceptance:** CI tests `tests/node/display/test_display_power_methods.py` (with fake `cec-ctl` and `ddcutil`: each method sends the right command and reports "confirmed" or "didn't answer"; the blip after a DDC/CI standby writes nothing; a test ends on the Pi's own clock), `tests/test_display_identity.py` (DB: the same serial on another Output is the same Display and its settings follow; a display with no usable serial is recognised by make and model on its Frame, two identical ones on one Pi's two ports stay two Displays, an unbound port holds it pending, and an unplug or a blip keeps the Display recorded), `tests/test_display_changed_readiness.py` (DB: a changed display keeps the Frame not ready until Position is committed), `tests/test_display_power_requests.py`, `tests/integration/test_central_display_power.py` (a console Test reaches the Pi by notification), `tests/browser/test_frame_display_changed_browser.py`, `tests/browser/test_frame_power_browser.py`, and for q3 `tests/native_player_resource_harness.py`, `tests/node/health/test_app_absent.py`, the node-pid1 success scenario's Player crash step and `tests/test_central_silent_player.py`. Bench step: an agent runs Test off and on against the test Pi's monitor and fills in the support table, and checks the monitor stays in standby after Weston turns its Output back on.
- **Depends on:** 1a; a **design pass** for the Node bus power command.
- **Risks and unknowns:** answered by the spike and the build: the Pi 5 exposes each HDMI port's DDC bus as I2C once `i2c-dev` is loaded (no overlay); the command from Central is a written document, not a Node bus method. Still open: whether a monitor stays asleep once Weston turns its Output back on after a DDC/CI standby blip, and whether signal off works on a live Weston.
- **Size:** L. Split: spike and image tools; Display identity with G5; power methods and the Power tab.
- **Moment it proves:** the first half of 6: Test: turn off makes the test monitor report off (by CEC or DDC/CI), or the Pi reports its HDMI signal off.

### 1c. Brightness, contrast and colour

- **Status (2026-10-10):** built in one pull request ([#79](https://github.com/mcurcio/photo-wall/pull/79); owner, 2026-10-10: one pull request, no split). The design is [operator console design §5–§6](operator-console-design.md#5-the-frame-page), the Node side [Player architecture](player-architecture.md#picture-settings-1c) and the drawing [architecture › Player and rendering](architecture.md#player-and-rendering). The scope grew at the owner's gate ([gate page](https://claude.ai/artifact/GngF5gqPdD6f1PYayGHiZd)) to take in the Frame fit: every Frame uses **Cover**, with **Corners** as an explicit opt-in (q1); nothing is drawn outside a Frame's corners and the rest of the display is black (q2); Trim edges (#99) is removed (q3); any rotation angle in Cover; and every Frame's rotation reset to 0 when it moved to Cover.
- **Built:** the Display's four picture settings with where brightness and contrast act, set at the display's first report and moved only by the operator; the Output document's picture part with its own change number (required: the display line's schema major is 2); the Pi's picture controller (DDC/CI VCP 0x10 and 0x12, read back, one channel per port, newest wins); frame protocol version 4 carrying picture operations to the Player and the presented picture change back; the renderer's final pass with the masks last; the Picture tab on the Live model with Copy; the Position tab's fit and rotation; migration 075 moving every Frame to Cover and the old brightness (gain) to the Display; the V1 calibration route removed.
- **Not built:** Show grey ramp (B5); colour temperature and gamma on the display itself; two Frames bound to one display (Central binds one Frame per HDMI port; the renderer already masks each Frame). The bench steps below are NOT RUN until a release with 1c is deployed on the test Pi.
- **What it costs at deploy:** every Frame changes its picture and is **not ready until its Position is saved again**, and a Frame that is not ready shows no photos, so the wall shows no photos until each Frame's Position is re-checked ([operator console design §6](operator-console-design.md#6-display-identity)). A Frame that relied on a quarter turn needs it set again. Central and the Pis must run the 1c release together (no backwards compatibility).
- **Outcome:** moving Brightness on the Picture tab changes the test monitor's own backlight when it accepts DDC/CI, and the slider says "On the display"; otherwise it says "Photo Wall picture adjustment".
- **In scope:** B5, C3, and B4's fit. Settings #96–#99, #111–#114, #119.
- **Out of scope:** automatic dimming (#115, through Home Assistant later).
- **Acceptance** (corrected from the first draft, which asked for a fallback to picture adjustment when a write is refused and for software adjustment in Display Host; the accepted design reports a refused write and never switches a setting by itself, and the Player renderer is the only picture pipeline): CI tests `tests/node/display/test_display_picture_controls.py` (a fake DDC/CI device: brightness and contrast set and read back; a refused write reported, the setting left on the display; unset writes nothing; newest wins; one command at a time per port; the picture operations from the settings, colour temperature against Charity's blackbody table), `tests/test_native_math.py` (Cover the smallest unskewed rectangle of the Frame profile's aspect at 0, 7, 45, 90 and 135 degrees; Corners stretched exactly onto the corners; the final pass order; two Frames on one Output, black between them), `tests/native_renderer_pixel_harness.py` and `tests/native_display_smoke.py` (CI `display-harness`: the real renderer against that reference, black outside the corners at low contrast, frame protocol version 4), `tests/native_picture_e2e_harness.py` (CI `display-harness`: an Output document's brightness and contrast as picture adjustment go through the Pi's picture controller, the shell and the real Player to Weston's captured pixels, black outside the corners at contrast 25 %, and come back as the report's presented change), `tests/test_display_picture_settings.py` (DB: stored on the Display and following it, first-report placement, its own change number, Done, Revert, Copy never copying position, migration 075), `tests/test_frame_fit_settings.py` (DB: the fit through Save; a landscape Frame saves Cover at any angle and its profile, its orientation, is unchanged), `tests/test_live_session.py`, `tests/integration/test_central_display_picture.py` (a change reaches the Pi's document; the report places the settings), `tests/browser/test_frame_picture_browser.py` (the "where it acts" label; Done waits for the Pi; Copy copies display values, never position) and `tests/browser/test_frame_fit_browser.py` (Cover by default, Corners, the angle). Bench steps: an agent sets brightness from the console and reads it back with `ddcutil getvcp 10`; checks Cover at 7 and 90 degrees on the test monitor; whether contrast (VCP 0x12) changes its picture; DDC/CI write timing per drag; and the Player surviving a 0x10 write ([display-control](../.claude/skills/display-control/SKILL.md)).
- **Depends on:** 1b.
- **Risks and unknowns:** DDC/CI writes are slow and some monitors save each one to memory that wears, so the slider sends at most 2 per second (a starting rate the bench measures); whether the test monitor's contrast register changes its picture is unseen.
- **Size:** M as planned; the Frame fit added about a third.
- **Moment it proves:** none of the ten; it completes the owner's three questions.

### 4. Real photo sources

- **Status (2026-10-11):** built in one pull request ([#81](https://github.com/mcurcio/photo-wall/pull/81); owner, 2026-10-10: one pull request per delivery, no split), in one unattended overnight build with no owner gate (owner, 2026-10-11: "That all sounds correct. I’m hoping for one clean overnight build —plan the whole batch"). The owner's words for it, 2026-10-11: "create an immich query that becomes a photo pool". The design's twelve choices were taken as recommended and stay open to him; the pull request lists them, with its findings. How it works is the [media module](module-media.md) and the [media worker](module-media-worker.md); the key's home is [decision 0020](decisions/0020-immich-key-in-central-database.md).
- **Built:** connections kept in Central's database, added in Settings › Photo library with no file and no restart, the key written once and never read back; the deployment's connection set in Central's environment instead and shown read-only in Settings (owner, 2026-10-12: "The immich url and api key should be injected by iac"; [runbook](runbook.md#the-deployments-immich)); a connection check every 15 minutes, on Save and on Check now, with Immich's own counts and health over time as Needs a look with one fix each (on the page and on Needs attention), while the last pool keeps playing; the Photo source editor with Include (albums, people, a place, dates fixed or relative, favourites, tags, media types, partners) and Exclude (albums, people, tags, screenshots), archived, hidden and locked never included; drafts whose check really runs, so the pool fills page by page before and after saving; one pool per distinct query; checks that read Immich by keyset pages with no ceiling, subtract each exclusion as its own Immich query, add what they confirm and remove only when complete; a stack counted once, through its primary, with Immich's timeline deciding which photos are stacked copies; a check stopped part-way resumed by itself after 15 minutes, and a dead media worker's job handed back within about a minute (this also fixes an older gap: a worker killed mid-preparation used to stop all preparation); preparation from the photo as Immich shows it, edits included, at preview size when that covers the wall's largest Frame and full size otherwise; hand-placed photos from before 4 rewritten once so they still prepare; Show now, the Scene editor and the media pipeline showing each Source's check with Check now, and Why nothing new? stopping only when no Source of the Scene has photos; several Sources per Scene; the Planner reading every Source's whole pool with no candidate cap. The connections file, its volume, the unsaved-Source preview and the 1,000-match refusal are gone.
- **Not built:** #12 as a control (checks run every 15 minutes, fixed); #21 (On this day, moved to [5a](#5a-photo-fit-and-order)'s Order); #24 minimum rating and #33 blurry/duplicates (deferred); one photo reached through two connections counted once; paging the hand-placed photo picker. The Immich 3.1 fixture and the finding that a photo played at preview size on a Frame larger than its preview (full-size copies off) were closed by the follow-up to 5a ([media module](module-media.md#original-validation-and-preparation-recipe)). Settings counts are Immich's totals, archived photos included, so a Source of everything can show fewer. Requires an `mcurcio/iac` pull request after merge: the worker environment there still names the connections file ([runbook](runbook.md#connecting-immich)).
- **What it costs at deploy:** each existing connection becomes an empty row; until it has its address and key (the deployment's environment fills it at Central's first start, or they are pasted once in Settings), no new photos play and Frames keep their last photo. Existing Sources gain the screenshot exclusion, and a Source that asked for non-favourites only now selects all photos. Every photo is prepared once more (preparation recipe 2), and every edited photo becomes a new asset.
- **Outcome:** the deployment connects Immich (or the owner pastes it in Settings, no file, no restart), the owner sees the real counts, and builds "Anna and Ben, last 3 years, no screenshots", whose pool matches what plays.
- **In scope:** A3, D1, D2. Settings #8–#33, #36. Immich health over time: a rejected key or an unsupported Immich version shows as Needs a look with the fix.
- **Out of scope:** writing anything back to Immich.
- **Acceptance** (corrected from the first draft to the tests that landed): CI tests against one real Immich (`tests/integration/compose.immich.yml`, the `photo-sources` job of the software e2e, which replaced `immich-adapter`): `tests/test_immich_connection.py` (with a worker started before the connection exists: the fixture's exact photo, video, album and people counts with no file and no restart; the key in no route answer, job row, worker log or console file; a key missing `face.read`, a rejected key, another user's key and an unreachable address each in plain words, and the good key clearing them; newest wins under two Check now requests), `tests/test_source_filters.py` (each filter and each exclusion, archived, hidden and locked never, partners only when chosen, a stack only through a matching primary), `tests/test_source_no_ceiling.py` (at page size 3 the eight photos at one instant all join once; the pool is the include matches minus exclusions, stacked copies and partners' photos; a new photo joins and a deleted one leaves only at a complete check; an interrupted check removes nothing), `tests/test_preparation_rendition.py` (an edited photo fetched, thumbnailed and sized edited; the preview only when it covers the Frame: a strict expected failure on the fixture, whose Immich keeps no full-size copies, see Not built), `tests/test_source_preview_matches_plan.py` (every photo the draft shows prepares and is a Planner candidate; nothing else is assigned; two Sources on one Scene union). DB: `tests/test_library_store_conformance.py` (the PostgreSQL store and the tests' fake store held to one behaviour suite), `tests/test_migration_081.py` (hand-placed photos from before 4), `tests/test_managed_immich_connection.py` (the deployment's connection: set, kept across a restart, updated in place, refused by the API, a partial setting logged, released when unset), `tests/test_wall_demo.py`'s operator test. Browser: `tests/browser/test_photo_library_settings_browser.py`, `tests/browser/test_source_flow_browser.py` extended, `tests/browser/test_scene_flow_browser.py` extended, `tests/browser/test_show_now_browser.py` and `tests/browser/test_operator_showrunner_browser.py` (Show now's Saved Sources, the media pipeline, Why nothing new?). No bench step.
- **Depends on:** nothing. Placed right after 1c for visible value; the owner can move it back.
- **Risks and unknowns:** Immich's API changes between versions (a changed field reads as Needs a look, never as an empty pool); every check re-reads every match, about 100 requests per 100,000 photos every 15 minutes, and a big first check takes minutes (photos play from its first page); the `photo-sources` CI leg likely takes about 5 minutes, over the about-4-minutes-per-leg budget.
- **Size:** L, built as one pull request.
- **Moments it proves:** 2 (the counts test) and 5 (the preview-matches-plan test).

### 2a. Power: Night off and All off/on

- **Status (2026-10-12):** built with 2b in one pull request ([#86](https://github.com/mcurcio/photo-wall/pull/86); owner, 2026-10-11: "launch a design for 2a and 2b as one deliverable unit"); size L, not M + M. How it works is [0021](decisions/0021-power-timetable-and-repeating-schedules.md), the [Player architecture](player-architecture.md#display-identity-and-power-1b) (the Pi's side) and the [console design §7](operator-console-design.md#7-power); operating it is the [runbook](runbook.md#night-off-and-the-week). Central works out each HDMI port's next 7 days of off and on set times and the Pi switches on its own clock (owner, 2026-10-12: "Yes, send 7 days of set times", [overview gate](https://claude.ai/artifact/8DXMFAosB3HKY7m35fmHno)).
- **Outcome:** with Night off set, the test monitor goes off at its start and on at its end, the Frame reads Resting, and All off holds the displays off until the next scheduled power change.
- **In scope:** E2, F5. Night off, holds and the power timetable. Settings #80, #81 (clock and sun times), #82, #83, #90, #91. A hold carries its source (the console today), so 3 adds Home Assistant as another source without changing the model.
- **Out of scope:** Scene power requests (#70, with its Night off guard #89): owner, 2026-10-12: "Not now"; Home Assistant (3); a hold that lasts until a set time (#90's third choice).
- **Acceptance:** CI tests `tests/test_power_requests.py` (DB, controllable clock: All off holds each Frame until its own next change and the newest hold wins; Resume schedule; a double tap makes one hold; until resumed; a stale Night off save), `tests/test_power_timetable.py` (Night off's spans, the 7-day horizon, holds), `tests/browser/test_power_lane_browser.py` (bands, holds with who set them, Resume schedule; lands with the console part of #86), `tests/node/display/test_display_power_timetable.py` (the Pi switches at the set times by itself, a clock step, past the horizon nothing is written) and `tests/node/display/test_display_black_picture.py` (black picture, and black when no method answers), `tests/integration/test_central_night_off.py` (the Pi keeps Night off while Central is away). Bench step: an agent sets Night off two minutes ahead on the test Pi and confirms off, then on: **NOT RUN** until a release with #86 is on the Pi.
- **Depends on:** 1b; the U1 amendment (decided 2026-10-12).
- **Risks and unknowns:** a wrong Pi clock switches at the wrong time; after 7 days without Central the displays stay as they are; Central and every Pi must run the release together (the display line's schema 3).
- **Size:** L with 2b.
- **Moment it proves:** 6: at Night off's start the test monitor reports off within one minute, and at its end reports on within one minute.

### 2b. The week

- **Status (2026-10-12):** built with 2a in one pull request ([#86](https://github.com/mcurcio/photo-wall/pull/86)); size L with 2a, not M + M. How it works is [Runtime](module-runtime.md#house-time-and-repeating-programs) and the [execution contract](execution-contract.md#programs-repeat-a-default-scene-underneath); today's Programs end at deploy.
- **Outcome:** the owner makes "Evenings, Mon–Fri 17:00–23:00" as one repeating Schedule, sees it on the week calendar, and every Frame's Why is this playing names it.
- **In scope:** E1, E3, E5, E6, A4 (settings #2–#5, including the location sun times need), B3's names and room label (#92 on the one wall, #94). Settings #72–#76, #78, the sun-relative power times in #81.
- **Out of scope:** holidays and date ranges (E4, #77, deferred); several walls (6).
- **Acceptance:** CI tests `tests/test_program_recurrence.py` (DB, controllable clock: weekly rules across midnight and a daylight-saving change; the Default Scene fills gaps; a mid-window save keeps its end; Save and apply now), `tests/test_house_time.py` (daylight saving, sun times, polar days), `tests/test_why_playing.py` (every Frame's explanation names the winner and what it beat, a window kept out by protected Frames), `tests/test_week.py` (the week's data across the autumn change), `tests/browser/test_schedule_flow_browser.py` extended, `tests/browser/test_frame_why_browser.py` and `tests/browser/test_house_settings_browser.py` (the last two land with the console part of #86). Today's Programs end at deploy (they were timed in the browser's zone, which Central never recorded). No bench step.
- **Depends on:** 2a (the Power lane shares the page).
- **Risks and unknowns:** today's separate Programs are not migrated: the owner remakes the routine and picks a Default Scene after deploy; sun times use `astral`, in Central only.
- **Size:** L with 2a.
- **Moment it proves:** 7: at any test-clock moment, each Frame's Why is this playing names the Schedule or Default Scene that won and the one it beat.

### 3b-i. Home and phone

- **Outcome:** signed in with his own account on a phone, the owner sees one status word per Frame, skips a photo, puts one on the Never show list, pauses a Frame for an hour, restarts a stuck app, and starts Show now with an end time.
- **In scope:** A2 (settings #135, #138), G1, G2, G3 (Restart app with progress), F2, F3 (with the Never show list page, #71), F4 (#84, #85), F6, F8 (#86). The six status words, the problems strip and the error template. "Getting ready" shows per-Frame progress after a power cut (U2).
- **Out of scope:** the live picture of what each Pi presents (3b-ii); notifications (7); household roles (deferred).
- **Acceptance:** CI tests `tests/browser/test_home_browser.py` (each of the six words from fixture states; a 390-pixel phone viewport has no sideways scroll; every problem names display, Pi, app, network or Central), `tests/browser/test_show_now_browser.py` extended (end time; Back to normal reveals the schedule's current state), `tests/test_skip_never_show.py` (DB: Skip and Never show act on unsecured assignments; on secured ones Never show starts with the next Run and Skip is not offered; a Never show photo is not assigned again), `tests/test_sign_in.py` (DB: the owner account replaces the environment token; session length). Bench step: an agent runs Restart app on the test Pi and watches the progress reach "back".
- **Depends on:** 2a, 2b (names); **owner decisions: the U1 amendment**, and what Pause does.
- **Risks and unknowns:** sign-in replaces the token every script and test uses today.
- **Size:** L. Split: sign-in; Home with status words and problems; tile actions, Pause and Show now.
- **Moments it proves:** 8: in the phone viewport, Never show is two taps, and the photo is never assigned again in the planner test; 9: for each fixture fault, the problem names the failing part in plain words.

### 3b-ii. The live map

- **Outcome:** each Home tile shows a thumbnail of what its Pi reports it is presenting, and it changes within about 2 seconds of a Skip.
- **In scope:** F1. Setting #106.
- **Out of scope:** proving what the panel lights up (the Pi reports what it presents, not what is visible).
- **Acceptance:** CI tests `tests/integration/test_node_bus_presenting.py` (the Player reports the presented asset; Central serves its thumbnail; the browser never gets an Immich address), `tests/browser/test_home_live_map_browser.py`. Bench step: an agent skips on the test Pi and times the tile change.
- **Depends on:** 3b-i.
- **Risks and unknowns:** needs a new Node bus fact for "what is presented", not yet designed in [0017](decisions/0017-node-redesign-r3.md).
- **Size:** M.
- **Moment it proves:** none alone; it makes 9's "what the Pi presents" visible.

### 5a. Photo fit and order

- **Status (2026-10-11):** built in one pull request ([#82](https://github.com/mcurcio/photo-wall/pull/82); owner, 2026-10-10: one pull request per delivery, no split), stacked on 4's, in one overnight build (owner, 2026-10-11: "I’m hoping for one clean overnight build —plan the whole batch"). The owner's words for it, 2026-10-11: "how to apply that pool onto frames, making sure that photos are only assigned to compatible frames and there are never duplicates down at the same time". The design was accepted at the [gate page](https://claude.ai/artifact/LgXHkE9hyev7Y2bSn7z6kk) ("That all sounds correct"). How it works is the [Planner](module-planner.md#the-photo-fit-rule) (the fit rule, order and passes, the wall plan), the [execution contract](execution-contract.md#what-a-frame-shows-its-crop-and-when-a-photo-is-on-screen) (the crop and the on-screen extent) and the [media worker](module-media-worker.md#key-content) (key content); the requirements are [compatibility](requirements.md#live-media-compatibility-and-preparation).
- **Built:** **fill or don't show**: a photo goes only on a Frame it fills, judged by the ratio of its sides, never by the labels landscape and portrait (owner, 2026-10-11: "It’s about the ratio, not strict labels"); one photo per Frame, with no mats, blur or pairs. **Smart crop**: the media worker measures each photo's key content once, Immich's face boxes and an interest grid from the small thumbnail; the fit rule keeps every face whole and the interesting part, refuses a crop beyond the Scene's Crop limit or below the Frame's pixels, and puts the crop on the layer; the Player draws it, and preparation never crops. Without Immich's face detection, crops ignore faces, with a note on the connection. **Never on two Frames at once**: Central plans every Frame together an hour ahead (owner, 2026-10-11: "I suggest an algorithm that computes all frame+photo assignments well in advance"), and never puts one photo on two Frames whose time on screen overlaps, kept stills and hand-overs included, hand-placed photos too. **Order and no repeats**: each Scene shuffles by default (or oldest first, newest first, On this day) and reshuffles once every photo has had its turn; a photo shown recently ranks lower, but a Frame never goes blank because of it, and keeps its previous photo when no other is free. "Shown" means a Player reported it displayed. The Scene editor's Order, Don't repeat within and Crop limit rows; the Frame page's Photos counts with the reason each photo is skipped there.
- **Not built:** favourite weighting (#45) and keep events together (#46); key content in videos (a video takes a centred window); slow pan and zoom (#60); a per-Frame crop limit; the plan reacting to a Player's failed draw. Open findings: a 4:3 photo of 8 MP cropped for a 4K 16:9 Frame is below the Frame's pixels and does not show there; a wide panorama prepared at its 3,840-pixel long edge can show below a 4K Frame's pixels; dates for On this day are the house date since [2b](#2b-the-week) (resolved). The face padding (10 %), the smallest face (0.2 %) and the "how much interest a crop may cut" limit (1.75 times its share of the area) are placeholders to tune on the owner's library; the first and last were loosened on 2026-10-12 from 20 % and 1.25 after the owner found the defaults excluded many photos.
- **What it costs at deploy:** no backwards compatibility: a Player before 5a refuses plans that carry a crop, so Frames show nothing new between Central's deploy and each Pi's reboot onto the 5a release. A new pool shows nothing new for the seconds its photos take to measure and prepare; Frames keep what they show meanwhile. Fewer photos fit: portrait-heavy libraries show far fewer photos on landscape Frames, and older libraries fewer on 4K Frames.
- **Outcome:** on a wall of landscape and portrait Frames, every photo fills its Frame with its faces and subject kept, two Frames of one shape never show the same photo at once, and Scenes shuffle instead of showing newest first.
- **In scope:** D3, D4. Settings #21 (On this day, an order since 4), #43, #44, #48 (the Crop limit, replacing the fit modes), #103 (strict, with the skipped count). Deleted at the owner's answers of 2026-10-11: #47 (sync), #49–#52 (mats, portraits on landscape, the face-aware switch), #102 (fit override per Frame) and #105 with C5 (Span; owner: "Yes").
- **Out of scope:** video and captions (5b); slow pan and zoom (#60).
- **Acceptance:** CI tests `tests/test_photo_crop.py`, `tests/test_on_screen_extent.py`, `tests/test_key_content_model.py` and `tests/test_photo_fit.py` (the 4:3-on-16:9 boundary judged by content, 2:3 on 16:9 refused, faces at the edge kept or refused, a 1.05 : 1 photo on a 0.95 : 1 Frame, the pixel floor, videos), `tests/test_key_content.py` (recorded v2.5.6 face answers; thumbnails drawn in the test), `tests/test_key_content_store.py`, `tests/test_planner_order.py` (a pass reshuffles; a refresh keeps order; a repeat rather than nothing), `tests/test_shown_history.py` (DB: written on the presented report; idempotent; a failed report records nothing), `tests/test_planner_wall_assignment.py` (the whole hour in one call; a property test across re-plans: no photo's extents overlap on two Frames and locked layers never change; every photo that fits is eventually shown; order, then least crop; a hand-placed photo stays put), `tests/test_coordination.py` (DB: two Frames of one shape show different photos and the offered layers carry crops), `tests/test_wall_plan_tracer.py` (end to end: through the real Planner, the fit rule and the Player's reference, then through coordination with the media worker measuring a new photo and a presented report ranking it later), `tests/test_migration_084.py` (stored layers from before 5a read back with the centred window they showed), `tests/test_native_math.py` and the pixel harness's crop case (`tests/native_renderer_pixel_harness.py`, CI `display-harness`: the left side kept), `tests/test_immich_faces.py` (the `photo-sources` leg, real Immich), `tests/browser/test_scene_flow_browser.py` extended (Order, Don't repeat within, Crop limit) and `tests/browser/test_frame_photo_fit_browser.py` (the Frame page's reasons). No bench step.
- **Depends on:** 4. D13's visible history was decided for it on 2026-10-11 ([design decisions](design-decisions.md)).
- **Risks and unknowns:** a new dependency, OpenCV's contrib build for its saliency module, adds about 138 MB plus numpy's 24 MB to the Central and media worker image (measured 2026-10-11; about 20 lines of numpy could replace it); the hour is re-planned every second across every Frame, a cost that grows with the pools (about 0.65 s per pass for 8 Frames, an hour and 10,000 photos on an Apple-silicon Mac, the Planner alone; [Planner](module-planner.md#the-wall-plan)); fewer photos fit (above); Immich's face quality on the owner's library is unproven.
- **Size:** L, built as one pull request.
- **Moment it proves:** 4: every planned photo fits its Frame, and no photo is on two Frames at once.

### 5b. Video, captions and transitions

- **Outcome:** videos play muted, capped at 30 seconds, at most a tenth of the slideshow; captions show date and place when turned on; crossfade, cut or slide is chosen per Scene.
- **In scope:** D5's rest (#40, #42), D6 (#53–#55), D7 (#56–#59).
- **Out of scope:** clock and weather overlays (#61), burn-in protection (#68).
- **Acceptance:** CI tests `tests/test_planner_video.py` (share and cap), a Player test that renders caption text, `tests/browser/test_scene_flow_browser.py` extended. No bench step.
- **Depends on:** 5a.
- **Risks and unknowns:** the Player has no text rendering today (fonts in the image, a text overlay stage); memory on a 4 GB Pi.
- **Size:** M.
- **Moment it proves:** none.

### 3. Home Assistant

- **Outcome:** Home Assistant shows one device per Frame with a power switch, status and Skip, and a shipped motion Blueprint wakes a room's displays.
- **In scope:** F7. Settings #69, #87 and #88 (the Home Assistant power guards, with their controls on the Power lane), #141–#144; the device and entity map, the request topic and Blueprints ([design §8](operator-console-design.md#8-home-assistant)).
- **Out of scope:** a dedicated Home Assistant integration; native sensors.
- **Acceptance:** CI test `tests/integration/test_home_assistant_mqtt.py` against a Mosquitto container (discovery announcements kept by the broker with stable ids; a switch becomes a power request under the hold rule; Remove clears them; a broker restart re-announces). Bench step: an agent points a throwaway Home Assistant container at the broker and checks the devices appear.
- **Depends on:** 2a (power requests), 2b (Frame names and room label), 3b-i (status words, Show now with an end time).
- **Risks and unknowns:** broker credentials stored on the server; guard defaults untested in a real home; Scene power waits on the owner.
- **Size:** L. Split: connection and discovery; controls and readings; inputs and Blueprints.
- **Moment it proves:** none of the ten.

### 6. Setup and hardware life

- **Outcome:** on a fresh install, the setup checklist takes the owner from an empty Central to photos on a newly plugged Pi, and a replacement Pi is bound with one tap.
- **In scope:** A1, A5 (the network-boot self-test and router help, setting #130), B1 (the code on the display), B3's several walls, B7, C1, C2 (#104), G4's New Pis waiting, G10's Retire a Pi, the "needs 4 GB" refusal shown in the console ([requirements](requirements.md#supported-player-hardware)). Settings #7, #93, #107 (bulk actions on selected Frames), #121.
- **Out of scope:** roles.
- **Acceptance:** CI tests `tests/browser/test_setup_checklist_browser.py` (each step turns green only from a real check), `tests/test_new_pi_binding.py` (DB: one tap binds; the Frame keeps position, Display settings and Schedules), `tests/test_surfaces.py` (DB: several walls), and a check that a refused 2 GB board shows its reason (extending the existing node memory tests). Bench step: an agent clears the test Pi's binding, power-cycles it and times its appearance.
- **Depends on:** 1b (the display code), 2b (names), 4 (the checklist's Immich step and B7's Favourites slideshow need the Immich page).
- **Risks and unknowns:** several walls is a domain change: a Surface is a bare text label today, so walls need a stored record (M on its own); routers differ in how they hand out network-boot settings.
- **Size:** L. Split: checklist and network boot; New Pis waiting and replace; several walls and groups.
- **Moments it proves:** 1: a netbooted test Pi appears in the console within 60 seconds of power-on and its display shows the same code; 10's first half: Replace with… binds a new Pi in one action and every Frame setting survives.

### 7. Peace of mind

- **Outcome:** Central backs itself up nightly and before every upgrade, a restore shows what it will change, Pi logs and updates are one click, and the owner is told only when a Frame stays Not showing for 15 minutes.
- **In scope:** G6 (settings #125–#127, and the gate the console cannot open today), G7 (#139, #140), G8 (#124), G9 (#131–#134), G10's reset, G11, the Central cache limit (#129), and current photo and Pi readings in Home Assistant (#143).
- **Out of scope:** notification channels beyond the basics; change history.
- **Acceptance:** CI tests `tests/test_backup_restore.py` (DB: restoring last night's backup into an empty Central gives back every Frame, Display, Scene and Schedule), `tests/test_upgrade_backup_first.py` (migrations do not run until a backup succeeded), `tests/test_notifications.py` (one alert after 15 minutes of Not showing in scheduled hours; none for Resting or self-healed blips), `tests/browser/test_pi_logs_browser.py`. Bench step: an agent downloads the test Pi's logs from the console.
- **Depends on:** 3b-i (status words drive notifications).
- **Risks and unknowns:** until 7 lands, an upgrade runs forward-only migrations with no automatic backup, so the deploy path must take one by hand.
- **Size:** L. Split: backups and safe upgrade; notifications; logs, updates and cache.
- **Moment it proves:** 10's second half: the restore test above.

## The ten moments of truth

These decide whether a power user keeps Photo Wall. Each is stated so a test can check it; the delivery that proves it runs that test.

| # | Moment | Checked by | Delivery |
|---|---|---|---|
| 1 | A Pi plugged into a display shows up with its name on screen and in the console within a minute, with no SSH | A netbooted test Pi appears within 60 s and shows the same code (bench step) | [6](#6-setup-and-hardware-life) |
| 2 | Connecting Immich shows real counts and thumbnails | Test shows the fixture library's exact counts and thumbnails | [4](#4-real-photo-sources) |
| 3 | Dragging a corner moves the picture on the real display right away, and Revert works | Done unlocks only after the latest change is acknowledged as presented; Revert restores the saved corners | [1a](#1a-the-frame-page) |
| 4 | Every photo fills its Frame with its faces and subject kept: a portrait never shows as a strip on a landscape display, and two Frames never show the same photo at once | Every photo the wall plan places fits its Frame by the fit rule, and no photo's on-screen extents overlap on two Frames (`tests/test_wall_plan_tracer.py`, `tests/test_planner_wall_assignment.py`) | [5a](#5a-photo-fit-and-order) |
| 5 | The Photo source preview is exactly what will play | Every assigned asset is in the preview set | [4](#4-real-photo-sources) |
| 6 | Night off really turns the displays off, and they come back on in the morning | The test monitor reports off within a minute of Night off starting and on within a minute of it ending | [1b](#1b-display-identity-and-power), [2a](#2a-power-night-off-and-all-offon) |
| 7 | "Why is this showing?" has an answer for every Frame | At any test-clock moment each Frame names the winner and what it beat | [2b](#2b-the-week) |
| 8 | Putting a photo on the Never show list from a phone takes two taps, and it never comes back | Two taps in a phone viewport; never assigned again | [3b-i](#3b-i-home-and-phone) |
| 9 | A black screen is explained in plain words that name the failing part: display, Pi, app, network or Central | Each fixture fault names its part | [3b-i](#3b-i-home-and-phone) |
| 10 | Replacing a Pi is one click and keeps every setting; backup and restore does the same for the whole house | The binding test and the restore test | [6](#6-setup-and-hardware-life), [7](#7-peace-of-mind) |

## Placed elsewhere

Requirements and needs that are not journey steps, and where each goes:

| Item | Where |
|---|---|
| Hand-placed Scenes (#38) | Already exist; kept as they are |
| Activation priority (#78) | [2b](#2b-the-week), as three named levels |
| U2 recovery after a power cut ([requirements](requirements.md#failure-visibility-and-recovery)) | Per-Frame progress in [3b-i](#3b-i-home-and-phone); the timing itself is qualified under the [validation guide](validation.md), not here |
| The "needs 4 GB" refusal ([requirements](requirements.md#supported-player-hardware)) | [6](#6-setup-and-hardware-life) |
| Central upgrade with a backup first | [7](#7-peace-of-mind) (G11) |
| Retire a Pi; reset Central | [6](#6-setup-and-hardware-life); [7](#7-peace-of-mind) |
| Router network-boot setup, setting #130 | [6](#6-setup-and-hardware-life) |
| Immich health over time (key rotation, version changes) | [4](#4-real-photo-sources) |
| Central cache limit #129, releases kept on Central #128 (a fixed constant until then), Pi logs #124 | [7](#7-peace-of-mind) |
| At a Schedule's end, stop now (#79): not built; a Program's end always finishes gracefully | Waits until a delivery picks it up |
| U1's open fault classes (compositor, graphics, kernel, panel, power, before the display starts; [requirements](requirements.md#failure-visibility-and-recovery)) | Outside this roadmap: owned by the Node programme ([Player architecture](player-architecture.md)). Cost: a compositor or panel fault can still leave a display dark with no explanation on it, so moment 9 holds only for faults the console can see |
| Nice-to-have catalogue rows no delivery names: #1 house name, #6 language, #60 slow pan and zoom, #61 clock and weather, #137 single sign-on | Wait until a delivery picks them up |

## Deferred, and what that costs

| Deferred | Cost while it waits |
|---|---|
| Holidays and date ranges (E4, #77) | Christmas photos need a Schedule turned on and off by hand |
| Repeated activation choices (#66) | Starting a playing Scene again is ignored (the requirements' default) |
| The reference experiences beyond the slideshow and Good night: Talking portraits, Haunted portraits overlay, Calendar change beneath an overlay, Coordinated readiness ([requirements](requirements.md#reference-experiences)) | The wall does slideshows, Show now takeovers and Night off; theatrical, choreographed effects wait, though Central's endings and protection got controls in [1x](#1x-expose-what-already-exists) and child Scenes get theirs in its follow-up |
| Several Installations ([requirements](requirements.md#operations-and-scope)) | One House per Central |
| Household and guest roles | Everyone who signs in can change everything; a guest cannot be given Skip and Pause alone |
| Notification channels beyond the basics | Alerts reach only the channels 7 builds |
| Change history | Only the 10-second Undo after a change; no record of who changed what, and no Revert later |
| Native sensors and auto-dim | Room-light brightness and motion need Home Assistant |
| Lights and other non-display controls | Scenes cannot drive lights or relays |
| Burn-in protection (#68) | The owner has OLED panels; a static element shown for hours (captions from 5b, a paused photo) can burn in. Until it lands, keep captions on auto-hide (#59) and Pause short |
| Per-Pi software pins ([console DDD Part E](operator-console-ddd.md#part-e-v2-only-console-and-node-release-workflows-feature-layer)) | The whole wall runs one selected release |

What the design itself gives up is listed in [the operator console design](operator-console-design.md#13-what-this-design-gives-up).

## Settings status

The [settings catalogue](operator-console-design.md#11-settings-catalogue) listed 144 settings a power user expects, each with one home. On 2026-10-09, after [1x](#1x-expose-what-already-exists), 34 exist in the console (some in a poor form: one Photo source per Scene, one schedule window, a position editor that times out, size editable only while unbound, software brightness labelled "draft"), 1 is built in Central with no control in the console (#67 child Scenes, 1x's follow-up), and 109 are missing (1x's re-check moved #79 and #128 here). On 2026-10-10, after [1b](#1b-display-identity-and-power): 38 exist (#116 without the smart plug, #117, #118 and #120 added; #118 cannot act on 1b's methods), 1 is built with no control, and 105 are missing (#110 shows the display's modes but cannot be set). On 2026-10-10, after [1c](#1c-brightness-contrast-and-colour): #99 (crop per edge) is removed, so the catalogue lists 143; 41 exist (#112, #113, #114 and #119 added; #111 was already counted as the software brightness, and #99 counted as existing before it was removed), 1 is built with no control, and 101 are missing. On 2026-10-11, after [4](#4-real-photo-sources): 57 exist, 1 is built with no control, and 85 are missing. Of the 27 rows 4 covers (#8–#33, #36), 23 exist: before 4, #13, #14, #19, #22, #23, #25 (without live photos) and #36 (one Source per Scene) were counted; 4 added the other 16, counting #17 (all, fixed) and #29 and #30 (one fixed line) as existing. #12 checks every 15 minutes with no control, so, like #128, it stays missing; #21 moved to 5a; #24 and #33 are deferred. On 2026-10-11, after [5a](#5a-photo-fit-and-order): #47, #49–#52, #102 and #105 are removed (none of them existed), so the catalogue lists 136; 62 exist (#21, #43, #44, #48 as the Crop limit, and #103 as the strict floor shown with its skipped count, counted like #17's fixed rule), 1 is built with no control, and 73 are missing (#45 and #46 among them). Update these counts when a delivery lands.
