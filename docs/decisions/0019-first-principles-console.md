# 0019 — The first-principles console: one Frame page, Display identity, and power as its own concept

**Date:** 2026-10-09 · **Layer:** system (the console's information architecture, the Display, display power and the Home Assistant boundary) · **Status:** Journey adopted as the roadmap by the owner, 2026-10-09; decisions 1–8 proposed, pending this PR. Nothing is built; the [roadmap](../roadmap.md#delivery-order) owns the delivery order.

**Review page:** [Console Setup Review](https://claude.ai/artifact/EwjdvEcM82HyLDb4BrDSdC). **Design:** [operator console design](../operator-console-design.md). **Requirements:** [Operator experience](../requirements.md#operator-experience). **Register:** D12 and D13 in [design decisions](../design-decisions.md).

## The problem

The owner could not find the knobs a person reaches for first: where a Frame's picture sits on its display, how the display turns on and off, and its brightness and contrast. Position was under Wall › Frame › Calibration › Start live calibration, behind a 30-second session; there was no power control anywhere; brightness was a software multiplier labelled "draft", with no contrast. Under [0018](0018-console-by-domain-and-design-system.md) a Frame had two pages (setup and live) and a Pi had two (Hardware and Software), so one question could take three pages. Most settings a power user expects are missing ([settings status](../roadmap.md#settings-status)).

## The owner's words (chat, 2026-10-09)

- Unprompted: "Central needs another UI pass to make sure that all of the features are being exposed on the UI. I cant find some of the simple config knobs, like how do i set the visible frame position on a display? how do i control the CEC power? how do i change the brightness and contrast?" / "i continue to be frustrated with how poorly designed the UI is."
- Unprompted: "dont stop at my first 3 questions. i want you to do a comprehensive analysis and take a "first principles" approach to thinking through this like a user would. ignore what the current code does, and just walk through the steps a power user would expect to take."
- Asked how Night off should work: "I like the idea of a native power schedule, yeah. But I also want home automation inputs, so also consider how this system might be exposed to home assistant."
- Asked whether a display's hardware settings should follow it when it moves: "Not all displays are TVs; some are just LCD or OLED panels. But to your question: yes, the display hardware settings could remain consistent to where the panel moves."
- Asked whether brightness should drive the display hardware: "As much as possible. The test pi is not connected to a TV — it's a portable monitor"
- Asked whether power is its own concept or part of Scenes: "I meant power as a unique concept, but it could go either way. Or both."
- Asked who wins between the power schedule, Home Assistant and a person: "I don't know. I'm hoping for configuration options built in sensible defaults"
- On the review page: "I LOVE the Journey section of that doc. I want those ideas codified as the roadmap."

## Decisions (proposed)

These are design choices made to meet those words; only the quotes above are the owner's.

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| 1 | **One Frame page** with tabs Overview, Position, Picture, Power, Photo fit, Hardware, reached by clicking the Frame anywhere | 0018's two pages per Frame (setup on Wall, live on Screens) | All three of the owner's questions land on one page; the signage "screen settings" page every comparable product has |
| 2 | **Navigation by lifecycle:** Everyday (Home, Schedule, Scenes) and Set up (Frames, Pis, Settings), plus a Setup checklist until finished; the console opens on **Home** | 0018's domain sidebar (Show running, Show creating, Wall, Fleet, Needs attention) opening on Screens | Keeps one-time bring-up apart from daily use with six top-level items; Home answers "is it OK and what is on it" |
| 3 | **Display with identity**, the UI label for the domain's Panel (a design choice, not the owner's wording): recognised by the maker, model and serial in its EDID; brightness, contrast, colour, gamma and power options are stored on it and follow it; position, crop, rotation and the Frame display profile stay on the Frame. A changed display blocks the Frame's readiness until Position is re-checked | Settings on the Frame (they would not follow the panel); shared picture presets per model (dropped) | The owner's answer on a moved display; the requirements' revalidation rule |
| 4 | **Hardware control first:** power through HDMI-CEC, DDC/CI, HDMI signal off or a smart plug through Home Assistant; brightness and contrast through DDC/CI, Photo Wall picture adjustment otherwise; every slider says where it acts | Software only; hardware only (hiding sliders) | "As much as possible"; software gain is never called measured panel brightness |
| 5 | **Power is its own concept** with a Power lane on the Schedule page, the one place that decides and explains display power. Scenes asking for displays on or off is offered under "Or both" as an exception that needs the owner's confirmation, because it puts operational state in authored content | A dark Scene targeting a display-power Actuator (the review's first recommendation); a house-level window outside the Schedule | "a unique concept … Or both"; display power stays operational state |
| 6 | **Precedence:** the newest power request wins until it ends, then the power schedule resumes; [the hold rule](../operator-console-design.md#the-hold-rule) says how long each request lasts; guard settings with defaults | A fixed priority order; per-source priority numbers | "configuration options built in sensible defaults" |
| 7 | **Home Assistant through MQTT discovery** from Central's media worker; a House device and one device per Frame; a request topic and shipped Blueprints; Pis never touch MQTT and the internal bus is never bridged | A custom Home Assistant integration (possible later on the same interface); Home Assistant's WebSocket API | Requirements already name Home Assistant/MQTT; the pattern Zigbee2MQTT and Frigate use; devices appear with no code in Home Assistant |
| 8 | **Six status words** (Showing, Resting, Getting ready, Needs a look, Not showing, Can't tell) drawn on 0018's severity scale, and one error template; internal evidence words move under Details | Today's repeated, internal-word health labels | One vocabulary everywhere; never claims visible pixels |

The Journey becoming the roadmap is the owner's own ask, not a proposed decision. Kept from earlier choices: the release choices of [console DDD Part E](../operator-console-ddd.md#part-e-v2-only-console-and-node-release-workflows-feature-layer); binding only from the Frame (0018); the design-system layers, tokens, rules S1 to S3, the severity scale and the look (0018).

## Costs

- A display that reports no usable identity loses its picture and power settings when moved.
- Many TVs accept power commands but not brightness; some panels accept neither, leaving picture adjustment and a smart plug.
- The Pi image must gain CEC and DDC tooling; the test Pi has CEC devices but no I2C bus devices and neither tool.
- Home Assistant needs an MQTT broker; there are no custom Home Assistant actions; Photo Wall cannot switch a Home Assistant-only plug by itself.
- Rooms are labels, not objects. Sensors arrive only through Home Assistant until native ones are built.
- Every console page moves again; the 0018 domain pages built so far are re-homed under the new navigation.
- Treating Night off, Pause and All off as intended darkness needs a U1 amendment the owner has not made ([design §12](../operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)).
- What is deferred, and its cost: the [roadmap's deferred list](../roadmap.md#deferred-and-what-that-costs).

## What it supersedes

- [0018](0018-console-by-domain-and-design-system.md):
  - the domain sidebar and the Screens landing (decision 2);
  - "a Frame has two pages (setup, live) and a Pi has two (Hardware, Software)" (decision 1);
  - **H3**, "every write that changes a Panel is Wall setup": writes that change a display live on the Frame page's Position, Picture and Power tabs, which Home tiles open too;
  - the split of Frame health into live health (Screens) and setup state (Wall): the two facts stay distinct, but both show on the one Frame page; live health is spoken in the six status words and setup state uses the scale's `todo` level.
  - The severity scale (`ok`, `todo`, `notice`, `alarm`, `unknown`) is kept. The six words map onto it: Showing and Resting → `ok`, Getting ready and Needs a look → `notice`, Not showing → `alarm`, Can't tell → `unknown`. H1, H2, the layers, rules S1 to S3 and the look stand.
- [Console UX design](../operator-console-ux-design.md): the whole document becomes the historical gate record, superseded by the [operator console design](../operator-console-design.md#14-what-it-supersedes) wherever they differ, including its open display-power precedence (§7.5) and §10 Q2 and Q3.
- [Requirements](../requirements.md#reference-experiences): Good night's "persistent dark-state and wake-up policy remain open" now points to the owner's power-schedule answer.
- [Console DDD](../operator-console-ddd.md): §8's roadmap of passes is replaced, for what comes next, by the [roadmap](../roadmap.md).
