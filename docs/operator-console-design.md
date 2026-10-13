# Operator console design (2026-10-09)

**Status:** as recorded in [decision 0019](decisions/0019-first-principles-console.md). Nothing here is built except what the [roadmap](roadmap.md#delivery-order) marks as landed; for display identity and power (1b), §5 to §7 and the catalogue rows #110 and #116–#120 say what was built and what was not; for picture settings and the Frame fit (1c), §5's Position and Picture rows, §6 and the catalogue rows #96–#99, #111–#114 and #119 do, with the picture settings as [0022](decisions/0022-picture-hardware-and-software-apart.md) split them into the display's own and Photo Wall's (catalogue row #145 added) and display tuning ([0024](decisions/0024-display-tuning.md)) added the test patterns and the signal range (rows #101 and #154); for real photo sources (4), §9's Photo sources line and the catalogue rows #8–#33 and #36 do. The [roadmap](roadmap.md) owns the delivery order, each journey step's status today, and the deferred list; [decision 0019](decisions/0019-first-principles-console.md) records the decisions and what they replace. The earlier [console UX design](operator-console-ux-design.md) is the historical gate record this document supersedes. The design-system layers, tokens, lint rules and look of [decision 0018](decisions/0018-console-by-domain-and-design-system.md) stand: every page here is built from that catalog, following the [design language](design-language.md) (principles, page templates, patterns, save models, status words and voice).

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
| **Frame** | A fixed spot on a wall. It holds where the picture sits there (its Position: four corners, Frame fit and rotation) and its display profile (size and resolution). It stays when the display or Pi is swapped. | Frame, with its Frame display profile |
| **Display** | The TV, monitor or bare panel at a Frame, recognised from what it reports over HDMI. Its brightness, contrast, colour temperature, gamma and power method move with it. Edited from the Frame page. | Panel, plus its display-power Actuator and the photometric part of Calibration |
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
| **Display** (with identity) | The display hardware at a Frame, recognised by the maker, model and serial it reports over HDMI (its EDID, the identity block every HDMI display sends). Its picture and power settings are stored on it and follow it to another Frame. | The owner: display hardware settings "could remain consistent to where the panel moves" | Panel. Panel had no stored identity; this gives it one. A display with no usable serial is recognised by maker and model, tied to the Frame it feeds ([§6](#6-display-identity)). |
| **Frame fit** | How the Frame's picture sits in its four corners: **Cover**, the default (the picture is never skewed: it is the smallest rectangle of the Frame display profile's aspect, upright or turned to the Frame's rotation, that covers all four corners), or **Corners**, an explicit choice (the picture is stretched exactly onto the four corners, the skewing fit every Frame had before 1c) | The owner, 2026-10-10: "a "cover" default is more likely plus an optional rotation angle" | Part of Position. Not the photo fit, which decides which photos fill the Frame and the crop of each ([Planner](module-planner.md#the-photo-fit-rule)) |
| **Opening** | The area inside the Frame's four corners: what the viewer sees. Nothing of the Frame is drawn outside it | Cover needs a name for what must be covered | The four corners (#97) |
| **Cover rectangle** | In Cover, the Frame's picture: the smallest unskewed rectangle of the Frame display profile's aspect, at the Frame's rotation, that holds the whole opening. The geometry is in [architecture](architecture.md#player-and-rendering) | The geometry is stated once | The Frame display profile's aperture |
| **As the display has it** | A display (hardware) setting nobody set in Photo Wall, or one handed back: nothing is written to the display for it | So a new display is never changed on sight | The catalogue's old default "50 %" |
| **Display (hardware)** and **Photo Wall (software)** | The Picture tab's two sections ([0022](decisions/0022-picture-hardware-and-software-apart.md)). Display (hardware): the picture settings the display itself keeps and answers over DDC/CI (brightness, contrast, colour preset, colour gains…), each in the display's own count. Photo Wall (software): the four the Player applies to the picture it sends, the same on every display | The owner, 2026-10-12 (chat): "i want to independently control the hardware from the software" | "On the display" and "Photo Wall picture adjustment", the words of 1c, which these replace. The section names are the design's; the owner's answer did not rule on them |
| **Picture list** | The fixed list of display settings Photo Wall asks a display about (ddcutil's colour group plus sharpness; `contracts/node_picture.py` `PICTURE_LIST`). Not on it: power, input, volume, maker codes E0–FF, position and size on screen | Bounds what is asked and offered; owner, 2026-10-12 (chat): "the "picture" screen should have any hardware settings related to "picture"" | none |
| **Print look** | The Photo Wall (software) settings that make a photo read as a print: Saturation, Soft highlights, Black floor and a LUT with its amount ([0023](decisions/0023-print-look.md)). Each starts at no change | The owner, 2026-10-12 (chat): "It should look like a printed paper photo"; "the print look belongs to the Display more than the Frame" | Photo Wall (software) settings; "print look" only names the group of rows |
| **Soft highlights** (highlight rolloff) | How softly values Photo Wall's brightness pushes past white ease into the Display's white; 0 % is the old hard clip, and it acts only above 100 % brightness | So brightness above 100 % no longer flattens the brightest parts | none |
| **Black floor** | The darkest a pixel may get, as a share of white light: a print's darkest ink (0.0 to 10.0 %) | Paper never reaches a screen's black | none |
| **LUT** | The owner's word (2026-10-12): a colour lookup table, made by Central from a paper profile, that gives each colour the look that paper prints it with | "a LUT is an enhancement" | none |
| **Paper profile** | The colour profile (an ICC file) a paper maker publishes free for each paper and printer; the one thing a LUT is made from | What a LUT comes from | none (an industry term) |
| **Mat** | On a Frame: how far the mat's inner edge stands in front of the picture (its depth, in millimetres) and the lights that fall on it. Part of the Frame's place on the wall, set on Position | The owner, 2026-10-12 (chat): "the mat shadow is definitely a Frame quality" | part of Position |
| **Light** | A light the room shines on one Frame: where it comes from, its steepness, softness, strength and colour. The Pi draws the shadows it casts from the mat; lights never change the photo's colours | The owner, 2026-10-12 (chat): "cool white light coming from the side AND warm white coming from the top" | none |
| **History** | On the Picture and Position tabs: what that Display's picture, or that Frame's position, mat and lights, were, newest first, each with its time, what made it and its values; **Restore** loads one into the editor and Done still keeps it ([0025](decisions/0025-configuration-snapshots.md)) | The owner, 2026-10-13 (chat): "i like the value of 3 as "configuration snapshots" so it keeps a history of what has been done in the past." | Configuration snapshot (his word): one Frame at one time, composed from the two histories; the agent reads it, the tabs list the histories (code `TuningSnapshot`, picture history, Position history) |
| **Tried, not kept** | A History entry for values sent to the wall while tuning that Done never kept (a draft, and a test pattern with it) | So a photo taken while tuning can be read later, and nobody mistakes a try for what is kept | A sent draft entry (code `sent-draft`) |
| **Put back** | A History entry for the wall returned to the kept values when a try ended without Done (Revert, leaving the tab, a closed browser) | So the list says why the wall changed with nothing kept | A sent back entry (code `sent-back`) |
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
| **Overview** | Today's timeline for this Frame with a now line; Why this is playing; its Groups; recent problems; anything unfinished in its setup. **Photos (built in 5a):** one group per Scene that targets this Frame and each of its Sources (titled with the Scene, the Source and the Scene's crop limit, since each Scene judges at its own limit; read again every 15 s while shown): "N photos fit this Frame", then one line per reason a photo is skipped here, with its count: "doesn't fit this Frame's shape", "too small for this Frame", "a face would be cut", "the interesting part would be cut", "this Frame shows no videos", "not measured yet". No orientation words: fit is the ratio of the photo's sides against the Frame's ([Planner](module-planner.md#the-photo-fit-rule)). |
| **Position** | The display shows a test pattern with the Frame name, edges, corner positions and actual output mode. Drag four corners, or an edge to move its two corners together (owner, 2026-10-12: "i want to be able to drag "edges" of a frame position and not just the corners"), or nudge the whole picture, a corner or an edge with arrow keys in 1, 10 or 50 pixel steps; Reset to full screen (keeps the fit, sets the angle to 0). No session timeout while the tab is open. **Fit:** **Cover**, the default for every Frame, or **Corners** ("Stretch the picture exactly onto the corners"), an explicit opt-in; owner, 2026-10-10: "Switch to cover. It's a more sensible default, and skewing transforms or rotations should be an explicit opt in". **Rotation:** in Cover any angle, with buttons for 0, 90, 180 and 270 and a fine control to 0.1 degree; in Corners the four buttons only. New Frames start on Cover at 0. Nothing of a Frame is drawn outside its four corners, so a Frame never paints over a neighbour on the same display, and the display's area outside every Frame stays black whatever the Picture tab sets; owner, 2026-10-10: "Black/nothing. Remember that a display can have multiple frames, so outside the configured frame is "someone else's territory"". Save keeps the fit and the angle and nothing else: the Frame display profile (the Hardware tab, or the display-changed card) is the one source of the Frame's aspect in either fit, the cover rectangle has that aspect at every angle, and no angle is refused for its orientation; owner, 2026-10-11: ""landscape" and "portrait" are descriptions of the aspect ratio. whereas the angle of rotation is a quality of the way frames are hung on the wall, or possibly some unique conditions of how displays are mounted within the matte". **Trim edges is gone**; owner, 2026-10-10: "Yes. The corners feel like enough". **Built in 1c**; every Frame moved to Cover when it was deployed ([§6](#6-display-identity)). **Mat** (the print look, [0023](decisions/0023-print-look.md)): below the fit, the mat's depth (0 to 20 mm in 0.5) and up to four lights, each with where it comes from (clockwise from the wall's up, so it turns with the Frame), steepness, softness, strength and colour, "Add a light" and "Remove light N"; "No lights: no shadow." Part of the Position session: drawn live, kept by Done, put back by Revert; a mat-only change never changes which photos the Frame gets. **History** ([0025](decisions/0025-configuration-snapshots.md); owner, 2026-10-13: "View and restore (Recommended)"), below the editor: every change to this Frame's position, mat and lights, newest first by day, each with its time, what made it ("Kept (Done)", "Kept (screen size changed)", "Changed outside Photo Wall", "Tried, not kept", "Put back"), one line of what differs from the row below (the Picture tab's rule: "Top-left 6.3 % across, 0.0 % down (120, 0 px)", "Mat depth 6.0 mm"; all of it for the oldest shown) and every value under Details, the corners and the lights' count included; the tries of one session follow one another as one run, its newest shown and "Show N earlier tries from this session" opening the rest; read again every 2 s while the tab is shown and after Done or Revert, with "Show older". A row not kept is dashed and muted. **Restore** replaces the draft with that entry while the editor runs ("Open while the editor runs" otherwise); the display shows it live, Done keeps it, Revert returns to the kept Position. An entry made for another screen size ("Made for another screen size (… px).") or on another display ("Made on another display.") cannot be restored, and its row says why. Restoring the newest kept entry changes nothing, so Done is not offered. |
| **Picture** | Test pattern and Signal range first ([0024](decisions/0024-display-tuning.md)), then two sections, never coupled ([0022](decisions/0022-picture-hardware-and-software-apart.md)), then Copy to other Frames… The values belong to the display and say so ("These settings belong to the display (Samsung 55", serial …4K2) and move with it"). **Display (hardware):** each setting on the [picture list](#3-new-terms) that the display answered when its Pi asked, a level as a percent of the display's range with its own count second (owner, 2026-10-12, picked "Percent, raw count second"): a slider from 0 to the display's maximum ("46 % (117 of 255)"), a picker of exactly the choices the display names for a list setting ("6500 K"), and a read-only line for a list setting whose choices the display does not name. Each offers **As the display has it**, which hands the setting back to the display's own buttons: nothing is written for it. Until the Pi has asked a connected display, the section says "Checking what this display offers" (and, for a display never checked that is not connected, "Photo Wall checks what this display offers when it is connected"), and a value set here shows alone. What a display offered at its last check stays known while it is away. A setting set here that the display no longer offers says "Not offered by this display at its last check". **Photo Wall (software):** brightness and contrast from 0 to 150 % (100 % changes nothing; owner, 2026-10-12, chat: "0–150%"), colour temperature (3000 to 9300 K; 6500 K changes nothing), gamma (1.8 to 2.6; 2.2 changes nothing), and the [print look](#3-new-terms) ([0023](decisions/0023-print-look.md)): Saturation (0 to 150 %; 100 % changes nothing), Soft highlights (0 to 100 %; 0 % changes nothing; its row says it "acts only above 100 % brightness", where white can pass white: the Pi's rule, stated, never decided by the console), Black floor (0.0 to 10.0 % of white light; 0.0 % changes nothing) and LUT ("Makes photos look printed on one paper": None, or one on the LUTs list, with LUT amount 0 to 100 % shown once one is chosen; "No LUTs on the list yet" while the list is empty, with a link to Settings › LUTs to add one). Each slider shows its value; the notes say only what changes nothing and where a setting acts. A chosen LUT the Pi has not fetched yet: the sliders show at once, and Done waits for the LUT. Both groups act at once: the display can sit at 46 % (117 of 255) while Photo Wall adds 114 % (owner, 2026-10-12, chat: "i might set hardware brightness to 46% AND the software brightness to 114%"). **Test pattern** (display tuning, [0024](decisions/0024-display-tuning.md); owner, 2026-10-13, chat: "have a button in Central that shows pure white on the frame"): Off, White, Grey ramp or Black level, shown instead of photos on every Frame of this display while the tab's session is live, Position's grid included; Done, Revert, leaving the tab or 10 minutes on the Pi's own clock take it off, and it is never stored. White and Grey ramp go through the Photo Wall (software) values and the Frame's mat, so they show what photos get. The Black level is the raw signal, through no Photo Wall step and no mat. Under each pattern the tab says in one line what to do with it: under White, match a sheet of white paper taped beside the Frame; under the Grey ramp, tell every step apart; under the Black level, how to read it while switching Full and Limited. The wording's one home is the tab (`PATTERN_HOW` in `central/console/src/domain/frame-picture.tsx`); this page does not repeat it. **Calibration targets** ([0026](decisions/0026-calibration-targets.md); owner, 2026-10-13, chat: "build the calibration targets"): two more choices on the same Test pattern, the screen target pre-software (drawn raw, so the display's own controls are judged) and post-software (through the Photo Wall (software) values, the mat shadow skipped and the paper's dark box raw black), offered side by side and never ordered (owner, 2026-10-13: "thats the users choice -- not something that the software enforces"). Each has its line under the control like the other patterns, in the same one home, and with either one chosen the tab links to print the paper target on Letter or A4 (`GET /v1/operator/calibration-target/paper.pdf`). The tab reads nothing from a photo: the agent does ([runbook](runbook.md#calibration-targets)). **Signal range** (owner, 2026-10-13, chat: "can we set the pi to output full range? Can we make it configurable per display?"): Auto (the Pi's choice, Limited for TV modes), Full or Limited: which numbers the Pi's HDMI signal uses for black and white. It belongs to the display like the two sections, rides the same session (kept by Done, put back by Revert or leaving the tab), and acts on the Pi, which starts the picture again on every display of that Pi (one Weston drives both HDMI ports): the tab says a change restarts the picture on every display of the Pi, with no photos until it is back. A range the Pi could not set shows with the range the display still gets; a port with no range setting says so. **Built in 1c, split in the picture-split delivery** on the Live model ([design language](design-language.md#save-models)), the Test pattern and Signal range in display tuning: each change reaches the display as it is made, at most 2 per second while dragging and the last on release; **Done** unlocks once the Pi reports it has written the latest hardware values, the Player has presented the latest software values and test pattern, and the Pi has answered the latest signal range (any answer, a failed one included); **Revert**, or leaving the tab, puts back what was in force before (Central ends the session 5 s after its last edit or keepalive). A write the display refuses is reported ("The display did not take it") and nothing else changes. The Pi reads the display's settings again from time to time, and a set value that differs from the stored one says "Changed on the display's buttons" with the display's value, left alone until that setting is set again. "Applies when the display is connected" while it is not; its values can still be set. Revert of a hardware setting that was *as the display has it* puts the display back once to what the Pi read just before its first write in the session, and the setting stays *as the display has it*; with no such read yet the display keeps the session's value. **Copy to other Frames** copies every Photo Wall value, the print look's included, and each display value set in Photo Wall where the target's display offers it the same way (owner, 2026-10-12, chat: "Both (Recommended)"; "Only ones set in Photo Wall (Recommended)"): a list value only when the target lists the same value and name, a level at the same share of its range (46 of 100 becomes 117 of 255, a design choice). Anything else stays as it is on the target and is named as skipped. Never position, and never the signal range (it follows each display). **Input** (owner, 2026-10-12, chat: "Let’s put it on both. That seems like the optimal UX"): below the picture settings, the Power tab's "Switch the display to this input on power-on", the same stored setting and the same row, saved on change outside Done and Revert. **History** ([0025](decisions/0025-configuration-snapshots.md); owner, 2026-10-13: "View and restore (Recommended)"), below Input and shown once a display is known: every change to this display's picture, newest first by day, following the display to another Frame. Each row has its time, what made it ("Kept (Done)", "Kept (Copy from another display)", "Changed outside Photo Wall", "Tried, not kept", "Put back", "Kept values sent again"), a summary (what differs from the row below, the Position tab's alike; a kept row on the tried row of the same values reads "Same values as the change below"); the tries of one session follow one another as one run, its newest shown and "Show N earlier tries from this session" opening the rest, and every value under Details: display settings as "Name (display)", each Photo Wall value, the LUT ("made with an older recipe" when it was), the signal range and a try's test pattern. Read again every 2 s while the tab is shown and after Done or Revert, with "Show older"; when the values changed outside Photo Wall: "This display's values changed outside Photo Wall since its last kept change. The next Done records them." **Restore**, while the editor runs, loads every value of the entry into it, live on the display, except the test pattern; Done keeps it, Revert puts back. It says what did not come back: "Not restored: … (this display does not offer them now).", "The LUT is no longer on the list: the current one stays.", "The LUT is made with today's recipe." |
| **Power** | Power method: HDMI-CEC, DDC/CI, HDMI signal off (the display may sleep) or a smart plug through Home Assistant, the detected one chosen. **Test: turn off / turn on**, reading "Display confirmed off" or "Display didn't answer". Switch the display to this input on power-on (on; also on the Picture tab, the same setting). Never power off while the display shows another input (on). Then, read-only: "Now on · next off 23:00, set by Schedule › Power lane", and any hold ("Off — by Home Assistant (Away mode)"). When displays are on is not set here. **Built in 1b:** the method (Best detected, HDMI-CEC, DDC/CI or HDMI signal off; no smart plug until [3](roadmap.md#3-home-assistant)), the two switches, and **Test: turn off** and **Turn on** ([§7](#7-power)), with "Waiting for the Pi" until the Pi answers the change that carried the test (after Turn on, the change that dropped it). In place of the Power lane line, which waits for [2a](roadmap.md#2a-power-night-off-and-all-offon), the tab shows what the Pi reports in force ("Now: Off · by a test on this tab, 5 min left") and the method in use. |
| **Hardware** | The Frame's display profile (size, resolution, detected with an override); the Display (model, serial, HDMI mode); "Fed by Pi pw-3f2a · HDMI 1" with **Replace with…**; when a different display appears, the [display-changed flow](#6-display-identity). **Built in 1b:** the Display section (make, model, serial or "Recognised by make and model on this Frame", and the modes its EDID lists, read-only) and the display-changed card. The HDMI mode (#110) is shown, not set: nothing on the Pi applies a mode yet. |

How each tab saves is assigned in the [design language's concept → UX table](design-language.md#8-concept--ux-translation) (rows B4, B5, B6, D3, G4); the models themselves are its [save models](design-language.md#save-models).

**Unbound Frame:** every tab shows, and Position, Picture and Power say "Choose which Pi and HDMI port feeds this Frame" with a picker. As built, the Power tab links to the Hardware tab, where the picker is, rather than embedding it. Binding happens only here (unchanged from 0018).

**Errors** name the failing part inline, for example "Pi online; the display stopped answering on HDMI at 21:04. Check the display's input. [Restart photo app] [Reboot Pi]".

Pattern: the screen-settings page every signage product has, split the way a TV's own menu is. Unlike those products, the Frame and the Pi are separate, so replacing a Pi keeps every setting.

## 6. Display identity

- A Display is identified by the maker, model and serial in its EDID. When a Pi reports a display on an Output, Central matches it to a known Display or records a new one. Central alone decides "same display"; the Pi only reports what it reads.
- **A display with no usable serial** (the serial is blank or zero, as on the test Pi's monitor, or another display of the same maker and model reports the same serial at the same time) is recognised by maker and model, **tied to the Frame it feeds**. It stays the same Display through a Pi swap or a port change; moved to another Frame it starts over, with default settings. On a port no Frame is bound to it waits, as no Display, until the port is bound. Owner, 2026-10-10 (q2 on the [1b gate page](https://claude.ai/artifact/DjZTF9HGKWLVy6kexikeUp)), choosing this rule, noted: "There might be duplicate make/model attached to both pi HDMI’s"; so two identical monitors with no serial on one Pi's two ports, bound to two Frames, are two Displays. Cost: two such monitors swapped between Frames are not noticed, and a serial once seen twice at the same time is never trusted again for that maker and model.
- An unplug, a short drop of the HDMI signal (a monitor going to standby can drop it for about ¼ s) or an identity the Pi cannot read never changes which Display Central records on a port; only a readable, different identity does.
- Stored on the Display, and following it: the display (hardware) values set in Photo Wall, by setting, the Photo Wall (software) values and the signal range ([0024](decisions/0024-display-tuning.md)); power method, switch input on power-on, never power off while another input shows. Picture travels in its own picture document per HDMI port with its own change number, never in the Output document, so it never re-runs power, and it is never a Position commit: it leaves the Frame's position, its readiness and the Player's configuration alone.
- Stored on the Frame, and staying put: Position (four corners, Frame fit, rotation) and the Frame display profile (physical size and resolution), which the requirements place on the Frame ([installation model](requirements.md#installation-model)).
- **A display changed or moved here:** its picture and power settings come with it, and the Frame is **not ready** until Position is re-checked. The requirements make this a block, not a prompt: "A changed profile requires calibration revalidation before the new equipment is considered ready" ([installation model](requirements.md#installation-model)). Readiness is worked out, never stored: a bound Frame is ready when its latest Position commit, made since its last bind or profile change, names the Display now seen on its port. The display-changed card on the Hardware tab has two steps: **Confirm Frame profile** (pre-filled from the new display's preferred mode, turned to the Frame's orientation; Edit Frame profile corrects it, and the profile is editable while the display is changed even on a bound Frame), then **Re-check Position**. Confirming the profile is the card's first step, not a gate: an unchanged profile leaves no record that it was confirmed (errata E-1B-9).
- **When 1c was deployed** (migration 075), every Frame moved to Cover: its corners became the opening, its rotation was reset to 0 (owner, 2026-10-10: "Every frame should reset to 0 rotation") and its trim was dropped. Each Frame's picture changed, so **no Frame is ready until its Position is saved again**, and a Frame that is not ready shows no photos (only ready Frames' displays get media). A Frame that relied on a quarter turn (a portrait Frame on a landscape display, or the reverse) must be turned again before its Save is accepted. Its old brightness, the Photo Wall dimmer stored with its position, moved to its Display: 100 % became *as the display has it*, any other value a picture-adjustment brightness of half the old percent (150 % became 75 %). **When the picture split was deployed** (migration 092, [0022](decisions/0022-picture-hardware-and-software-apart.md)), a brightness or contrast that acted as picture adjustment became a Photo Wall (software) value of twice the old percent, capped at 150 % (100 % is now no change); one that acted on the display was forgotten (the display keeps it, but Photo Wall no longer puts it back until it is set again); open picture sessions were dropped. **When display tuning was deployed** (migration 094, [0024](decisions/0024-display-tuning.md)), every Display started at signal range Auto, which changes nothing, and open picture sessions were dropped again.
- When 1b was deployed, each Frame took the first display reported on its port as the one its Position was set against, so a display swapped before then is not caught.
- A display whose EDID cannot be read at all is not recorded; the Frame keeps the Display it had.
- The settings are edited from the Frame page; there is no separate Display page.
- Two channels inside the HDMI cable carry commands to a display: **HDMI-CEC**, the remote-control channel most TVs answer (power, input), and **DDC/CI**, the settings channel most monitors answer (brightness, contrast, colour settings, often power).
- Asked whether brightness should drive the display hardware, the owner answered "As much as possible." So power goes through HDMI-CEC, DDC/CI, HDMI signal off, or a smart plug through Home Assistant. Picture has two groups, set apart ([0022](decisions/0022-picture-hardware-and-software-apart.md)): the display's own settings over DDC/CI, those on the picture list the display answers, and Photo Wall's four, applied by the Player on every display. Software adjustment is never called measured panel brightness ([failure visibility](requirements.md#failure-visibility-and-recovery)). The Pi image carries `cec-ctl` and `ddcutil` and the DDC/CI driver since 1b. The test Pi's portable 1920x1080 monitor answers DDC/CI (power, brightness, contrast) and not CEC; how to operate both by hand is the repo skill [display-control](../.claude/skills/display-control/SKILL.md).

## 7. Power

Asked whether power should be its own concept or part of Scenes, the owner answered: "I meant power as a unique concept, but it could go either way. Or both." This design does both.

**Who can turn displays on and off:**

1. The **power schedule** in the Schedule page's Power lane: on and off times (clock or sunrise/sunset ± minutes), days, which Frames (the editor pre-selects every current Frame explicitly; a new Frame joins only if the user chose the "All Frames" group), and Night off (default off 23:00 to 07:00, every day, powering displays off where they support it and showing black otherwise).
2. A **Scene** that asks for its displays on, off, or "leave as scheduled". **Not now** (owner, 2026-10-12: "Not now", [system gate](https://claude.ai/artifact/HHNRy2Hs1CpEm5VVK58DWm)): it would store an operational setting (display power) inside authored content, which the requirements keep apart ([operations](requirements.md#operations-and-scope)). Night off, holds and a dark Scene cover Good night; the power timetable can take Scene requests later.
3. The console's **All off/on**, a hold.
4. **Home Assistant**, through its switches or the request topic.

**Built in 2a: the power timetable** ([#86](https://github.com/mcurcio/photo-wall/pull/86), [0021](decisions/0021-power-timetable-and-repeating-schedules.md)). Central decides whether each display should be on; the Pi switches at the set times on its own clock and reports what it did. Per HDMI port Central writes the **power timetable** in the Output document ([Player architecture](player-architecture.md#display-identity-and-power-1b)): the power wanted now, the next 7 days of off and on set times, and how a display is dark (displays off, or black). It is worked out from three layers:

- **Night off** underneath (default 23:00 to 07:00, every day, every current Frame chosen one by one, displays off); a Frame that does not follow it is on.
- **The newest hold on top** (All off, All on), until each Frame's own next change (owner, 2026-10-12: "Each Frame's hold ends at its own next change; a Frame with no Night off holds until 07:00 tomorrow", [remaining-work page](https://claude.ai/artifact/C7wGab1zo8UuCMYfaQh6u2)): a Frame that follows Night off until Night off's next on or off time; a Frame that does not until Night off next turns displays on (its on time, 07:00 by default, the next one after the press); or until resumed, as #90 says. **Resume schedule** ends it at once.
- **1b's test on top of both**: Test: turn off lasts 5 minutes, or less if **Turn on** is pressed, counted on the Pi, so a test ends even when Central is away (owner, 2026-10-10, q1 on the [1b gate page](https://claude.ai/artifact/DjZTF9HGKWLVy6kexikeUp)).

With Central away the Pi keeps following the set times for up to 7 days (owner, 2026-10-12: "Yes, send 7 days of set times", [overview gate](https://claude.ai/artifact/8DXMFAosB3HKY7m35fmHno)), then leaves its displays as they are. A daily rewrite that wants the same as the Pi is doing writes nothing, so it never fights a person with the remote. The result says what the display or the Pi confirmed and never claims what is on the screen: "Display confirmed off" or "on" (the display read back the state), "Picture stopped; the display may sleep" (signal off), "Display didn't answer", "Left on: the display is showing another input" (#118), and, while Central cannot reach the Pi, when it last heard from it. With "Show black" (#83), or when no power method can switch the display, the Pi shows an opaque black from Display Host instead. The Frame page's Power tab and Why is this playing read one power line ("Resting: Night off until 07:00", "Held off by the console until 23:00"); a display that did not answer is a power problem, never Resting. Home Assistant (3) adds a hold source to the same timetable.

### The hold rule

How long each power request lasts. This is the one statement of the rule; other sections link here.

| Request | Lasts until |
|---|---|
| The power schedule | Always in force underneath; it is what the others return to |
| A Scene's request | Not now (owner, 2026-10-12, above) |
| Console **All off** / **All on** | Each Frame's own next scheduled power change, a Frame outside Night off until Night off's next on time (the default; see the guard below); built in 2a |
| A Home Assistant **power switch** (per Frame, or All on/off) | The same as the console: the next scheduled power change by default |
| Home Assistant **Keep all off** (away mode) | The switch is turned off. Shown as "Off — by Home Assistant (Away mode)". |
| A message on the request topic | Its `until` time ("Off — by Home Assistant (film night) until 23:30"); a message with no `until` is a hold like a power switch |

**Which request wins:** the newest one, until it ends; then the power schedule takes over again. The guard settings adjust it. Asked who should win between the schedule, Home Assistant and a person, the owner answered: "I don't know. I'm hoping for configuration options built in sensible defaults".

| Guard (Schedule › Power lane › Rules) | Default | Why this default |
|---|---|---|
| Home Assistant may turn displays on during Night off | No | A motion sensor at 2 am shouldn't light up the room. A person can still press All on in the console. |
| Home Assistant may turn displays off | Yes, any time | Turning off is always safe (away mode, a film starting) |
| Scenes may turn displays on during Night off | Not now (goes with Scene requests, owner 2026-10-12) | Night off means dark unless a person decides otherwise |
| A hold (console All off/on, or a Home Assistant power switch) lasts until (beside All off and All on on the week page, owner 2026-10-12: "the setting moves beside All off/All on") | Each Frame's next scheduled power change (built in 2a with the other choice, until resumed; "a set time" is not built) | Like a thermostat hold: nothing stays off forever by accident |

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
- **Show now** (a sheet from Home, a tile, or the phone tab; as built in 4 a two-step flow on the Now page whose Scene step names the Scene's Photo sources in one line, "Photos from garden (Checked Sun 03:39)", with no check action and no "Runs" heading). Pick what (an album, person, Photo source or Scene), where (Frames or Groups; every current Frame pre-selected explicitly) and how long (1 hour, until tonight's Night off, until I stop). It is an overlay: the schedule beneath keeps advancing and is revealed in its current state ([progression](requirements.md#progression-visibility-and-target-control)).
- **Scenes and Photo sources.** A form with a live preview on a chosen Frame, in sections What, Where, Timing, Order, Photo fit, Video, Captions, Ending, Advanced (the Order rows and the Crop limit are built in 5a on the flow's Playback step). Photo sources include by albums, people, place, dates, favourites, tags, media types and partners, and exclude albums, people, tags and screenshots (screenshots on by default); archived, hidden and locked photos are never included, one fixed line rather than switches; the pool, its counts and its thumbnails fill as Immich is read, before and after saving. **Built in 4** ([media module](module-media.md#photo-source-queries)); after the designer review (follow-ups D1, 2026-10-11) the Photo source editor is the [T4 Editor](design-language.md#t4-editor): one title ("New Photo source" or the Source's name), the photos beside the filters and kept in view from `lg` (under them from `md`; on a phone a count line that opens them in a sheet), each thumbnail at its photo's own ratio, one check time, Photos · Live photos · Videos as one toggle group that says on or off in words, Save in a bar that stays at the bottom, and an empty pool that names the query's choices, each with a Clear. The **Sources** list is cards (four photos, the count, what it includes, the check's state in its chip; the name opens the editor; More holds Check now and Delete, which an Undo toast can take back for 10 s). The Scene editor's Photos step is the same cards, chosen by checkbox (a radio for a hand-picked Scene), with "New Photo source" as the last card. The Never show list shows every photo on it with **Show again**.
- **How photos fit the Frames** (follow-ups C1 and C2, 2026-10-11; owner: "the real solution should be more visual and allow me to preview the content selections and transient Frame compatibility. ALSO make sure that compatibility is measuring against the Frame output and not the Display"; "this preview idea could apply at a few different moments"). One read on Central, the fit analysis, answers how a selection fits the Frames, every input saved or a draft; one design-system pattern, the **Fit board** ([design language §4](design-language.md#4-component-and-pattern-catalogue)), draws it, and each moment decides what is previewed: the **Photo source editor** (the unsaved Source's photos on every Frame, at the house crop limit or as a Scene using it judges them), the **Sources** list (a line per Source), the **Scene editor** (Frames before Photos; the board on Photos and Playback, the crop limit's drag answered live; Review's compact board with "Raise the crop limit to N %"), **Show now** (a line), the **Frame page** (Overview › Photos: one row per Scene with how it relates to now; the Position and Frame profile editors judged with the unsaved values) and **Settings › Photo fit** (the house's tuning values, judged before Save). A sample opens the **photo sheet**: the photo with each Frame's window and verdict, the original's type, size and bytes, Immich's copies with their sizes as measured ("size known after first use" before then) and the copy the wall's prepared one was made from; no "Open in Immich" (it would put Immich's id in the page). Every count is of the Frame output, the Frame's picture on its Output, never the Display's mode; a Frame whose Pi has not reported its Display's size is judged by shape only, and says so.
- **Frames.** A tab per Wall plus "+ Wall"; a canvas to real scale in cm or inches; Identify on each Frame; overlapping Frames flagged; Groups.
- **Pis.** "New Pis waiting: which Frame did you plug this into?" (one tap binds). A table with Frames fed, status, software version and last heard. The Pi page has rename, Identify, Restart app, Reboot (requested → reboot started → back online), logs and Retire. Boxes too small say so ("2 GB, needs 4 GB").
- **Setup checklist.** Server checks (a Pi release picked automatically on a wall that never had one), Immich URL and key with a Test showing counts, house basics, first Pi, first Frame with Position, a pre-filled Favourites slideshow, a schedule and Night off. Each step turns green from a real check, never from a click. It hides itself when done.
- **Settings.** House; Photo library (the Immich connections, set and checked in the console, **built in 4**: each connection a card titled by its name; a connection that needs a look shows one problem card whose chip is its only status and whose one button is its fix, the same button Needs attention shows as "Archive (Immich): Needs a look since Sun 03:39"; **Add a connection** opens the form; the connection the deployment sets in Central's environment reads "Set by your deployment", read-only like a provisioned data source, with its health and Check now and no Change, Paste a new key or Remove, [runbook](runbook.md#the-deployments-immich)); Updates, which follow the release choices of [console DDD Part E](operator-console-ddd.md#part-e-v2-only-console-and-node-release-workflows-feature-layer); Integrations (§8); Backups; Storage and network boot.

## 10. How it behaves

How the console saves, speaks status and words its errors is the [design language](design-language.md), the one home for UI rules: the save models ([§5](design-language.md#save-models)), the six status words and their place on 0018's severity scale ([§6](design-language.md#6-status-and-severity)), and the error template with its plain-words rules ([§7](design-language.md#the-error-template)). Each journey step's page template, patterns and save model are in its [concept → UX table](design-language.md#8-concept--ux-translation).

## 11. Settings catalogue

Every setting a power user expects, with exactly one home: 136 settings (numbered to 144; #99 was removed in 1c, and #47, #49–#52, #102 and #105 in 5a, their numbers kept free; #48 became the Crop limit in 5a). **Lives on** is page › section. **P** is priority (M must, S should, N nice). Whether each exists today is tracked by the [roadmap](roadmap.md#settings-status), not here. Display size and resolution are filed under Wall and Frame because the requirements make them part of the Frame display profile.

### House

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 1 | House name | Settings › House | "Home" | N |
| 2 | Timezone (built in 2b) | Settings › House | Central's own zone at deploy; the browser's is offered when it differs | M |
| 3 | Location (for sunrise/sunset; built in 2b, latitude and longitude, Clear) | Settings › House | unset; sun times cannot be chosen until set | S |
| 4 | Units (cm/in; built in 2b) | Settings › House | cm | S |
| 5 | Clock format 12/24 h (built in 2b) | Settings › House | 24 h | S |
| 6 | Language | Settings › House | from browser | N |
| 7 | Show setup checklist | Settings › House | auto-hide when done | S |

### Photo library

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 8 | Immich server URL (built in 4; "/api" added when missing, plain http accepted; the deployment's connection is set in Central's environment and shown read-only, [runbook](runbook.md#the-deployments-immich)) | the deployment (`PHOTO_WALL_IMMICH_URL`), else Settings › Photo library | none | M |
| 9 | API key (built in 4: stored in Central's database, written and never read back; the console shows its last four characters, [decision 0020](decisions/0020-immich-key-in-central-database.md); the deployment's connection takes it from Central's environment) | the deployment (`PHOTO_WALL_IMMICH_API_KEY`), else Settings › Photo library | none | M |
| 10 | Check now + counts (built in 4: Save checks at once; Immich's photo, video, album and people counts; health over time as Needs a look with the fix) | Settings › Photo library | n/a | M |
| 11 | Additional Immich users/connections (built in 4: one connection per Immich user) | Settings › Photo library | none | S |
| 12 | Look for new photos every (4 checks every 15 min; no control yet) | Settings › Photo library | 15 min | N |

### Photo source

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 13 | Name (built in 4) | Scenes › Photo sources › editor | from first chip | M |
| 14 | Connection (which Immich; built in 4, hidden when there is one) | Photo source editor › top | the only one | S |
| 15 | Include albums (built in 4: in every chosen album) | Photo source editor › Include | none | M |
| 16 | Include people (built in 4) | Photo source editor › Include | none | M |
| 17 | People must all appear (built in 4 as all, fixed: Immich's meaning; for any of them, a second Source on the Scene, #36) | Photo source editor › Include | all | N |
| 18 | Include places (built in 4: one place, its city, state and country together) | Photo source editor › Include | none | S |
| 19 | Date range (fixed; built in 4) | Photo source editor › Include | none | M |
| 20 | Relative dates ("last 2 years"; built in 4, counted in UTC until 2b) | Photo source editor › Include | none | S |
| 21 | On this day (moved in 4: an order, not a filter; built in 5a as one of #43's orders) | Scene editor › Order | off | S |
| 22 | Favourites only (built in 4) | Photo source editor › Include | off | M |
| 23 | Tags (built in 4: every chosen tag, nested tags included) | Photo source editor › Include | none | M |
| 24 | Minimum rating (deferred in 4: Immich 2.5 matches one exact rating only) | Photo source editor › Include | none | N |
| 25 | Media types (photos / videos / live photos; built in 4) | Photo source editor › Include | photos, live photos and videos | M |
| 26 | Exclude albums (built in 4) | Photo source editor › Exclude | none | M |
| 27 | Exclude people (built in 4) | Photo source editor › Exclude | none | M |
| 28 | Exclude tags (built in 4) | Photo source editor › Exclude | none | S |
| 29 | Archived photos never included (built in 4 as one fixed line with #30, not a switch) | Photo source editor › Exclude | always | M |
| 30 | Hidden and locked photos never included (the same fixed line as #29) | Photo source editor › Exclude | always | M |
| 31 | Exclude screenshots (built in 4: a file named "screenshot", or a PNG with no camera model) | Photo source editor › Exclude | on | M |
| 32 | Include partners' photos (built in 4) | Photo source editor › Include | off | N |
| 33 | Skip blurry/duplicates (deferred in 4: needs Immich's machine learning) | Photo source editor › Exclude | off | N |

### Scene

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 34 | Name | Scene editor › header | "New Scene" | M |
| 35 | Kind (Slideshow / Hand-placed / Dark) | Scene editor › header | Slideshow | M |
| 36 | Photo sources (several; built in 4: the Scene plays the photos of all of them) | Scene editor › What | one | M |
| 37 | Frames / Groups targeted | Scene editor › Where | every current Frame, selected explicitly | M |
| 38 | Hand-placed photo per Frame | Scene editor › What (Hand-placed) | none | S |
| 39 | Seconds per photo | Scene editor › Timing | 30 s | M |
| 40 | Transition type (crossfade / cut / slide) | Scene editor › Timing | crossfade | M |
| 41 | Transition length | Scene editor › Timing | 1.5 s | M |
| 42 | Stagger between Frames | Scene editor › Timing | on, spread over the duration | S |
| 43 | Order (built in 5a: Shuffle, Oldest first, Newest first, On this day; Oldest first continues after the Scene's last shown photo) | Scene editor › Order | Shuffle | M |
| 44 | Don't repeat within N days (built in 5a: 0 to 365, 0 reads "Off"; a photo shown recently ranks lower, and a Frame never goes blank because of it) | Scene editor › Order | 7 days | S |
| 45 | Favourite weighting | Scene editor › Order | medium | S |
| 46 | Keep events together | Scene editor › Order | off | N |
| 47 | Removed in 5a (was Same photo on all Frames; owner, 2026-10-11: "Never show the same photo on two frames at the same time") | n/a | n/a | n/a |
| 48 | Crop limit (built in 5a, replacing the fit modes: the most of a photo's area a Frame may cut away to fill itself, whatever its content; faces and the interesting part are always kept, [Planner](module-planner.md#the-photo-fit-rule)) | Scene editor › Photo fit | 35 % (5 % to 60 %) | M |
| 49 | Removed in 5a (was Mat colour; owner, 2026-10-11: "there is no “blur” or “color mat” because a photo either fills the frame or it doesn’t show") | n/a | n/a | n/a |
| 50 | Removed in 5a (was Mat padding, for the same reason as #49) | n/a | n/a | n/a |
| 51 | Removed in 5a (was Portraits on landscape Frames, show, pair two or hide; owner, 2026-10-11: "Correct. One photo per frame.") | n/a | n/a | n/a |
| 52 | Removed in 5a (was Face-aware crop as a switch; smart crop places every photo, owner 2026-10-11: "Smart crop sounds reasonable") | n/a | n/a | n/a |
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
| 70 | Displays during this Scene (on / off / leave as scheduled; not now, owner 2026-10-12: "Not now") | Scene editor › Ending | leave as scheduled | S |
| 71 | Never show list | Scenes › Never show list | empty | M |

### Schedule and Show now

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 72 | Scene to play (built in 2b: a Schedule is one repeating rule) | Schedule › editor | n/a | M |
| 73 | Days of week (built in 2b; at least one) | Schedule › editor | every day | M |
| 74 | Start/end clock time (built in 2b, on the house clock) | Schedule › editor | all day | M |
| 75 | Sunrise/sunset ± offset (built in 2b; needs the house location) | Schedule › editor | off | S |
| 76 | Crosses midnight (built in 2b: shown, not asked) | Schedule › editor | automatic: an end at or before the start is the next day | M |
| 77 | Date range / yearly event | Schedule › editor | none | S |
| 78 | Priority (Normal / Special event / Always wins; built in 2b, the Default Scene below all three) | Schedule › editor | Normal | M |
| 79 | At end: finish gracefully / stop now | Schedule › editor | finish gracefully | N |
| 80 | Power schedule: displays on/off times (its own lane, not a Scene; built in 2a as Night off) | Schedule › Power lane › Night off | on 07:00, off 23:00 | M |
| 81 | Power schedule days and sunrise/sunset times (built in 2a; sun times need the house location) | Schedule › Power lane › Night off | every day; clock times | M |
| 82 | Which Frames follow the power schedule (built in 2a: chosen one by one, or the "All Frames" group) | Schedule › Power lane › Night off | every current Frame, selected explicitly | S |
| 83 | Night off turns displays off (vs black screen only; built in 2a: "Turn displays off" or "Show black") | Schedule › Power lane › Night off | power off where the display supports it, black otherwise | M |
| 84 | Show now default length | Show now sheet | 1 h | S |
| 85 | Show now default Frames | Show now sheet | every current Frame, selected explicitly | S |
| 86 | Pause default length | Show now sheet › Pause | 1 h | S |
| 87 | Home Assistant may turn displays on during Night off | Schedule › Power lane › Rules | No | S |
| 88 | Home Assistant may turn displays off | Schedule › Power lane › Rules | Yes, any time | S |
| 89 | Scenes may turn displays on during Night off (not now: goes with Scene power requests, #70; owner, 2026-10-12: "Not now") | n/a | n/a | S |
| 90 | A hold (console All off/on, Home Assistant power switch) lasts until (next scheduled change / set time / until resumed; built in 2a without "set time") | Schedule › week page, beside All off / All on | next scheduled change | M |
| 91 | Holds shown with who set them ("Off — by Home Assistant (Away mode)") and Resume schedule (built in 2a; the console is the one source until 3) | Schedule › Power lane | n/a | M |

### Wall and Frame

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 92 | Wall name + room label (built in 2b, the one wall) | Frames › Wall tab | "Wall 1", no room | S |
| 93 | Wall photo backdrop | Frames › Wall tab | none | N |
| 94 | Frame name (built in 2b; unique, a taken name refused) | Frame page › header | "Frame N" | M |
| 95 | Placement on the wall (where the Frame hangs) | Frames › wall layout | from Display size | M |
| 96 | Orientation | Frame page › Hardware (the Frame display profile's aspect alone: landscape when at least as wide as tall; the angle never changes it, owner 2026-10-11) | from the profile | M |
| 97 | Corner positions (the opening) | Frame page › Position | full output | M |
| 98 | Frame fit and rotation (Cover: any angle, with 0/90/180/270 buttons and a fine control, the picture keeping the profile's aspect; Corners: 0/90/180/270) | Frame page › Position | Cover, 0 | M |
| 99 | Removed in 1c (was overscan/crop per edge; owner, 2026-10-10: "Yes. The corners feel like enough"): the four corners set every visible edge | n/a | n/a | n/a |
| 100 | Nudge step | Frame page › Position | 10 px | N |
| 101 | Test pattern: on Position, the Frame's grid while editing; on Picture (built in display tuning, [0024](decisions/0024-display-tuning.md)), Off, White, Grey ramp or Black level, and since calibration targets ([0026](decisions/0026-calibration-targets.md)) the screen target pre-software or post-software with a link to print the paper target, on every Frame of the display while the session is live, never stored | Frame page › Position / Picture | Position: on while editing; Picture: Off | M |
| 102 | Removed in 5a (was Fit override per Frame; fit is the ratio and the key content, not a per-Frame mode: owner, 2026-10-11: "It’s about the ratio, not strict labels") | n/a | n/a | n/a |
| 103 | Minimum photo quality for this Frame (built in 5a as the strict floor, shown as the "too small for this Frame" count) | Frame page › Overview › Photos | strict (requirement); lenient needs a requirement change | S |
| 104 | Group name + members (manual or rule) | Frames › Groups | rooms automatic | S |
| 105 | Removed in 5a (was Spanning one picture across adjacent Frames; asked whether to delete it, the owner, 2026-10-11: "Yes") | n/a | n/a | n/a |
| 106 | Live view: what the Pi is presenting now | Frame page › header (and Home tiles) | on | M |
| 107 | Bulk actions on selected Frames (skip, restart, power test) | Frames › list | n/a | S |
| 108 | Display size (diagonal) (Frame display profile) | Frame page › Hardware | from the display's report | M |
| 109 | Resolution (detected + override) (Frame display profile) | Frame page › Hardware | detected | S |
| 151 | Mat depth: how far the mat's inner edge stands in front of the picture, 0 to 20 mm in 0.5 (in millimetres, not the house's units: a mat is a few millimetres deep) ([0023](decisions/0023-print-look.md)) | Frame page › Position › Mat | 0.0 mm (no shadow) | S |
| 152 | Mat lights, up to four: comes from (0–359°, clockwise from the wall's up), steepness (5–90°), softness (0–45°), strength (0.0–10.0), colour (2000–10000 K) | Frame page › Position › Mat | none (no shadow); a new light: from above, 45° steep, 10° soft, strength 1.0, 6500 K | S |

### Display (moves with the panel)

| # | Setting | Lives on | Default | P |
|---|---|---|---|---|
| 110 | HDMI mode / refresh rate (1b shows the modes the display lists, read-only; nothing applies a mode yet) | Frame page › Hardware | best detected | S |
| 111 | Brightness: the display's own (a Display (hardware) setting when the display offers it) and Photo Wall's (0–150 %, 100 % changes nothing), each on its own | Frame page (stored on the display) › Picture | display: as the display has it; Photo Wall: 100 % | M |
| 112 | Contrast: the display's own (when offered) and Photo Wall's (0–150 %), each on its own | Frame page (stored on the display) › Picture | display: as the display has it; Photo Wall: 100 % | M |
| 113 | Colour temperature (Photo Wall (software); a display's own colour temperature request or preset is a #145 setting) | Frame page (stored on the display) › Picture | 6500 K | S |
| 114 | Gamma (Photo Wall (software)) | Frame page (stored on the display) › Picture | 2.2 | S |
| 145 | The display's other picture settings: each on the picture list it answers (colour preset, red, green and blue gain, black levels, backlight, hue, saturation, six-axis colour, display mode, sharpness), a level or a list of its named choices, read-only when it names none ([0022](decisions/0022-picture-hardware-and-software-apart.md)) | Frame page (stored on the display) › Picture › Display (hardware) | as the display has it | S |
| 146 | Saturation (Photo Wall (software), print look; [0023](decisions/0023-print-look.md)) | Frame page (stored on the display) › Picture › Photo Wall (software) | 100 % (no change) | S |
| 147 | Soft highlights (highlight rolloff), 0–100 % | Frame page (stored on the display) › Picture › Photo Wall (software) | 0 % (no change) | S |
| 148 | Black floor, 0.0–10.0 % of white light | Frame page (stored on the display) › Picture › Photo Wall (software) | 0.0 % (no change) | S |
| 149 | LUT: None or one on the LUTs list (#153) | Frame page (stored on the display) › Picture › Photo Wall (software) | None | S |
| 150 | LUT amount, 0–100 %, shown once a LUT is chosen | Frame page (stored on the display) › Picture › Photo Wall (software) | 100 % | S |
| 154 | Signal range: Auto, Full or Limited, the numbers the Pi's HDMI signal uses for black and white; belongs to the display, acts on the Pi (a change restarts that display's picture, about 10 s), never copied ([0024](decisions/0024-display-tuning.md)) | Frame page (stored on the display) › Picture | Auto | S |
| 115 | Auto-dim with room light (from a Home Assistant sensor) | Home Assistant blueprint; native later | off | N |
| 116 | Power method: HDMI-CEC / DDC/CI / HDMI signal off (display may sleep) / external switch via Home Assistant, with Test off/on (best detected tries HDMI-CEC, then DDC/CI, then signal off; the external switch waits for [3](roadmap.md#3-home-assistant)) | Frame page › Power (stored on the display) | best detected | M |
| 117 | Switch the display to this input on power-on (since 1b: by HDMI-CEC Active Source, not yet seen on a CEC display; DDC/CI switches no input) | Frame page › Power (stored on the display) | on | S |
| 118 | Never power off a display that is showing another input (stored and sent since 1b, but neither HDMI-CEC nor DDC/CI tells the Pi which input is its own, so the off goes ahead) | Frame page › Power (stored on the display) | on | S |
| 119 | Copy picture settings to other Frames (every Photo Wall value, the print look's included; each display value set in Photo Wall where the target offers it the same way, a level at the same share of its range, the rest named as skipped; never position or the signal range) | Frame page › Picture | n/a | S |
| 120 | Display moved or changed: picture and power follow it; the Frame is not ready until Position is re-checked, after the card asks to confirm or correct the Frame profile ([§6](#6-display-identity)) | Frame page › Hardware (prompt) | prompted | M |

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
| 153 | LUTs: the paper profiles Central keeps, each a LUT the Picture tab offers; add one (an ICC file from the paper's maker), remove one no Display uses | Settings › LUTs (`#/luts`) | none | S |

## 12. Where a power user's expectation meets a written requirement

Recommendations only; none changes a requirement. Rows marked **owner** need his decision because the recommendation would change a requirement or bend one.

| Requirement | A power user expects | Recommendation |
|---|---|---|
| Authored edits default to the next Run ([live media](requirements.md#live-media-compatibility-and-preparation)) | Change seconds-per-photo and see it now | Keep the rule; add **Save and apply now** |
| "Once an assignment is scheduled and secured by its responsible Players, its content is locked for that execution" ([live media](requirements.md#live-media-compatibility-and-preparation)) | Skip, Previous and Never show act at once | They act at once on assignments still drawn from a live query (not yet secured). On a secured assignment, Never show takes effect from the next Run, and Skip is not offered. |
| U1: a failure never leaves a display dark ([failure visibility](requirements.md#failure-visibility-and-recovery)) | Night off, Pause and All off leave displays dark, and that is not a failure | **Resolved:** U1 amended 2026-10-12 (owner: "Amend U1", [system gate](https://claude.ai/artifact/HHNRy2Hs1CpEm5VVK58DWm)): intended darkness, authored or operational, is allowed and reads Resting; a failure during it shows in the console at once and on the display when it is next due on. |
| "A changed profile requires calibration revalidation before the new equipment is considered ready" ([installation model](requirements.md#installation-model)) | A new display just works, with a prompt to re-check | The requirement wins: the display-changed flow blocks the Frame's readiness until Position is re-checked; it is not just a prompt ([§6](#6-display-identity)) |
| "Every target intentionally affected by a Scene or its children must participate explicitly" ([composition](requirements.md#composition-and-spatial-authoring)) | "All Frames" as the default for the power schedule, Scenes and Show now | Editors pre-select every current Frame explicitly; a Frame added later joins only if the user chose the explicit "All Frames" group |
| "Operational state … remains distinct from authored content state" ([operations](requirements.md#operations-and-scope)) | A "Good night" Scene that turns the displays off | **Not now** (owner, 2026-10-12: "Not now"): Scenes do not carry display power; Night off, holds and a dark Scene cover Good night ([§7](#7-power)) |
| A photo goes only on a Frame it fills with its key content kept; cropping that would cut a face or the interesting part is no exception ([compatibility](requirements.md#live-media-compatibility-and-preparation)) | A fill option, mats and portrait pairing | Built in 5a as the requirement says: every photo fills its Frame by smart crop, a photo that would lose its key content skips that Frame, one photo per Frame (owner, 2026-10-11: "Correct. One photo per frame."); the Frame page counts what was skipped and why |
| A 720p video never on a 40-inch Frame ([compatibility](requirements.md#live-media-compatibility-and-preparation)) | An adjustable floor and a skipped count | Keep it strict and always show the skipped count; a lenient floor would be a requirement change |
| The Installation owns policy and timezone ([installation model](requirements.md#installation-model)) | Location for sun times, units | Add location (optional) as house policy (built in 2b: Settings › House) |
| The operator selects the Player release ([provisioning](requirements.md#player-provisioning)) | Automatic updates | Keep the existing release choices ([console DDD Part E](operator-console-ddd.md#part-e-v2-only-console-and-node-release-workflows-feature-layer)) |
| U2: photos within a couple of hours after a power cut | Minutes | Keep U2 as the bound; show per-Frame progress |
| No playback guarantee after a cold reboot without Central ([stateless Players](requirements.md#central-authority-and-stateless-players)) | The wall survives a server reboot | Accept; the display says "Can't reach the Photo Wall server" |
| U1: at minimum an error page | Last photo plus a small badge | Last photo plus a badge for photo problems; the error page for app or system faults |
| Not a second general-purpose photo library ([purpose](requirements.md#purpose-and-experience)) | Never show this photo again | A wall-only Never show list; nothing written back to Immich |
| Nothing on backup, phone use, captions or a live view (ordering and mats were answered for 5a: shuffle by default, no mats) | All of them | [Operator experience](requirements.md#operator-experience) records what the owner stated; the rest stays design until he states it |

## 13. What this design gives up

- Picture and power settings move with the display. That needs each display to report a usable serial over HDMI; one that reports none keeps its settings only on the Frame it feeds and starts over on another, and two identical ones swapped between Frames go unnoticed ([§6](#6-display-identity)).
- Rooms are labels that make automatic Groups, not separate objects; Home Assistant areas cover the rest.
- Sensors and triggers arrive through Home Assistant rather than being built into Photo Wall; native auto-dim waits.
- Hardware brightness and power depend on each display: many TVs take power commands but not brightness, and some panels take neither. The Display (hardware) section shows only what a display answers from the picture list; its other settings stay on its own buttons, and setting names are ddcutil's, in English.
- Photo Wall brightness above 100 % eases the brightest parts into white only with Soft highlights above 0 % ([0023](decisions/0023-print-look.md)); at 0 % they clip flat, as before. The print look is per display, so two Frames on one display share it, and a mat's lights only shade: they never warm or cool the photo itself. Display settings that move each other (a colour preset resetting the gains) are reported, never fought, and reverting a preset does not restore the gains it moved.
- Cover never skews the picture, so a display hung at an angle shows its picture turned rather than stretched onto its corners (Corners stays for that). The picture is cut to the four corners, so corners set inside the visible area leave a black edge. With Trim edges gone, the corners are the only way to trim a Frame.
- One Frame per display today: Central binds one Frame to each HDMI port. The renderer keeps each Frame inside its own corners and the rest of the display black, but its final pass masks with that one Frame's corners, so two Frames on one display also needs the renderer to mask with every Frame's corners and draw each Frame's picture (proven so far only on the reference drawing; [architecture › Player and rendering](architecture.md#player-and-rendering)).
- What is deferred, and what that costs, is the [roadmap's deferred list](roadmap.md#deferred-and-what-that-costs).

## 14. What it supersedes

- The [console UX design](operator-console-ux-design.md) of 2026-09-13 and its later notes, wherever they differ: Home first; the Everyday / Set up rail; one Frame page with the tabs of §5 in place of the Frame Inspector's facets; the console says Display again (§2); the commissioning layer and its open display-power precedence (its §7.5) are answered by §6 and §7; its §10 Q2 (recurrence deferred) and Q3 (no sensors, triggers or actuator registry) give way to repeating Schedules and Home Assistant inputs.
- [Decision 0018](decisions/0018-console-by-domain-and-design-system.md), as listed in [decision 0019](decisions/0019-first-principles-console.md#what-it-supersedes).
