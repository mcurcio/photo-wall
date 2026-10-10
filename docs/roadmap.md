# Photo Wall roadmap

**Status:** accepted 2026-10-09. This is the product roadmap. It walks the whole life of a Photo Wall install the way a power user lives it, from before any hardware arrives to fixing things years later, and orders the work by what the owner can do on the real wall after each delivery. Nothing in it is built until its delivery says so.

The owner, 2026-10-09 (chat), on the first-principles console review: "I LOVE the Journey section of that doc. I want those ideas codified as the roadmap." The review page is [Console Setup Review](https://claude.ai/artifact/EwjdvEcM82HyLDb4BrDSdC).

Where things are defined:

- The console's target design (pages, the Frame page, power, Home Assistant, the settings catalogue) is [§0 of the console UX design](operator-console-ux-design.md#0-the-first-principles-console-2026-10-09); the choices and what they replace are [decision 0019](decisions/0019-first-principles-console.md).
- What the owner asked for is in [requirements › Operator experience](requirements.md#operator-experience).
- The engineering slice order for the first MVP is the [implementation plan](implementation-plan.md); this roadmap orders product capabilities on top of it. Each delivery is cut into beads when it starts, in its own design pass.

How to read the tables. **Priority:** M = broken without it, S = should have, N = nice. **Today** is the state on 2026-10-09: **exists** (in the console, perhaps in a poor form), **built, no controls** (Central or the Pi can do it, the console cannot reach it), **missing** (nowhere). The note says what is there now. Update a step's Today cell when a delivery lands.

## The journey

### A. Before any hardware

| # | Step | What the user expects | P | Today | Now |
|---|---|---|---|---|---|
| A1 | Install Central | One Compose file; the first page is a checklist (database, network boot serving, boot files staged) | M | built, no controls | Checks exist in pieces; no checklist |
| A2 | Sign in | Create an owner account | M | missing | A server token from an environment variable |
| A3 | Connect Immich | Paste URL and API key, Test: "41,203 photos, 212 albums, 96 people" | M | missing | Hand-written JSON file in the media worker's volume, then a restart |
| A4 | House basics | Timezone, location for sunrise and sunset, units | M | missing | Not settable |
| A5 | Ready for Pis | A "network boot OK" self-test; Pi 5 with 4 GB or more stated up front | S | missing | A fresh install refuses every boot until a release is picked |

### B. The first Pi and display

| # | Step | What the user expects | P | Today | Now |
|---|---|---|---|---|---|
| B1 | Pi appears | Plugged in, it shows up within a minute and the display shows its code | M | exists | Appears under Hardware; no on-screen code |
| B2 | Identify | The display flashes its name and a border | M | exists | "Identify Panel" on the Binding tab |
| B3 | Name and place | "Living room left", pick the wall | M | missing | A code-style Frame id; one wall only |
| B4 | Fit the picture | Test pattern; drag corners, rotate, crop, nudge; live on the display; Done/Revert | M | exists | Hidden under Calibration; a 30-second session |
| B5 | Picture quality | Brightness, contrast, colour temperature, gamma, grey ramp | S | built, no controls | Software brightness only, labelled "draft" |
| B6 | Power | Detect how this display can be switched; Test off/on | M | missing | Nothing |
| B7 | First photos | A Favourites slideshow starts by itself | M | missing | Build a Source, a Scene and a Program by hand |

### C. More screens

| # | Step | What the user expects | P | Today | Now |
|---|---|---|---|---|---|
| C1 | Wall layout | Drag scaled rectangles in real cm or inches onto each wall | S | exists | One wall; millimetres typed by hand |
| C2 | Rooms and groups | Every room is a group; act on groups | M | missing | No groups |
| C3 | Copy settings | Copy picture settings to other Frames | S | missing | Nothing |
| C4 | Two displays per Pi | Both HDMI ports listed, each feeding its own Frame | S | exists | Possible through Binding |
| C5 | Span | One photo across neighbouring Frames | N | missing | Nothing |

### D. Content

| # | Step | What the user expects | P | Today | Now |
|---|---|---|---|---|---|
| D1 | Pick photos | Albums, people, places, dates, favourites; live count and thumbnails | M | exists | Tags, dates and favourites only; no albums or people |
| D2 | Exclude | Exclude people and albums; archived, hidden and screenshots excluded by default | M | missing | Nothing |
| D3 | Photo fit | Blurred mat, colour mat, fill; portraits paired on landscape displays | M | missing | Nothing |
| D4 | Order | Shuffle with no repeats, chronological, On this day | M | missing | Newest first, fixed |
| D5 | Timing and transitions | Seconds per photo, crossfade, stagger between Frames | M | built, no controls | Seconds per photo only; fades exist without controls |
| D6 | Video | Muted, length cap, share of videos | M | missing | Nothing |
| D7 | Captions | Date, place, people | S | missing | Nothing |

### E. Scheduling

| # | Step | What the user expects | P | Today | Now |
|---|---|---|---|---|---|
| E1 | Weekly routine | A calendar with repeating blocks | M | exists | One window; repeats make up to 60 copies |
| E2 | Night off | Off 23:00 to 07:00, displays actually off | M | missing | Nothing |
| E3 | Sunrise and sunset | Times relative to the sun | S | missing | Nothing |
| E4 | Holidays | Date ranges that override the routine | S | missing | Nothing |
| E5 | Default content | What shows when nothing is scheduled | M | missing | Nothing |
| E6 | Why is this playing? | An explanation for every Frame | M | exists | Planned facts by internal ids |

### F. Daily use, from a phone

| # | Step | What the user expects | P | Today | Now |
|---|---|---|---|---|---|
| F1 | Glance | A live map of what each Pi presents | M | missing | Planned facts only; no picture |
| F2 | Skip and previous | On a tile | M | missing | Nothing |
| F3 | Hide forever | Two taps, undoable | M | missing | Re-tag the photo in Immich |
| F4 | Show now | An album or person on chosen Frames, for 1 hour | M | exists | Exists; no Frame choice, no end time |
| F5 | All off/on | One button | M | missing | Nothing |
| F6 | Phone layout | Bottom tabs, usable while standing at the display | M | missing | Tables scroll sideways off the screen |
| F7 | Home Assistant | Frames, power, Scenes and status in Home Assistant; motion, room light and away mode drive the wall | S | missing | Nothing |

F7 comes from the journey research (its step "Integrations"); the review page's journey table left it out although its build order schedules it as delivery 3.

### G. Fixing things

| # | Step | What the user expects | P | Today | Now |
|---|---|---|---|---|---|
| G1 | Health at a glance | One status word per Frame and for the house | M | exists | Repeated in five places, internal words, a manual Refresh |
| G2 | Diagnose | A plain cause naming the failing part, with a time | M | exists | Detailed but full of internal terms |
| G3 | Restart | Restart the app, reboot the Pi, with progress | M | exists | Reboot only |
| G4 | Replace a Pi | "Which Frame did you plug this into?" One tap | M | exists | Unbind and rebind by hand across pages |
| G5 | Display changed | Prompt to re-check position, picture and power | M | missing | The display profile is editable only while unbound |
| G6 | Updates | New releases pre-download; you click Apply | M | exists | Exists behind a gate the console can't open |
| G7 | Backup and restore | Download, a nightly copy, restore with a preview | M | missing | Nothing |
| G8 | Logs | Per-Pi logs without SSH | S | missing | SSH only |

G6 follows the owner's release choice of 2026-10-04: no automatic updates; releases pre-download and the operator clicks Apply.

## Delivery order

Each delivery ends with something the owner can do on the real wall. Delivery 1 is split so the first piece needs no new Pi work.

| # | Delivery | What it builds | Journey steps it completes | After it the owner can |
|---|---|---|---|---|
| 1a | The Frame page | Click a Frame on the wall map to open its page: Overview, Position, Picture, Hardware. Position without the timeout, with arrow-key nudge and Done/Revert; the existing software brightness moved to Picture; Identify in the header; Replace with… on Hardware. Console only | B4; B2 moves to the Frame header; B5 and G4 get their home (finished in 1c and 6) | Find and set a Frame's position and brightness in two clicks |
| 1b | Display identity and power | Photo Wall recognises each display from what it reports over HDMI (maker, model, serial), and its picture and power settings move with it. The Pi image gains HDMI-CEC and DDC/CI tools. The Power tab detects the power method and offers Test off/on | B6 | Turn a display off and on from the console |
| 1c | Brightness, contrast and colour on the display | Through DDC/CI wherever the display accepts it, Photo Wall picture adjustment otherwise, each slider saying which; grey ramp; Copy to other Frames | B5, C3 | Use all three of the knobs he asked about, as far as each display allows |
| 2 | Power schedule and the week | The Power lane with Night off and sunrise/sunset times; one power-request model (who, which Frames, until when, why), used first by All off/on, with its guard settings; repeating Schedules, running past midnight, the Default Scene, Why is this playing? | E1, E2, E3, E5, E6, F5 | Have displays go off at 23:00 and come back at 07:00, and see the week |
| 3 | Home Assistant | MQTT discovery, the must-have controls and readings, Scenes in Home Assistant, the request topic, the setup screen, Blueprints for motion, room light and smart plugs | F7 | Let motion, away mode and a "guests" button drive the wall |
| 3b | Home that tells the truth | Status word, problems strip, a live map of what each Pi presents, Skip, Hide, Pause, Show now with an end time, the phone layout | F1, F2, F3, F4, F6, G1, G2 | Check the wall from a phone and act on it |
| 4 | Real photo sources | The Immich connection page; albums, people, exclusions; several sources per Scene | A3, D1, D2 | Build "Anna and Ben, last 3 years, no screenshots" without leaving the console |
| 5 | A good-looking slideshow | Blurred mat, portrait pairing, shuffle with no repeats, transitions, video rules, captions | D3, D4, D5, D6, D7 | See no black bars or cropped heads, and no longer newest-first only |
| 6 | Setup and hardware life | The setup checklist, New Pis waiting, Frame names, several Walls and rooms, the display-changed flow | A1, A5, B1, B3, B7, C1, C2, G4, G5 | Bring a new install or a new Pi to photos in minutes |
| 7 | Peace of mind | Backups and restore, notifications, the Hidden photos page, current photo and Pi readings in Home Assistant | G7 (and F3's list page) | Leave it alone for months |

Dependencies the order relies on: 1b's Display identity is what lets 1c's settings follow a display; 2's power-request model is the one 3's Home Assistant switches use; 3b's live map needs the Pi to report what it presents. E3 (sun times) needs the House location from A4, so delivery 2 carries that one setting ahead of the rest of A4.

**Steps no delivery covers yet.** These need a place in the order before the journey is complete:

| Step | Gap |
|---|---|
| A2 Sign in | The owner account replacing the environment-variable token is a must-have, but no delivery lists it |
| A4 House basics | Only the location (for E3) is placed; timezone, units and clock format are not |
| E4 Holidays | Date ranges and yearly events appear in the Schedule design but in no delivery |
| G3 Restart | Restart the app with progress appears in the Pi page design but in no delivery |
| G6 Updates | Exists, but the gate the console can't open is in no delivery |
| G8 Logs | Per-Pi logs appear in the Pi page design but in no delivery |
| C5 Span | Deferred (below) |

## The ten moments of truth

These decide whether a power user keeps Photo Wall. They are the roadmap's acceptance yardstick: a delivery that names a moment is accepted only when that moment holds on the real wall, proved by an automated test where one can run and by bench evidence where it needs the physical display ([which evidence](../CONTRIBUTING.md#choose-the-right-evidence)).

| # | Moment | Delivered by |
|---|---|---|
| 1 | A Pi plugged into a display shows up with its name on screen and in the console within a minute, with no SSH | 6 |
| 2 | Connecting Immich shows real counts and thumbnails | 4 |
| 3 | Dragging a corner moves the picture on the real display right away, and Undo works | 1a |
| 4 | The first portrait on a landscape display looks good: a blurred mat or two portraits side by side, never a cropped head | 5 |
| 5 | The Photo source preview is exactly what will play | 4 |
| 6 | Night off really turns the displays off, and they come back on in the morning | 1b, 2 |
| 7 | "Why is this showing?" has an answer for every Frame | 2 |
| 8 | Hiding a photo from a phone takes two taps, and it never comes back | 3b |
| 9 | A black screen is explained in plain words that name the failing part: display, Pi, app, network or Central | 3b |
| 10 | Replacing a Pi is one click and keeps every setting; backup and restore does the same for the whole house | 6, 7 |

## Deferred, and what that costs

| Deferred | Cost while it waits |
|---|---|
| Household and guest roles | Everyone who signs in can change everything; a guest cannot be given Skip and Pause alone |
| Notification channels beyond the basics | Fewer ways to be told; alerts reach only the channels delivery 7 builds |
| Change history | Only the 10-second Undo after a change; there is no record of who changed what, and no Revert later |
| Native sensors and auto-dim | Room-light brightness and motion need Home Assistant; without it there is no automatic dimming |
| Lights and other non-display controls | Scenes cannot drive lights or relays; only display power is controlled |
| Spanning one photo across several Frames | Each Frame shows its own photo |
| Per-Pi software pins (deferred by the owner 2026-10-04) | The whole wall runs one selected release; one Pi cannot be held back on another |

What the design itself gives up is listed in [§0.12 of the console UX design](operator-console-ux-design.md#012-what-this-design-gives-up).
