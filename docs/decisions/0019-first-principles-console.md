# 0019 — The first-principles console: one Frame page, Display identity, and power as its own concept

**Date:** 2026-10-09 · **Layer:** system (the console's information architecture, the Display, display power and the Home Assistant boundary) · **Status:** accepted 2026-10-09. The owner answered every open question on the review page (quoted below) and asked that its Journey become the roadmap. Nothing is built; the [roadmap](../roadmap.md#delivery-order) orders the work, starting with delivery 1a.

**Review page:** [Console Setup Review](https://claude.ai/artifact/EwjdvEcM82HyLDb4BrDSdC). **Design:** [§0 of the console UX design](../operator-console-ux-design.md#0-the-first-principles-console-2026-10-09). **Requirements:** [Operator experience](../requirements.md#operator-experience). **Register:** D12 and D13 in [design decisions](../design-decisions.md).

## The problem

The owner could not find the knobs a person reaches for first: where a Frame's picture sits on its display, how the display turns on and off, and its brightness and contrast. Position was under Wall › Frame › Calibration › Start live calibration, behind a 30-second session; there was no power control anywhere; brightness was a software multiplier labelled "draft", with no contrast. Under [0018](0018-console-by-domain-and-design-system.md) a Frame had two pages (setup and live) and a Pi had two (Hardware and Software), so one question could take three pages. Of 144 settings a power user expects, 29 exist in the console, 8 are built with no controls and 107 are missing.

## The owner's words (chat, 2026-10-09)

- "Central needs another UI pass to make sure that all of the features are being exposed on the UI. I cant find some of the simple config knobs, like how do i set the visible frame position on a display? how do i control the CEC power? how do i change the brightness and contrast?" / "i continue to be frustrated with how poorly designed the UI is."
- "dont stop at my first 3 questions. i want you to do a comprehensive analysis and take a "first principles" approach to thinking through this like a user would. ignore what the current code does, and just walk through the steps a power user would expect to take."
- On Night off: "I like the idea of a native power schedule, yeah. But I also want home automation inputs, so also consider how this system might be exposed to home assistant."
- On a swapped display: "Not all displays are TVs; some are just LCD or OLED panels. But to your question: yes, the display hardware settings could remain consistent to where the panel moves."
- On brightness: "As much as possible. The test pi is not connected to a TV — it's a portable monitor"
- On power: "I meant power as a unique concept, but it could go either way. Or both."
- On who wins: "I don't know. I'm hoping for configuration options built in sensible defaults"
- On the roadmap: "I LOVE the Journey section of that doc. I want those ideas codified as the roadmap."

## Decisions

The choices below are design choices made to meet those words; only the quotes above are the owner's.

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| 1 | **One Frame page** with tabs Overview, Position, Picture, Power, Photo fit, Hardware, reached by clicking the Frame anywhere | 0018's two pages per Frame (setup on Wall, live on Screens) | All three of the owner's questions land on one page; the signage "screen settings" page every comparable product has |
| 2 | **Navigation by lifecycle:** Everyday (Home, Schedule, Scenes) and Set up (Frames, Pis, Settings), plus a Setup checklist until finished; the console opens on **Home** | 0018's domain sidebar (Show running, Show creating, Wall, Fleet, Needs attention) opening on Screens | Keeps one-time bring-up apart from daily use with six top-level items; Home answers "is it OK and what is on it" |
| 3 | **Display with identity:** recognised by the maker, model and serial in its EDID; brightness, contrast, colour, gamma and power options are stored on it and follow it; position, crop and rotation stay on the Frame. A display with no usable identity is new each time | Settings on the Frame (they would not follow the panel); shared picture presets per model (dropped) | The owner's answer on a swapped display |
| 4 | **Hardware control first:** power through HDMI-CEC, DDC/CI, HDMI signal off or a smart plug through Home Assistant; brightness and contrast through DDC/CI, Photo Wall picture adjustment otherwise; every slider says where it acts | Software only; hardware only (hiding sliders) | "As much as possible"; software gain is never called measured panel brightness |
| 5 | **Power is its own concept** with a Power lane on the Schedule page, the one place that decides and explains display power; Scenes may also request on, off or leave as scheduled | A dark Scene targeting a display-power Actuator (the review's first recommendation); a house-level window outside the Schedule | "a unique concept … Or both"; display power stays operational state |
| 6 | **Precedence:** the newest power request wins until it ends, then the power schedule resumes; guard settings with defaults (Home Assistant may wake displays during Night off: No; may turn them off: Yes; Scenes may wake them during Night off: No; a manual hold lasts until the next scheduled change) | A fixed priority order; per-source priority numbers | "configuration options built in sensible defaults" |
| 7 | **Home Assistant through MQTT discovery** from Central's media worker; a House device and one device per Frame; a request topic and shipped Blueprints; Pis never touch MQTT and the internal bus is never bridged | A custom Home Assistant integration (possible later on the same interface); Home Assistant's WebSocket API | Requirements already name Home Assistant/MQTT; the pattern Zigbee2MQTT and Frigate use; devices appear with no code in Home Assistant |
| 8 | **Six status words** (Showing, Resting, Getting ready, Needs a look, Not showing, Can't tell) and one error template; internal evidence words move under Details | Today's repeated, internal-word health labels | One vocabulary everywhere; never claims visible pixels |
| 9 | **The Journey is the roadmap**, delivered in the order 1a, 1b, 1c, 2, 3, 3b, 4 to 7 | | The owner's roadmap ask |

Kept from earlier choices: no automatic updates (releases pre-download and the operator clicks Apply), a release selected automatically at first run, per-Pi pins deferred (owner, 2026-10-04); binding only from the Frame (0018); the design-system layers, tokens, rules S1 to S3 and the look (0018).

## Costs

- A display that reports no usable identity loses its picture and power settings when moved.
- Many TVs accept power commands but not brightness; some panels accept neither, leaving picture adjustment and a smart plug.
- The Pi image must gain CEC and DDC tooling; the test Pi has CEC devices but no I2C bus devices and neither tool.
- Home Assistant needs an MQTT broker; there are no custom Home Assistant actions; Photo Wall cannot switch a Home Assistant-only plug by itself.
- Rooms are labels, not objects. Sensors arrive only through Home Assistant until native ones are built.
- Every console page moves again; the 0018 domain pages built so far are re-homed under the new navigation.
- Deferred: roles, notification channels beyond the basics, change history, lights and other non-display controls, spanning, per-Pi pins ([roadmap](../roadmap.md#deferred-and-what-that-costs)).

## What it supersedes

- [0018](0018-console-by-domain-and-design-system.md): the domain sidebar, the Screens landing, and "a Frame has two pages (setup, live) and a Pi has two (Hardware, Software)". H1's one owning home per fact stays, with the Frame page as the home of everything about one spot on the wall. The design system is unchanged.
- [Console UX design](../operator-console-ux-design.md): the 2026-10-02 Panel rename, §7's commissioning layer and its open display-power precedence (§7.5), §8a's landing, and §10 Q2 and Q3; each is marked in place ([§0.14](../operator-console-ux-design.md#014-what-this-section-supersedes-in-this-document)).
- [Requirements](../requirements.md#reference-experiences): Good night's "persistent dark-state and wake-up policy remain open" is answered by the power schedule.
- [Console DDD](../operator-console-ddd.md): §8's roadmap of passes is replaced, for what comes next, by the [roadmap](../roadmap.md).
