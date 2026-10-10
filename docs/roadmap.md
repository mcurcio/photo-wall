# Photo Wall roadmap

**Status:** as recorded in [decision 0019](decisions/0019-first-principles-console.md).

This is the product roadmap. It walks the whole life of a Photo Wall install the way a power user lives it, from before any hardware arrives to fixing things years later, and orders the work by what the owner can do on the real wall after each delivery. Nothing in it is built until its delivery says so.

The owner, 2026-10-09 (chat), on the first-principles console review: "I LOVE the Journey section of that doc. I want those ideas codified as the roadmap." The review page is [Console Setup Review](https://claude.ai/artifact/EwjdvEcM82HyLDb4BrDSdC).

Where things are defined:

- The console's target design (pages, the Frame page, power, Home Assistant, the settings catalogue) is the [operator console design](operator-console-design.md); the decisions and what they replace are [decision 0019](decisions/0019-first-principles-console.md).
- This roadmap owns four things the others link to: each step's status today, the delivery order with each delivery's scope and acceptance, the moments of truth, and the deferred list.
- What the owner asked for is in [requirements › Operator experience](requirements.md#operator-experience).
- The engineering slice order for the first MVP is the [implementation plan](implementation-plan.md); this roadmap orders product capabilities on top of it. Each delivery is split into pull requests when it starts.

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
| A3 | Connect Immich | Paste URL and API key, Test: "41,203 photos, 212 albums, 96 people" | M | missing | Hand-written JSON file in the media worker's volume, then a restart | [4](#4-real-photo-sources) |
| A4 | House basics | Timezone, location for sunrise and sunset, units | M | missing | Not settable | [2b](#2b-the-week) |
| A5 | Ready for Pis | A "network boot OK" self-test and help for the router's network-boot setting; Pi 5 with 4 GB or more stated up front | S | missing | A fresh install refuses every boot until a release is picked | [6](#6-setup-and-hardware-life) |

### B. The first Pi and display

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| B1 | Pi appears | Plugged in, it shows up within a minute and the display shows its code | M | exists | Appears under Hardware; no on-screen code | [6](#6-setup-and-hardware-life) |
| B2 | Identify | The display flashes its name and a border | M | exists | "Identify Panel" on the Binding tab | [1a](#1a-the-frame-page) |
| B3 | Name and place | "Living room left", pick the wall | M | missing | A code-style Frame id; one wall only | [2b](#2b-the-week) (names, room label); [6](#6-setup-and-hardware-life) (several walls) |
| B4 | Fit the picture | Test pattern; drag corners, rotate, crop, nudge; live on the display; Done/Revert | M | exists | Hidden under Calibration; a 30-second session | [1a](#1a-the-frame-page) |
| B5 | Picture quality | Brightness, contrast, colour temperature, gamma, grey ramp | S | exists | Software brightness only, labelled "draft"; no contrast or colour | [1c](#1c-brightness-contrast-and-colour) |
| B6 | Power | Detect how this display can be switched; Test off/on | M | missing | Nothing | [1b](#1b-display-identity-and-power) |
| B7 | First photos | A Favourites slideshow starts by itself | M | missing | Build a Source, a Scene and a Program by hand | [6](#6-setup-and-hardware-life) |

### C. More screens

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| C1 | Wall layout | Drag scaled rectangles in real cm or inches onto each wall | S | exists | One wall; millimetres typed by hand | [6](#6-setup-and-hardware-life) |
| C2 | Rooms and groups | Every room is a group; act on groups | M | missing | No groups | [6](#6-setup-and-hardware-life) |
| C3 | Copy settings | Copy picture settings to other Frames | S | missing | Nothing | [1c](#1c-brightness-contrast-and-colour) |
| C4 | Two displays per Pi | Both HDMI ports listed, each feeding its own Frame | S | exists | Possible through Binding | already met |
| C5 | Span | One photo across neighbouring Frames | N | missing | Nothing | deferred |

### D. Content

| # | Step | What the user expects | P | Today | Now | Delivery |
|---|---|---|---|---|---|---|
| D1 | Pick photos | Albums, people, places, dates, favourites; live count and thumbnails | M | exists | Tags, dates and favourites only; no albums or people | [4](#4-real-photo-sources) |
| D2 | Exclude | Exclude people and albums; archived, hidden and screenshots excluded by default | M | missing | Nothing | [4](#4-real-photo-sources) |
| D3 | Photo fit | Blurred mat, colour mat, fill; portraits paired on landscape displays | M | missing | Nothing | [5a](#5a-photo-fit-and-order) |
| D4 | Order | Shuffle with no repeats, chronological, On this day | M | missing | Newest first, fixed | [5a](#5a-photo-fit-and-order) |
| D5 | Timing and transitions | Seconds per photo, crossfade, stagger between Frames | M | built, no controls | Seconds per photo only; fades exist without controls | [1x](#1x-expose-what-already-exists) (length), [5b](#5b-video-captions-and-transitions) (type, stagger) |
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
| G5 | Display changed | Picture and power follow the display; the Frame is not ready until its position is re-checked ([requirements](requirements.md#installation-model)) | M | missing | The display profile is editable only while unbound | [1b](#1b-display-identity-and-power) |
| G6 | Updates | New releases pre-download; you click Apply | M | exists | Exists behind a gate the console can't open | [7](#7-peace-of-mind) |
| G7 | Backup and restore | Download, a nightly copy, restore with a preview | M | missing | Nothing | [7](#7-peace-of-mind) |
| G8 | Logs | Per-Pi logs without SSH | S | missing | SSH only | [7](#7-peace-of-mind) |
| G9 | Notifications | Told when a Frame stays Not showing, and not otherwise | S | missing | Nothing | [7](#7-peace-of-mind) |
| G10 | Retire and reset | Retire a Pi; reset or uninstall Central cleanly | S | exists | Retire a Pi exists; no reset | [6](#6-setup-and-hardware-life) (Retire), [7](#7-peace-of-mind) (reset) |
| G11 | Upgrade Central | A new Central version installs with a backup taken before its database changes | M | missing | Migrations run at start-up with no backup | [7](#7-peace-of-mind) |

G6 follows the existing release choices in [console DDD Part E](operator-console-ddd.md#part-e-v2-only-console-and-node-release-workflows-feature-layer), under [requirements › Player provisioning](requirements.md#player-provisioning).

## Delivery order

**1a → 1x → 1b → 1c → 2a → 2b → 3b-i → 3b-ii → 4 → 5a → 5b → 3 → 6 → 7.** Home Assistant (3) comes after the slideshow work because it needs Frame names, status words and Show now with an end time first, and the owner's priority is a visibly working wall.

| # | Delivery | Size | Outcome | Depends on |
|---|---|---|---|---|
| [1a](#1a-the-frame-page) | The Frame page | M | Set a Frame's position from its own page, with no timeout | none |
| [1x](#1x-expose-what-already-exists) | Expose what already exists | S | Every setting Central or the Pi can already do has a control | none |
| [1b](#1b-display-identity-and-power) | Display identity and power | L | Turn a display off and on from the console; its settings follow it | 1a |
| [1c](#1c-brightness-contrast-and-colour) | Brightness, contrast and colour | M | Brightness and contrast act on the display where it allows | 1b |
| [2a](#2a-power-night-off-and-all-offon) | Power: Night off and All off/on | M | Displays go off at 23:00 and on at 07:00 | 1b; **owner: U1 amendment** |
| [2b](#2b-the-week) | The week | M | One repeating Schedule; every Frame says why it plays | 2a |
| [3b-i](#3b-i-home-and-phone) | Home and phone | L | Check and act on the wall from a phone, signed in | 2a, 2b |
| [3b-ii](#3b-ii-the-live-map) | The live map | M | Each tile shows what its Pi presents | 3b-i |
| [4](#4-real-photo-sources) | Real photo sources | L | Connect Immich and build a real Photo source in the console | none |
| [5a](#5a-photo-fit-and-order) | Photo fit and order | L | No cropped portraits, no bare letterbox, shuffle | 4; **owner: D13** (for no-repeat) |
| [5b](#5b-video-captions-and-transitions) | Video, captions and transitions | M | Muted, capped videos; captions on request | 5a |
| [3](#3-home-assistant) | Home Assistant | L | Frames as Home Assistant devices; motion wakes a room | 2a, 2b, 3b-i |
| [6](#6-setup-and-hardware-life) | Setup and hardware life | L | Fresh install to photos, and a Pi swap, without help | 1b, 2b |
| [7](#7-peace-of-mind) | Peace of mind | L | Backups, safe upgrades, notifications, logs | 3b-i |

### Owner decisions the order waits on

| Decision | Blocks | Recommendation |
|---|---|---|
| Amend U1 so intentional operational darkness (the power schedule, Pause, All off, a display powered off on purpose) counts as intended and shows as Resting | 2a, 3b-i | Amend ([design §12](operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)) |
| Allow a Scene to ask for its displays on or off (operational state in authored content) | Settings #70 and #89 only; nothing else waits | Confirm or drop ([design §7](operator-console-design.md#7-power)) |
| D13, visible-history accounting ([design decisions](design-decisions.md)) | 5a's "don't repeat within N days" (#44) | Decide before 5a starts |
| What Pause does to the screen (freeze the photo, or stand aside) | F8 in 3b-i | Freeze the current photo |

### 1a. The Frame page

- **Outcome:** from the wall plan, the owner clicks a Frame, drags a corner on its Position tab, and the change shows on the display with Done and Revert, with no 30-second timeout.
- **In scope:** B4, B2 (Identify in the page header), the existing software brightness moved to the Picture tab (B5's home), Replace with… on Hardware (G4's first half). Settings #96–#101, #108, #109 (editable while bound), #122, #123.
- **Out of scope:** hardware brightness, contrast and power (1b, 1c); the Home page (3b-i); New Pis waiting (6).
- **Acceptance:** CI tests `tests/browser/test_frame_page_browser.py` (open a Frame from the plan; drag and arrow-nudge a corner; Done stays disabled until the presentation acknowledgement for the latest change arrives; Revert restores the saved corners; the session stays open after 60 s idle on a test clock), and a DB test `tests/test_calibration_session_keepalive.py` (the page's keep-alive holds the session; it ends 2 minutes after the page closes). Bench step: an agent drags a corner against the test Pi and reads Display Host's presentation acknowledgement; visible pixels are not claimed.
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
- **Size:** S.
- **Moment it proves:** none; it answers the owner's first ask.

### 1b. Display identity and power

- **Starts with a spike:** add CEC (`cec-ctl`) and DDC/CI (I2C plus `ddcutil`) to the Pi image, with any kernel overlay the HDMI DDC bus needs. The test Pi has `/dev/cec0` and `/dev/cec1` but no `/dev/i2c-*`. Output: a support table per display on hand (the test Pi's portable monitor first): power off and on, input switch, brightness, contrast, each by CEC, DDC/CI or neither.
- **Outcome:** the owner clicks Test: turn off on a Frame's Power tab and the display goes off, Turn on brings it back, and a display moved to another Frame brings its settings while that Frame stays not ready until its position is re-checked.
- **In scope:** B6, G5. Display identity from the EDID. Settings #110, #116 (without the Home Assistant smart plug), #117, #118, #120.
- **Out of scope:** when displays turn on and off (2a); brightness on the hardware (1c); the smart-plug method (3).
- **Acceptance:** CI tests `tests/node/display/test_display_power_methods.py` (with fake `cec-ctl` and `ddcutil`: each method sends the right command and reports "confirmed" or "didn't answer"), `tests/test_display_identity.py` (DB: the same EDID on another Output is the same Display and its settings follow; no usable identity makes a new Display), `tests/test_display_changed_readiness.py` (DB: a changed display keeps the Frame not ready until Position is committed), `tests/browser/test_frame_power_browser.py`. Bench step: an agent runs Test off and on against the test Pi's monitor and fills in the support table.
- **Depends on:** 1a.
- **Risks and unknowns:** whether the Pi 5 kernel exposes the HDMI DDC bus as I2C at all; many monitors ignore CEC, so the portable monitor may support only "HDMI signal off"; the Pi does not report EDID today, and the command from Central to the Pi (a new Node bus method) is not designed; the image changes go through the image layers, not hand-built packages.
- **Size:** L. Split: spike and image tools; Display identity with G5; power methods and the Power tab.
- **Moment it proves:** the first half of 6: Test: turn off makes the test monitor report off (by CEC or DDC/CI), or the Pi reports its HDMI signal off.

### 1c. Brightness, contrast and colour

- **Outcome:** moving Brightness on the Picture tab changes the test monitor's own backlight when it accepts DDC/CI, and the slider says "On the display"; otherwise it says "Photo Wall picture adjustment".
- **In scope:** B5, C3. Settings #111–#114, #119.
- **Out of scope:** automatic dimming (#115, through Home Assistant later).
- **Acceptance:** CI tests `tests/node/display/test_display_picture_controls.py` (a fake DDC/CI device: brightness and contrast set and read back; fallback to picture adjustment when refused; software contrast, colour and gamma applied in Display Host), `tests/browser/test_frame_picture_browser.py` (the "where it acts" label; Copy to other Frames copies display values, never position). Bench step: an agent sets brightness from the console and reads it back with `ddcutil getvcp`.
- **Depends on:** 1b.
- **Risks and unknowns:** software contrast, colour and gamma need new Display Host shader work; DDC/CI writes are slow, so the slider must send at a limited rate.
- **Size:** M.
- **Moment it proves:** none of the ten; it completes the owner's three questions.

### 2a. Power: Night off and All off/on

- **Outcome:** with Night off set, the test monitor goes off at its start and on at its end, the Frame reads Resting, and All off holds the displays off until the next scheduled power change.
- **In scope:** E2, F5. The power-request model and the hold rule. Settings #80, #81 (clock times), #82, #83, #87–#91 (the Home Assistant guards are stored now and used in 3).
- **Out of scope:** Scene power requests (#70, #89) until the owner confirms them; sunrise and sunset times (2b, which brings the house location); Home Assistant (3).
- **Acceptance:** CI tests `tests/test_power_requests.py` (DB, controllable clock: the newest request wins until it ends; the schedule resumes; each guard default; a hold ends at the next scheduled change), `tests/browser/test_power_lane_browser.py` (bands, holds with who set them, Resume schedule), and a node test that an "off" target runs the display's chosen method. Bench step: an agent sets a Night off window two minutes ahead on the test Pi and confirms off, then on.
- **Depends on:** 1b; **owner decision: the U1 amendment** (blocking).
- **Risks and unknowns:** whether Central or the Pi evaluates the schedule is not designed; the Node redesign's autonomy favours the Pi holding it, so Night off survives Central being down.
- **Size:** M.
- **Moment it proves:** 6: at Night off's start the test monitor reports off within one minute, and at its end reports on within one minute.

### 2b. The week

- **Outcome:** the owner makes "Evenings, Mon–Fri 17:00–23:00" as one repeating Schedule, sees it on the week calendar, and every Frame's Why is this playing names it.
- **In scope:** E1, E3, E5, E6, A4 (settings #2–#5, including the location sun times need), B3's names and room label (#92 on the one wall, #94). Settings #72–#76, #78, the sun-relative power times in #81.
- **Out of scope:** holidays and date ranges (E4, #77, deferred); several walls (6).
- **Acceptance:** CI tests `tests/test_program_recurrence.py` (DB, controllable clock: weekly rules across midnight and a daylight-saving change; the Default Scene fills gaps; existing copies from the old repeat helper become one rule), `tests/test_why_playing.py` (every Frame's explanation names the winner and what it beat), `tests/browser/test_schedule_flow_browser.py` extended. No bench step.
- **Depends on:** 2a (the Power lane shares the page).
- **Risks and unknowns:** migrating the existing separate Programs; sun times need a sunrise library (prior art: `astral`).
- **Size:** M.
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

### 4. Real photo sources

- **Outcome:** the owner connects Immich in Settings (no file, no restart), sees the real counts, and builds "Anna and Ben, last 3 years, no screenshots", whose preview matches what plays.
- **In scope:** A3, D1, D2. Settings #8–#33, #36. Immich health over time: a rejected key or an unsupported Immich version shows as Needs a look with the fix.
- **Out of scope:** writing anything back to Immich.
- **Acceptance:** CI tests against the Immich fixture (`tests/integration/compose.immich.yml`): `tests/test_immich_connection.py` (Test shows the fixture's exact photo, video, album and people counts; a bad key reads as a plain error; the key never reaches the browser), `tests/test_source_filters.py` (albums, people, exclusions, the three default exclusions), `tests/test_source_preview_matches_plan.py` (every asset the planner assigns is in the preview set), `tests/browser/test_source_flow_browser.py` extended. No bench step.
- **Depends on:** nothing; it can move earlier if the wall needs real photos sooner.
- **Risks and unknowns:** the media worker's 1,000-match refusal contradicts [requirements](requirements.md#live-media-compatibility-and-preparation) and must go; Immich's API changes between versions.
- **Size:** L. Split: connection page and health; filters and exclusions; several sources per Scene.
- **Moments it proves:** 2 (the counts test) and 5 (the preview-matches-plan test).

### 5a. Photo fit and order

- **Outcome:** on a landscape Frame, portraits show as a pair or on a blurred mat, never cropped, and Scenes shuffle instead of showing newest first.
- **In scope:** D3, D4. Settings #43–#52, #102, #103 (strict, with the skipped count).
- **Out of scope:** video and captions (5b); slow pan and zoom (#60).
- **Acceptance:** CI tests `tests/test_planner_fit.py` (every assignment is same-orientation, a portrait pair, or a matted single; Fill crops only compatible photos), `tests/test_planner_order.py` (shuffle; no repeat within N days on a test clock), and a Player render test that draws a blurred mat and a portrait pair. No bench step.
- **Depends on:** 4; **owner decision: D13** (for no-repeat only).
- **Risks and unknowns:** a portrait pair is two photos on one Frame, a new assignment shape for the planner; face-aware crop needs face data from Immich.
- **Size:** L. Split: fit and mats; pairing; order.
- **Moment it proves:** 4, made checkable: no portrait photo is cropped on a landscape Frame, and no photo is letterboxed without a mat.

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
- **In scope:** F7. Settings #69, #87, #88, #141–#144; the device and entity map, the request topic and Blueprints ([design §8](operator-console-design.md#8-home-assistant)).
- **Out of scope:** a dedicated Home Assistant integration; native sensors.
- **Acceptance:** CI test `tests/integration/test_home_assistant_mqtt.py` against a Mosquitto container (discovery announcements kept by the broker with stable ids; a switch becomes a power request under the hold rule; Remove clears them; a broker restart re-announces). Bench step: an agent points a throwaway Home Assistant container at the broker and checks the devices appear.
- **Depends on:** 2a (power requests), 2b (Frame names and room label), 3b-i (status words, Show now with an end time).
- **Risks and unknowns:** broker credentials stored on the server; guard defaults untested in a real home; Scene power waits on the owner.
- **Size:** L. Split: connection and discovery; controls and readings; inputs and Blueprints.
- **Moment it proves:** none of the ten.

### 6. Setup and hardware life

- **Outcome:** on a fresh install, the setup checklist takes the owner from an empty Central to photos on a newly plugged Pi, and a replacement Pi is bound with one tap.
- **In scope:** A1, A5 (the network-boot self-test and router help, setting #130), B1 (the code on the display), B3's several walls, B7, C1, C2 (#104), G4's New Pis waiting, G10's Retire a Pi, the "needs 4 GB" refusal shown in the console ([requirements](requirements.md#supported-player-hardware)). Settings #7, #93, #107 (bulk actions on selected Frames), #121.
- **Out of scope:** roles; spanning.
- **Acceptance:** CI tests `tests/browser/test_setup_checklist_browser.py` (each step turns green only from a real check), `tests/test_new_pi_binding.py` (DB: one tap binds; the Frame keeps position, Display settings and Schedules), `tests/test_surfaces.py` (DB: several walls), and a check that a refused 2 GB board shows its reason (extending the existing node memory tests). Bench step: an agent clears the test Pi's binding, power-cycles it and times its appearance.
- **Depends on:** 1b (the display code), 2b (names).
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
| 4 | A portrait on a landscape display is never cropped, and nothing is letterboxed without a mat | Every planner assignment is same-orientation, a portrait pair, or a matted single | [5a](#5a-photo-fit-and-order) |
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
| Central cache limit #129, Pi logs #124 | [7](#7-peace-of-mind) |
| U1's open fault classes (compositor, graphics, kernel, panel, power, before the display starts; [requirements](requirements.md#failure-visibility-and-recovery)) | Outside this roadmap: owned by the Node programme ([Player architecture](player-architecture.md)) |
| Nice-to-have catalogue rows no delivery names: #1 house name, #6 language, #60 slow pan and zoom, #61 clock and weather, #68 burn-in protection, #137 single sign-on | Wait until a delivery picks them up |

## Deferred, and what that costs

| Deferred | Cost while it waits |
|---|---|
| Holidays and date ranges (E4, #77) | Christmas photos need a Schedule turned on and off by hand |
| Repeated activation choices (#66) | Starting a playing Scene again is ignored (the requirements' default) |
| The reference experiences beyond the slideshow and Good night: Talking portraits, Haunted portraits overlay, Calendar change beneath an overlay, Coordinated readiness ([requirements](requirements.md#reference-experiences)) | The wall does slideshows, Show now takeovers and Night off; theatrical, choreographed effects wait, though Central's overlays, child Scenes and protection get controls in [1x](#1x-expose-what-already-exists) |
| Several Installations ([requirements](requirements.md#operations-and-scope)) | One House per Central |
| Household and guest roles | Everyone who signs in can change everything; a guest cannot be given Skip and Pause alone |
| Notification channels beyond the basics | Alerts reach only the channels 7 builds |
| Change history | Only the 10-second Undo after a change; no record of who changed what, and no Revert later |
| Native sensors and auto-dim | Room-light brightness and motion need Home Assistant |
| Lights and other non-display controls | Scenes cannot drive lights or relays |
| Spanning one photo across several Frames (C5) | Each Frame shows its own photo |
| Per-Pi software pins ([console DDD Part E](operator-console-ddd.md#part-e-v2-only-console-and-node-release-workflows-feature-layer)) | The whole wall runs one selected release |

What the design itself gives up is listed in [the operator console design](operator-console-design.md#13-what-this-design-gives-up).

## Settings status

The [settings catalogue](operator-console-design.md#11-settings-catalogue) lists 144 settings a power user expects, each with one home. On 2026-10-09, 29 exist in the console (some in a poor form: one Photo source per Scene, one schedule window, a position editor that times out, size editable only while unbound, software brightness labelled "draft"), 8 are built in Central or on the Pi with no control in the console (the [1x](#1x-expose-what-already-exists) list), and 107 are missing. Update these counts when a delivery lands.
