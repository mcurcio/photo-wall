# Operator console design (2026-10-09)

**Status:** Journey adopted as the roadmap by the owner, 2026-10-09; decisions 1–8 proposed, pending this PR. Nothing here is built. The [roadmap](roadmap.md) owns the delivery order, each journey step's status today, and the deferred list; [decision 0019](decisions/0019-first-principles-console.md) records the decisions and what they replace. The earlier [console UX design](operator-console-ux-design.md) is the historical gate record this document supersedes. The design-system layers, tokens, lint rules and look of [decision 0018](decisions/0018-console-by-domain-and-design-system.md) stand: every page here is built from that catalog, following the [design language](design-language.md) (principles, page templates, patterns, save models, status words and voice).

**Review page:** [Console Setup Review](https://claude.ai/artifact/EwjdvEcM82HyLDb4BrDSdC).

## 1. Why

The owner, 2026-10-09 (chat):

- "Central needs another UI pass to make sure that all of the features are being exposed on the UI. I cant find some of the simple config knobs, like how do i set the visible frame position on a display? how do i control the CEC power? how do i change the brightness and contrast?"
- "i continue to be frustrated with how poorly designed the UI is."
- "dont stop at my first 3 questions. i want you to do a comprehensive analysis and take a "first principles" approach to thinking through this like a user would. ignore what the current code does, and just walk through the steps a power user would expect to take."

Three designers walked a whole-house install as a power user would, without reading the code, then checked the result against about 15 similar products (consumer frames such as Meural, Samsung The Frame and Aura; signage consoles such as Yodeck, ScreenCloud and Xibo; self-hosted viewers such as Immich Kiosk and ImmichFrame). A fourth merged the work and a reviewer attacked it. The journey they walked is the [roadmap](roadmap.md#the-journey).

The answer to the three questions is one page: **click the Frame, then use its Position, Picture or Power tab.** Everything a person adjusts for one spot on the wall lives on that page.

## 2. The object model

Eight things the user manages, each with one home. The UI label is what the console says; the domain term is the [requirements](requirements.md) word the code keeps.

| UI label | What it is, as the user sees it | Domain term |
|---|---|---|
| **House** | The whole home Photo Wall runs in: timezone, location for sunrise and sunset, units. There is one. | Installation |
| **Wall** | A wall in a room, with a drawing of where its Frames hang. It carries a room label. | Wall / Surface |
| **Frame** | A fixed spot on a wall. It holds where the picture sits there (position, crop, rotation) and its display profile (size and resolution). It stays when the display or Pi is swapped. | Frame, with its Frame display profile |
| **Display** | The TV, monitor or bare panel at a Frame, recognised from what it reports over HDMI. Its brightness, contrast, colour and power method move with it. Edited from the Frame page. | Panel, plus its display-power Actuator and the photometric part of Calibration |
| **Pi** | The replaceable box feeding one or two Frames over HDMI. | Player; its HDMI ports are Outputs; which port feeds which Frame is a Binding |
| **Photo source** | A saved question to Immich: albums, people, dates, with exclusions. Its answer updates by itself. | AssetSource |
| **Scene** | What plays and how: Photo sources, Frames, timing, fit, transitions. "Good night" is a dark Scene. | Scene |
| **Schedule** | When Scenes play: days, times, sunrise or sunset, date ranges, priority. | Program |

Verbs and states the user does not manage as things: **Playing now** is a Scene Run; **Show now** (a takeover with an end time) is an Activation request that starts an overlay Run; **Never show this photo** adds to the Never show list. A **Group** is a Target group; every Room is a ready-made Group.

```
House
 ├─ Wall "Living room" ── Frame "Left"  ◀── Display (Samsung 55")  ◀── Pi pw-3f2a · HDMI 1
 │                     └─ Frame "Right" ◀── Display (Samsung 55")  ◀── Pi pw-3f2a · HDMI 2
 ├─ Wall "Hall"        ── Frame "Portrait" ◀── Display (OLED panel) ◀── Pi pw-91c0 · HDMI 1
 │
 ├─ Photo sources ──used by──▶ Scenes ──play on──▶ Frames
 │                               ▲
 ├─ Schedule: content lane ──────┘   Show now (temporary, ends by itself)
 │            power lane ──────────▶ Displays on/off
 │
 └─ Home Assistant ◀──MQTT──▶ Frames as devices · power holds · Scenes · inputs
```

**Display, not Panel, is a design choice.** The owner's questions used the word "display" ("the visible frame position on a display"), but he did not choose the label; choosing it to name the domain's Panel is this design's call. Help text says "TV" where that is plainer; the page says "display" because not every display is a TV. The code keeps Panel. The word clashes with **Display Host**, the Pi's layer that owns the final picture: the console never shows "Display Host" outside Details, and there it keeps its full name.

## 3. New terms

| Term | Definition | Why it is needed | Existing term it could be |
|---|---|---|---|
| **Display** (with identity) | The display hardware at a Frame, recognised by the maker, model and serial it reports over HDMI (its EDID, the identity block every HDMI display sends). Its picture and power settings are stored on it and follow it to another Frame. | The owner: display hardware settings "could remain consistent to where the panel moves" | Panel. Panel has no stored identity today; this gives it one. A display that reports no usable identity is a new Display each time it is plugged in. |
| **Room** | A label on a Wall. Each distinct room makes a ready-made Group of its Frames and suggests the Home Assistant area. | People think "Living room", not "Surface 2"; every home product groups by room | A rule-defined Target group. No new domain entity. |
| **Never show list** | The wall-only list of photos never to show again. Nothing is written back to Immich. Named so it is not confused with Immich's own "hidden" photos. | "Never show" is a must-have of daily use | The wall-specific configuration Photo Wall may keep ([purpose](requirements.md#purpose-and-experience)) |
| **Default Scene** | The Scene that plays on a Frame when no Schedule covers that time | So a Frame is never left without a plan by a gap in the calendar | A Program's defaults ([experience model](requirements.md#experience-model)) |
| **Pause** | Freeze what a Frame shows for a time (default 1 hour), then carry on by itself. A paused Frame counts as Resting. | Daily use: stop on a photo someone is looking at | An Activation request for an overlay holding the current photo; to be confirmed with the owner |
| **Power lane** | The band above the content calendar on the Schedule page that shows, for each Frame, whether its display should be on, and why. The one place that decides and explains display power. | The owner described power as "a unique concept" | None. Display power is operational state ([operations](requirements.md#operations-and-scope)), not a Scene. |
| **Night off** | The power schedule's off window (default 23:00 to 07:00, every day). | The everyday name for the power schedule | The open "persistent dark-state and wake-up policy" of [Good night](requirements.md#reference-experiences) |
| **Power request** | A request to turn some displays on or off, carrying who asked, which Frames, until when and why. | One model for every source of power changes, so the Power lane can show why | An Activation request aimed at the display-power Actuator |
| **Hold** | A power request made by a person or by Home Assistant (not by the schedule or a Scene). How long each kind lasts is [the hold rule](#the-hold-rule). | So the Power lane can show who is holding a display and offer Resume schedule | A kind of power request |
| **Blueprint** | Home Assistant's shareable automation template, which a user imports and fills in. Photo Wall ships some. | Covers motion, room light and smart plugs without custom Home Assistant code | None (a Home Assistant term) |

## 4. Navigation

The rail keeps one-time bring-up apart from everyday use.

| Group | Section | What it is for |
|---|---|---|
| Everyday | **Home** | One status word, a live map of every Frame laid out like the room, problems with a fix button, Show now, All off/on. The console opens here. A tile opens that Frame's page. |
| Everyday | **Schedule** | The week as a calendar with two lanes: Power (when displays are on, with holds) and Content (which Scenes play); holidays; Why is this playing? |
| Everyday | **Scenes** | Scenes, Photo sources and the Never show list |
| Set up | **Frames** | Walls, the wall layout, the Frame list, Groups |
| Set up | **Pis** | New Pis waiting, software, restart, replace |
| Set up | **Settings** | House, Photo library (Immich), Updates, Integrations (Home Assistant), Backups, Storage and network boot; later Notifications, People and access |
| Until finished | **Setup checklist** | First-run steps; hides itself when done |

The top bar carries the house status word. On a phone the tabs are **Home · Show now · Schedule · More**; a tile tap opens the Frame page, so the knobs are two taps away while standing at the display, and Position works with big arrow buttons and corner handles.

Pattern: signage consoles put screens, playlists and schedule at the top and settings in one area. Frames and Pis are separate sections because a Frame is not a device (signage products that make the screen the device force re-pairing when the hardware changes).

## 5. The Frame page

One home for one spot on the wall, and the page every Home tile, Frames row and Pi port links to.

**Header:** the Frame name (editable inline), its Wall and room, the status word, a live view of what the Pi is presenting (labelled as that, never "on screen"), what is playing and until when, and the buttons **Identify** and **Skip**.

| Tab | What it holds |
|---|---|
| **Overview** | Today's timeline for this Frame with a now line; Why this is playing; its Groups; recent problems; anything unfinished in its setup |
| **Position** | The display shows a test pattern with the Frame name, edges, corner positions and actual output mode. Drag four corners, or nudge with arrow keys in 1, 10 or 50 pixel steps; rotation 0/90/180/270 (which sets the orientation); crop per edge; Reset to full screen. No session timeout while the tab is open. |
| **Picture** | Brightness, contrast, colour temperature (warm, 6500 K, cool), gamma (2.2), Show grey ramp, Copy to other Frames… The values belong to the display and say so ("These settings belong to the display (Samsung 55", serial …4K2) and move with it"). Each slider says where it acts: **On the display** (DDC/CI) or **Photo Wall picture adjustment**. |
| **Power** | Power method: HDMI-CEC, DDC/CI, HDMI signal off (the panel sleeps) or a smart plug through Home Assistant, the detected one chosen. **Test: turn off / turn on**, reading "Display confirmed off" or "Display didn't answer". Switch the display to this input on power-on (on). Never power off while the display shows another input (on). Then, read-only: "Now on · next off 23:00, set by Schedule › Power lane", and any hold ("Off — by Home Assistant (Away mode)"). When displays are on is not set here. |
| **Photo fit** | Use the Scene's setting, or override it for this Frame; minimum quality (strict, per [compatibility](requirements.md#live-media-compatibility-and-preparation)); what was skipped here and why ("312 too small · 1,204 wrong orientation"), each clickable |
| **Hardware** | The Frame's display profile (size, resolution, detected with an override); the Display (model, serial, HDMI mode); "Fed by Pi pw-3f2a · HDMI 1" with **Replace with…**; when a different display appears, the [display-changed flow](#6-display-identity) |

How each tab saves is assigned in the [design language's concept → UX table](design-language.md#8-concept--ux-translation) (rows B4, B5, B6, D3, G4); the models themselves are its [save models](design-language.md#save-models).

**Unbound Frame:** every tab shows, and Position, Picture and Power say "Choose which Pi and HDMI port feeds this Frame" with a picker. Binding happens only here (unchanged from 0018).

**Errors** name the failing part inline, for example "Pi online; the display stopped answering on HDMI at 21:04. Check the display's input. [Restart photo app] [Reboot Pi]".

Pattern: the screen-settings page every signage product has, split the way a TV's own menu is. Unlike those products, the Frame and the Pi are separate, so replacing a Pi keeps every setting.

## 6. Display identity

- A Display is identified by the maker, model and serial in its EDID. When a Pi reports a display on an Output, Central matches it to a known Display or records a new one.
- Stored on the Display, and following it: brightness, contrast, colour temperature, gamma, power method, switch input on power-on, never power off while another input shows.
- Stored on the Frame, and staying put: position, crop, rotation, photo fit, and the Frame display profile (physical size and resolution), which the requirements place on the Frame ([installation model](requirements.md#installation-model)).
- **A display changed or moved here:** its picture and power settings come with it, and the Frame is **not ready** until Position is re-checked and its display profile confirmed. The requirements make this a block, not a prompt: "A changed profile requires calibration revalidation before the new equipment is considered ready" ([installation model](requirements.md#installation-model)).
- A display reporting no usable identity (some bare panels) is treated as new each time it is plugged in; its settings start from defaults.
- The settings are edited from the Frame page; there is no separate Display page.
- Two channels inside the HDMI cable carry commands to a display: **HDMI-CEC**, the remote-control channel most TVs answer (power, input), and **DDC/CI**, the settings channel most monitors answer (brightness, contrast, often power).
- Asked whether brightness should drive the display hardware, the owner answered "As much as possible." So power goes through HDMI-CEC, DDC/CI, HDMI signal off, or a smart plug through Home Assistant; brightness and contrast go through DDC/CI where the display accepts it, Photo Wall picture adjustment otherwise. Software adjustment is never called measured panel brightness ([failure visibility](requirements.md#failure-visibility-and-recovery)). The Pi image needs CEC and DDC tooling: the test Pi, driving a portable 1920x1080 monitor, has CEC devices but no I2C bus devices and no `cec-ctl` or `ddcutil`.

## 7. Power

Asked whether power should be its own concept or part of Scenes, the owner answered: "I meant power as a unique concept, but it could go either way. Or both." This design does both.

**Who can turn displays on and off:**

1. The **power schedule** in the Schedule page's Power lane: on and off times (clock or sunrise/sunset ± minutes), days, which Frames (the editor pre-selects every current Frame explicitly; a new Frame joins only if the user chose the "All Frames" group), and Night off (default off 23:00 to 07:00, every day, powering displays off where they support it and showing black otherwise).
2. A **Scene** that asks for its displays on, off, or "leave as scheduled" (the default). Its request lasts while the Scene plays. A "Good night" Scene is optional. **This is a deliberate exception that needs the owner's confirmation:** it stores an operational setting (display power) inside authored content, which the requirements keep apart ([operations](requirements.md#operations-and-scope)). It is offered only because he said "Or both"; without his confirmation, Scenes do not carry it.
3. The console's **All off/on**, a hold.
4. **Home Assistant**, through its switches or the request topic.

### The hold rule

How long each power request lasts. This is the one statement of the rule; other sections link here.

| Request | Lasts until |
|---|---|
| The power schedule | Always in force underneath; it is what the others return to |
| A Scene's request (if confirmed, above) | The Scene stops playing |
| Console **All off** / **All on** | The next scheduled power change (the default; see the guard below) |
| A Home Assistant **power switch** (per Frame, or All on/off) | The same as the console: the next scheduled power change by default |
| Home Assistant **Keep all off** (away mode) | The switch is turned off. Shown as "Off — by Home Assistant (Away mode)". |
| A message on the request topic | Its `until` time ("Off — by Home Assistant (film night) until 23:30"); a message with no `until` is a hold like a power switch |

**Which request wins:** the newest one, until it ends; then the power schedule takes over again. The guard settings adjust it. Asked who should win between the schedule, Home Assistant and a person, the owner answered: "I don't know. I'm hoping for configuration options built in sensible defaults".

| Guard (Schedule › Power lane › Rules) | Default | Why this default |
|---|---|---|
| Home Assistant may turn displays on during Night off | No | A motion sensor at 2 am shouldn't light up the room. A person can still press All on in the console. |
| Home Assistant may turn displays off | Yes, any time | Turning off is always safe (away mode, a film starting) |
| Scenes may turn displays on during Night off | No | Night off means dark unless a person decides otherwise |
| A hold (console All off/on, or a Home Assistant power switch) lasts until | The next scheduled power change (other choices: a set time, or until resumed) | Like a thermostat hold: nothing stays off forever by accident |

One more power setting lives on each display, not here: **Never power off a display showing another input** (default on, Frame page › Power), because someone is watching TV on it.

The Power lane draws solid bands while displays should be on, grey for Night off, and hatched holds labelled with who set them, each with **Resume schedule**. It is the one place that shows why a display is on or off now; the Frame page's Power tab only reads it. Display power stays operational state ([operations](requirements.md#operations-and-scope)), and a display dark on purpose is **Resting**, not a fault (see [§12](#12-where-a-power-users-expectation-meets-a-written-requirement) on U1).

## 8. Home Assistant

Home Assistant decides **when**, through its own automations; Photo Wall decides **what happens**, and stays authoritative for its own configuration ([operations](requirements.md#operations-and-scope)). Asked how Night off should work, the owner answered: "I like the idea of a native power schedule, yeah. But I also want home automation inputs, so also consider how this system might be exposed to home assistant." This section is that consideration; it covers more than power.

**Connection.** Central's media worker connects to Home Assistant's MQTT broker (a message relay, usually the Mosquitto add-on) and announces its devices with MQTT discovery, so they appear in Home Assistant by themselves. Zigbee2MQTT, Valetudo and Frigate integrate the same way. Only Central talks to Home Assistant: Pis never touch MQTT, and Central's internal bus to the Pis is never bridged to it. If Home Assistant goes away, the wall keeps running on its own schedule.

**What Home Assistant sees.** One device for the house and one per Frame, named after it, placed in the area its room suggests. Device ids come from the Frame's internal id, so automations keep working when a display or Pi is swapped.

| Device | Must have | Should have | Nice |
|---|---|---|---|
| Each Frame | Power switch · Status (the six words; "Can't tell" shows as unavailable) · Brightness · Skip button | Now showing (Scene, until when) · Current photo image · Pause switch · "Display should be on" (to drive a smart plug) | |
| Photo Wall (the house) | All on/off switch · Keep all off switch (away mode) · each Scene marked "Show in Home Assistant" as a Home Assistant scene, which starts it as Show now | Night off enabled switch · Problem event ("Frame not showing") | |
| Pis | | | Online, temperature, software version; off unless diagnostics are turned on |

Power is a switch, not a light, so Home Assistant's "turn off all lights in this room" cannot blank the wall by accident. How long each switch holds is [the hold rule](#the-hold-rule).

**Inputs from Home Assistant.**

| In the house | What Photo Wall does | Requirements term |
|---|---|---|
| Motion or occupancy | A Blueprint sends a request-topic message to wake or sleep that room's displays, with an `until` | A Sensor feeding a Trigger; a power request |
| Room light level | Sets Brightness through a shipped Blueprint | A Sensor; environmental adaptation |
| A "guests arriving" button | Starts a chosen Scene as Show now for its default length | A Trigger making an Activation request |
| Away mode | Turns on **Keep all off**; displays stay off until it is turned off | A power request with no end |
| A film playing on that display | Pause; Photo Wall never powers off a display showing another input | Blanking |
| A panel with no power commands | "Display should be on" drives a smart plug through a Blueprint | The display-power Actuator |

Continuous viewer tracking stays out of scope ([operations](requirements.md#operations-and-scope)). Besides the entities, Central listens on one request topic, `photowall/request`, carrying `power`, `frames`, `until` and `reason`. Photo Wall ships Blueprints for motion, room light to brightness, and smart plugs.

**Setup: one screen, Settings › Integrations.** Broker address (default `mqtt://homeassistant.local:1883`), user and password (masked, kept on the server). A live checklist: broker connected → Home Assistant online → N devices published → last command received. Errors read like "Broker rejected the username/password". Toggles: **Home Assistant can control Photo Wall** (on) and **Include Pi diagnostics** (off). **Remove from Home Assistant** deletes the devices cleanly, clearing their retained announcements. The [validation guide](validation.md) already asks for stable entity ids, re-announcement after Home Assistant restarts, and cleanup when equipment is retired.

**Gives up:** Home Assistant needs a broker; there are no custom Home Assistant actions (the request topic and Blueprints cover them); Photo Wall cannot switch a Home Assistant-only plug by itself (a Blueprint does). A dedicated Home Assistant integration can come later on the same interface.

## 9. The other pages

- **Home.** One word for the house ("All good"). Problems pinned on top, each with a plain cause, a since time and one fix button. A live map of each Wall where every tile shows what its Pi is presenting, labelled as that. A now/next strip, and active takeovers with a countdown and **Back to normal**. Tile actions: Skip, Previous, Never show this photo, Why is this playing?, Open Frame (what Skip and Never show do to secured content is in [§12](#12-where-a-power-users-expectation-meets-a-written-requirement)). Updates by push, with no Refresh button. If the whole network is down, one house-wide banner replaces per-tile alerts.
- **Schedule.** The Power lane on top (§7); below it the content calendar with a now line. A Schedule has a Scene, days, start and end (a clock time or sunrise/sunset ±, showing the resolved time), runs past midnight without splitting, an optional date range or yearly event, and a priority (Normal, Special event, Always wins). The Default Scene fills the gaps. Clicking any block explains why it wins ("Evenings (Normal, Mon–Fri 17:00–23:00) beats the Default Scene").
- **Show now** (a sheet from Home, a tile, or the phone tab). Pick what (an album, person, Photo source or Scene), where (Frames or Groups; every current Frame pre-selected explicitly) and how long (1 hour, until tonight's Night off, until I stop). It is an overlay: the schedule beneath keeps advancing and is revealed in its current state ([progression](requirements.md#progression-visibility-and-target-control)).
- **Scenes and Photo sources.** A form with a live preview on a chosen Frame, in sections What, Where, Timing, Order, Photo fit, Video, Captions, Ending, Advanced. Photo sources pair every include with an exclude (albums, people, places, dates, favourites, tags); archived, hidden and screenshots are excluded by default; a live count and thumbnails show before and after saving. The Never show list shows every photo on it with **Show again**.
- **Frames.** A tab per Wall plus "+ Wall"; a canvas to real scale in cm or inches; Identify on each Frame; overlapping Frames flagged; Groups.
- **Pis.** "New Pis waiting: which Frame did you plug this into?" (one tap binds). A table with Frames fed, status, software version and last heard. The Pi page has rename, Identify, Restart app, Reboot (requested → reboot started → back online), logs and Retire. Boxes too small say so ("2 GB, needs 4 GB").
- **Setup checklist.** Server checks (a Pi release picked automatically on a wall that never had one), Immich URL and key with a Test showing counts, house basics, first Pi, first Frame with Position, a pre-filled Favourites slideshow, a schedule and Night off. Each step turns green from a real check, never from a click. It hides itself when done.
- **Settings.** House; Photo library (the Immich connection, set and tested in the console); Updates, which follow the release choices of [console DDD Part E](operator-console-ddd.md#part-e-v2-only-console-and-node-release-workflows-feature-layer); Integrations (§8); Backups; Storage and network boot.

## 10. How it behaves

How the console saves, speaks status and words its errors is the [design language](design-language.md), the one home for UI rules: the four save models and which page uses which ([§5](design-language.md#save-models)), the six status words and their place on 0018's severity scale ([§6](design-language.md#6-status-and-severity)), and the error template with its plain-words rules ([§7](design-language.md#the-error-template)). Each journey step's page template, patterns and save model are in its [concept → UX table](design-language.md#8-concept--ux-translation).

## 11. Settings catalogue

Every setting a power user expects, with exactly one home: 144 settings. **Lives on** is page › section. **P** is priority (M must, S should, N nice). Whether each exists today is tracked by the [roadmap](roadmap.md#settings-status), not here. Display size and resolution are filed under Wall and Frame because the requirements make them part of the Frame display profile.

### House

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 1 | House name | Settings › House | "Home" | N |
| 2 | Timezone | Settings › House | from browser at setup | M |
| 3 | Location (for sunrise/sunset) | Settings › House | unset; set at setup | S |
| 4 | Units (cm/in) | Settings › House | from locale | S |
| 5 | Clock format 12/24 h | Settings › House | from locale | S |
| 6 | Language | Settings › House | from browser | N |
| 7 | Show setup checklist | Settings › House | auto-hide when done | S |

### Photo library

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 8 | Immich server URL | Settings › Photo library | none | M |
| 9 | API key (stored server-side, masked) | Settings › Photo library | none | M |
| 10 | Test connection + counts | Settings › Photo library | n/a | M |
| 11 | Additional Immich users/connections | Settings › Photo library | none | S |
| 12 | Look for new photos every | Settings › Photo library | 15 min | N |

### Photo source

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 13 | Name | Scenes › Photo sources › editor | from first chip | M |
| 14 | Connection (which Immich) | Photo source editor › top | the only one | S |
| 15 | Include albums | Photo source editor › Include | none | M |
| 16 | Include people | Photo source editor › Include | none | M |
| 17 | People must all appear (any/all) | Photo source editor › Include | any | N |
| 18 | Include places | Photo source editor › Include | none | S |
| 19 | Date range (fixed) | Photo source editor › Include | none | M |
| 20 | Relative dates ("last 2 years") | Photo source editor › Include | none | S |
| 21 | On this day | Photo source editor › Include | off | S |
| 22 | Favourites only | Photo source editor › Include | off | M |
| 23 | Tags | Photo source editor › Include | none | M |
| 24 | Minimum rating | Photo source editor › Include | none | N |
| 25 | Media types (photos / videos / live photos) | Photo source editor › Include | photos + videos | M |
| 26 | Exclude albums | Photo source editor › Exclude | none | M |
| 27 | Exclude people | Photo source editor › Exclude | none | M |
| 28 | Exclude tags | Photo source editor › Exclude | none | S |
| 29 | Exclude archived | Photo source editor › Exclude | on | M |
| 30 | Exclude hidden/locked | Photo source editor › Exclude | on | M |
| 31 | Exclude screenshots | Photo source editor › Exclude | on | M |
| 32 | Include partners' photos | Photo source editor › Include | off | N |
| 33 | Skip blurry/duplicates | Photo source editor › Exclude | off | N |

### Scene

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 34 | Name | Scene editor › header | "New Scene" | M |
| 35 | Kind (Slideshow / Hand-placed / Dark) | Scene editor › header | Slideshow | M |
| 36 | Photo sources (several) | Scene editor › What | one | M |
| 37 | Frames / Groups targeted | Scene editor › Where | every current Frame, selected explicitly | M |
| 38 | Hand-placed photo per Frame | Scene editor › What (Hand-placed) | none | S |
| 39 | Seconds per photo | Scene editor › Timing | 30 s | M |
| 40 | Transition type (crossfade / cut / slide) | Scene editor › Timing | crossfade | M |
| 41 | Transition length | Scene editor › Timing | 1.5 s | M |
| 42 | Stagger between Frames | Scene editor › Timing | on, spread over the duration | S |
| 43 | Order (shuffle / newest / oldest / chronological) | Scene editor › Order | shuffle | M |
| 44 | Don't repeat within N days | Scene editor › Order | 7 days | S |
| 45 | Favourite weighting | Scene editor › Order | medium | S |
| 46 | Keep events together | Scene editor › Order | off | N |
| 47 | Same photo on all Frames (sync) | Scene editor › Order | off | N |
| 48 | Fit (Fill crop / Fit with blur / Fit with colour mat) | Scene editor › Photo fit | Fit with blur | M |
| 49 | Mat colour | Scene editor › Photo fit | black | S |
| 50 | Mat padding | Scene editor › Photo fit | 0 | S |
| 51 | Portraits on landscape Frames (show / pair two / hide) | Scene editor › Photo fit | pair two | S |
| 52 | Face-aware crop | Scene editor › Photo fit | on (when Fill) | S |
| 53 | Video share | Scene editor › Video | 10 % | S |
| 54 | Video sound | Scene editor › Video | muted | M |
| 55 | Video length rule (play to end / cap at N s) | Scene editor › Video | cap 30 s | M |
| 56 | Captions on | Scene editor › Captions | off | S |
| 57 | Caption fields (date/place/people) | Scene editor › Captions | date + place | S |
| 58 | Caption corner | Scene editor › Captions | bottom-left | S |
| 59 | Caption auto-hide after | Scene editor › Captions | 5 s | N |
| 60 | Ken Burns (slow pan/zoom) | Scene editor › Timing | off | N |
| 61 | Clock/weather overlay | Scene editor › Captions | off | N |
| 62 | Outro at end (fade to black / dissolve) | Scene editor › Ending | dissolve 3 s | S |
| 63 | Black vs see-through when fading (opacity) | Scene editor › Ending | black | N |
| 64 | When nothing is eligible: keep last photo / fallback | Scene editor › Advanced | keep last photo | M |
| 65 | Keep Frames visible together (protection) | Scene editor › Advanced | off | N |
| 66 | If started again while playing (ignore/restart/queue) | Scene editor › Advanced | ignore | N |
| 67 | Child Scenes | Scene editor › Advanced | none | N |
| 68 | Burn-in protection (pixel shift) | Scene editor › Advanced | off | N |
| 69 | Show in Home Assistant (as an HA scene) | Scene editor › header | off | S |
| 70 | Displays during this Scene (on / off / leave as scheduled) | Scene editor › Ending | leave as scheduled | S |
| 71 | Never show list | Scenes › Never show list | empty | M |

### Schedule and Show now

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 72 | Scene to play | Schedule › editor | n/a | M |
| 73 | Days of week | Schedule › editor | every day | M |
| 74 | Start/end clock time | Schedule › editor | all day | M |
| 75 | Sunrise/sunset ± offset | Schedule › editor | off | S |
| 76 | Crosses midnight | Schedule › editor | automatic | M |
| 77 | Date range / yearly event | Schedule › editor | none | S |
| 78 | Priority (Normal / Special event / Always wins) | Schedule › editor | Normal | M |
| 79 | At end: finish gracefully / stop now | Schedule › editor | finish gracefully | N |
| 80 | Power schedule: displays on/off times (its own lane, not a Scene) | Schedule › Power lane | on 07:00, off 23:00 | M |
| 81 | Power schedule days and sunrise/sunset times | Schedule › Power lane | every day; clock times | M |
| 82 | Which Frames follow the power schedule | Schedule › Power lane | every current Frame, selected explicitly | S |
| 83 | Night off turns displays off (vs black screen only) | Schedule › Power lane | power off where the display supports it | M |
| 84 | Show now default length | Show now sheet | 1 h | S |
| 85 | Show now default Frames | Show now sheet | every current Frame, selected explicitly | S |
| 86 | Pause default length | Show now sheet › Pause | 1 h | S |
| 87 | Home Assistant may turn displays on during Night off | Schedule › Power lane › Rules | No | S |
| 88 | Home Assistant may turn displays off | Schedule › Power lane › Rules | Yes, any time | S |
| 89 | Scenes may turn displays on during Night off | Schedule › Power lane › Rules | No | S |
| 90 | A hold (console All off/on, Home Assistant power switch) lasts until (next scheduled change / set time / until resumed) | Schedule › Power lane › Rules | next scheduled change | M |
| 91 | Holds shown with who set them ("Off — by Home Assistant (Away mode)") and Resume schedule | Schedule › Power lane | n/a | M |

### Wall and Frame

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 92 | Wall name + room label | Frames › Wall tab | "Wall 1" | S |
| 93 | Wall photo backdrop | Frames › Wall tab | none | N |
| 94 | Frame name | Frame page › header | "Frame N" | M |
| 95 | Placement on the wall (where the Frame hangs) | Frames › wall layout | from Display size | M |
| 96 | Orientation | Frame page › Position (follows rotation) | from rotation | M |
| 97 | Corner positions (keystone) | Frame page › Position | full output | M |
| 98 | Rotation 0/90/180/270 | Frame page › Position | 0 | M |
| 99 | Overscan/crop per edge | Frame page › Position | 0 | M |
| 100 | Nudge step | Frame page › Position | 10 px | N |
| 101 | Test pattern on/off | Frame page › Position / Picture | on while editing | M |
| 102 | Fit override (per Frame) | Frame page › Photo fit | use Scene's | S |
| 103 | Minimum photo quality for this Frame | Frame page › Photo fit | strict (requirement); lenient needs a requirement change | S |
| 104 | Group name + members (manual or rule) | Frames › Groups | rooms automatic | S |
| 105 | Spanning (one picture across adjacent Frames, bezel gap) | Frames › canvas | off | N |
| 106 | Live view: what the Pi is presenting now | Frame page › header (and Home tiles) | on | M |
| 107 | Bulk actions on selected Frames (skip, restart, power test) | Frames › list | n/a | S |
| 108 | Display size (diagonal) (Frame display profile) | Frame page › Hardware | from the display's report | M |
| 109 | Resolution (detected + override) (Frame display profile) | Frame page › Hardware | detected | S |

### Display (moves with the panel)

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 110 | HDMI mode / refresh rate | Frame page › Hardware | best detected | S |
| 111 | Brightness (display hardware where supported, else picture adjustment) | Frame page (stored on the display) › Picture | 50 % | M |
| 112 | Contrast (display hardware where supported, else picture adjustment) | Frame page (stored on the display) › Picture | 50 % | M |
| 113 | Colour temperature | Frame page (stored on the display) › Picture | 6500 K | S |
| 114 | Gamma | Frame page (stored on the display) › Picture | 2.2 | S |
| 115 | Auto-dim with room light (from a Home Assistant sensor) | Home Assistant blueprint; native later | off | N |
| 116 | Power method: HDMI-CEC / DDC/CI / HDMI signal off (panel sleeps) / external switch via Home Assistant, with Test off/on | Frame page › Power (stored on the display) | best detected | M |
| 117 | Switch the display to this input on power-on | Frame page › Power (stored on the display) | on | S |
| 118 | Never power off a display that is showing another input | Frame page › Power (stored on the display) | on | S |
| 119 | Copy picture settings to other Frames | Frame page › Picture | n/a | S |
| 120 | Display moved or changed: picture and power follow it; the Frame is not ready until Position is re-checked | Frame page › Hardware (prompt) | prompted | M |

### Pi

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 121 | Pi name | Pis › Pi page | serial short code | S |
| 122 | HDMI port → Frame (binding) | Frame page › Hardware (also shown on Pi page, read-only link) | unbound | M |
| 123 | Replace with… (move Frame to new Pi) | Frame page › Hardware | n/a | M |
| 124 | Log level / debug bundle | Pis › Pi page › Logs | normal | S |

### Settings

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 125 | Pi software release (selected) | Settings › Updates | newest stable (automatic at setup) | M |
| 126 | Apply new Pi software (you click Apply; new releases pre-download) | Settings › Updates | manual (see Updates) | M |
| 127 | Maintenance window | Settings › Updates | 03:00-05:00 | S |
| 128 | Releases kept warm on Central | Settings › Storage and network boot | newest 3 + current + previous | N |
| 129 | Central cache size limit | Settings › Storage and network boot | 80 % of disk | S |
| 130 | Network boot mode (proxy / full) | Settings › Storage and network boot | proxy | S |
| 131 | Notification channels (email/push/webhook) | Settings › Notifications | none; push at setup | N |
| 132 | Alert after Frame Not showing for | Settings › Notifications | 15 min (scheduled hours only) | S |
| 133 | Daily digest for "Needs a look" | Settings › Notifications | on, 09:00 | N |
| 134 | Quiet hours | Settings › Notifications | Night off window | N |
| 135 | Owner account | Settings › People and access | created at setup | M |
| 136 | Members and roles | Settings › People and access | owner only | N |
| 137 | OIDC sign-in | Settings › People and access | off | N |
| 138 | Session length | Settings › People and access | 30 days | N |
| 139 | Nightly backup | Settings › Backups | on | S |
| 140 | Backups kept | Settings › Backups | 7 | S |
| 141 | Home Assistant: MQTT broker address, user, password, Test | Settings › Integrations | mqtt://homeassistant.local:1883 | M |
| 142 | Home Assistant can control Photo Wall | Settings › Integrations | on | S |
| 143 | Include Pi diagnostics in Home Assistant | Settings › Integrations | off | N |
| 144 | Remove from Home Assistant | Settings › Integrations | n/a | S |

## 12. Where a power user's expectation meets a written requirement

Recommendations only; none changes a requirement. Rows marked **owner** need his decision because the recommendation would change a requirement or bend one.

| Requirement | A power user expects | Recommendation |
|---|---|---|
| Authored edits default to the next Run ([live media](requirements.md#live-media-compatibility-and-preparation)) | Change seconds-per-photo and see it now | Keep the rule; add **Save and apply now** |
| "Once an assignment is scheduled and secured by its responsible Players, its content is locked for that execution" ([live media](requirements.md#live-media-compatibility-and-preparation)) | Skip, Previous and Never show act at once | They act at once on assignments still drawn from a live query (not yet secured). On a secured assignment, Never show takes effect from the next Run, and Skip is not offered. |
| U1: "Authored darkness stays allowed"; a failure never leaves a display dark ([failure visibility](requirements.md#failure-visibility-and-recovery)) | Night off, Pause and All off leave displays dark, and that is not a failure | **Owner:** amend U1 so intentional operational darkness (the power schedule, Pause, All off, a display powered off on purpose) counts as intended darkness and shows as Resting. This is a requirement change. |
| "A changed profile requires calibration revalidation before the new equipment is considered ready" ([installation model](requirements.md#installation-model)) | A new display just works, with a prompt to re-check | The requirement wins: the display-changed flow blocks the Frame's readiness until Position is re-checked; it is not just a prompt ([§6](#6-display-identity)) |
| "Every target intentionally affected by a Scene or its children must participate explicitly" ([composition](requirements.md#composition-and-spatial-authoring)) | "All Frames" as the default for the power schedule, Scenes and Show now | Editors pre-select every current Frame explicitly; a Frame added later joins only if the user chose the explicit "All Frames" group |
| "Operational state … remains distinct from authored content state" ([operations](requirements.md#operations-and-scope)) | A "Good night" Scene that turns the displays off | **Owner:** a Scene's "Displays during this Scene" setting stores operational power in authored content. It is offered as a deliberate exception under his "Or both" and needs his confirmation ([§7](#7-power)) |
| Landscape photos never on portrait Frames; cropping is no exception ([compatibility](requirements.md#live-media-compatibility-and-preparation)) | A fill option and portrait pairing | Fill crops only photos that already match; pairing two portraits is a layout, not an exception; the Photo fit section says so in one line |
| A 720p video never on a 40-inch Frame ([compatibility](requirements.md#live-media-compatibility-and-preparation)) | An adjustable floor and a skipped count | Keep it strict and always show the skipped count; a lenient floor would be a requirement change |
| The Installation owns policy and timezone ([installation model](requirements.md#installation-model)) | Location for sun times, units | Add location (optional) as house policy |
| The operator selects the Player release ([provisioning](requirements.md#player-provisioning)) | Automatic updates | Keep the existing release choices ([console DDD Part E](operator-console-ddd.md#part-e-v2-only-console-and-node-release-workflows-feature-layer)) |
| U2: photos within a couple of hours after a power cut | Minutes | Keep U2 as the bound; show per-Frame progress |
| No playback guarantee after a cold reboot without Central ([stateless Players](requirements.md#central-authority-and-stateless-players)) | The wall survives a server reboot | Accept; the display says "Can't reach the Photo Wall server" |
| U1: at minimum an error page | Last photo plus a small badge | Last photo plus a badge for photo problems; the error page for app or system faults |
| Not a second general-purpose photo library ([purpose](requirements.md#purpose-and-experience)) | Never show this photo again | A wall-only Never show list; nothing written back to Immich |
| Nothing on backup, phone use, captions, ordering, mats or a live view | All of them | [Operator experience](requirements.md#operator-experience) records what the owner stated; the rest stays design until he states it |

## 13. What this design gives up

- Picture and power settings move with the display. That needs each display to report a usable identity over HDMI; one that reports none is treated as new each time it is plugged in.
- Rooms are labels that make automatic Groups, not separate objects; Home Assistant areas cover the rest.
- Sensors and triggers arrive through Home Assistant rather than being built into Photo Wall; native auto-dim waits.
- Hardware brightness and power depend on each display: many TVs take power commands but not brightness, and some panels take neither.
- What is deferred, and what that costs, is the [roadmap's deferred list](roadmap.md#deferred-and-what-that-costs).

## 14. What it supersedes

- The [console UX design](operator-console-ux-design.md) of 2026-09-13 and its later notes, wherever they differ: Home first; the Everyday / Set up rail; one Frame page with the tabs of §5 in place of the Frame Inspector's facets; the console says Display again (§2); the commissioning layer and its open display-power precedence (its §7.5) are answered by §6 and §7; its §10 Q2 (recurrence deferred) and Q3 (no sensors, triggers or actuator registry) give way to repeating Schedules and Home Assistant inputs.
- [Decision 0018](decisions/0018-console-by-domain-and-design-system.md), as listed in [decision 0019](decisions/0019-first-principles-console.md#what-it-supersedes).
