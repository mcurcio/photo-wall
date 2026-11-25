# 0036 — Home: a Frame's status, the house word and the wall's everyday actions

**Date:** 2026-11-22 · **Layer:** system (Central's read of a Frame's live health; four new everyday commands; the console's landing page and phone layout) · **Status:** accepted design (owner, 2026-11-22: "I'll take your recommendations. Proceed"). Slice 2 is built, in [#158](https://github.com/mcurcio/photo-wall/pull/158): where the build differs from this record is [As built](#as-built-slice-2), and what it proved is [Verified, and not](#verified-and-not). Of slice 3, Central's side of Skip, Previous and Never show is built ([As built (slice 3)](#as-built-slice-3-skip-previous-and-never-show)) and the rest is not; the owner ruled on it on 2026-11-24 and 2026-11-25 ([his rulings](#the-owners-rulings-for-slice-3-2026-11-24)), which change Skip, Previous, Never show, Pause and Show now as first written here, and its accepted design is [Slice 3 design](#slice-3-design). It is slices 2 and 3 of [roadmap 3b-i](../roadmap.md#3b-i-home-and-phone); slice 1 is [0035](0035-operator-sign-in-through-an-identity-provider.md). **Builds on:** the six status words ([design language §6](../design-language.md#6-status-and-severity)), the health verdict (issue #126), the power timetable ([0021](0021-power-timetable-and-repeating-schedules.md)), the wall plan ([Planner](../module-planner.md#the-wall-plan)) and [0032](0032-the-player-continues-centrals-plan.md).

**Decision owner:** the owner. His words below are his; everything not quoted from him is a design choice, not his ruling.

## The owner's words

| # | Words | Source | Kind |
|---|---|---|---|
| Q4 | Pause: "Freeze the photo": the Frame holds the photo it shows for the Pause length, then carries on by itself | owner, 2026-11-22 (question card) | a pick, revisable |
| R1 | "I'll take your recommendations. Proceed": every default of this record's open questions and findings stands as the owner's pick | owner, 2026-11-22 (chat) | a pick, revisable |
| — | "Proceed" (to this design, after the recommendation of 3b-i as the next delivery) | owner, 2026-11-22 (chat) | guidance |
| — | "No auto-restarts at all" | owner, issue #125 | guidance, bearing on [Restart app](#restart-app) |
| — | "Amend U1" (intended darkness reads Resting) | owner, 2026-10-12 | a ruling, bearing on [the decision table](#the-decision-table) |
| S1–S9 | Skip moves the wall, Previous too, an immediate Back to normal, the same photo on two Frames allowed for an action, Never show's last showing, Pause over Show now, Show now's lamps, no Pause on a video | owner, 2026-11-24 and 2026-11-25 (chat) | rulings, recorded in [The owner's rulings for slice 3](#the-owners-rulings-for-slice-3-2026-11-24); where they differ from the sections below, they govern |

## The problem

- **A Frame's health is worked out in the browser.** `central/console/src/health.js` `frameHealth` folds nine states in its own words; only "Resting" and "Getting ready" of the six words exist in code. Roadmap 3 wants the word as a Home Assistant entity and roadmap 7 notifies from it. Neither can read a browser.
- **Nobody knows since when.** The word's "since" is the notification's clock (15 minutes of Not showing) and the error template's second clause. Nothing records it.
- **The console opens on the Wall page**, a setup page. There is no Home, no house word and no bottom tabs.
- **Four everyday actions do not exist in Central:** Skip, Never show, Pause, and Show now on chosen Frames with an end time. Restart app does not exist on the Pi.

## Decision

1. **Central owns a Frame's status.** A new pure package, `central/frame_status/`, composes one `FrameStatus` per Frame from facts their owners already judge. It composes and never judges, as `central/central_errors/` does. The console's `frameHealth` and `wallAttention` are deleted; every chip reads the served word.
2. **One table decides the word**, first match wins, and one table maps every cause to the part that failed and the one fix. Both live in `policy.py` and nowhere else.
3. **Central records when a Frame's word changed**, one row per Frame, written only on a change.
4. **Home is one read**, `GET /v1/operator/home`, a document with a change number.
5. **Each everyday action is a command to the owner of the state it changes**, asked with the `operate` permission of 0035 and stamped with who asked.

## A Frame's status

### The shapes

```
FrameStatus
  frame_id
  word      StatusWord | None          # None: setup is not status; `setup` says which step
  setup     SetupStep | None           # needs-a-pi | display-changed | check-position
  since     Instant                    # when this word began, Central's clock
  because   Because                    # the one row of the decision table that matched
  problem   Problem | None             # present for Not showing, Can't tell, Needs a look
  progress  Progress | None            # present for Getting ready: the step and its count
  override  Override | None            # a hold, a pause or a takeover in force here
  can       frozenset[Action]          # what may be asked of this Frame now

Problem
  part      "display" | "pi" | "photo app" | "network" | "central" | "immich"
  code      ProblemCode                # closed vocabulary; the console words it
  since     Instant
  doing     DoingCode | None           # what Photo Wall is doing about it
  fix       Fix | None                 # one action the system can perform, or a link to the owner
  evidence  Evidence                   # the layer, where it came from, its time (Details)

HouseStatus
  word      StatusWord | "all-good"
  problems  tuple[Problem]             # worst first; a house problem has no Frame
```

- **Central sends codes, the console sends words.** Every string a person reads is the console's, from the generated contract, as room lights' refusals are. Central's vocabulary is closed, so an unworded code fails the console's typecheck.
- **`can` is how "not offered" works.** An action is absent from `can` where it cannot act (for Skip: a hand-placed photo, a black Frame, a Pi not answering; [Slice 3 design](#skip-and-previous-who-moves-and-who-stays)), so the console never draws a button it would have to refuse ([design §12](../operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)). (As first written, Skip was absent "on a secured assignment"; every layer on screen is secured, so that rule would never offer it, and the owner's rulings replace it.)
- **`Fix` is closed too:** `restart-app`, `reboot-pi`, `resume-schedule`, `carry-on`, `check-now` (a library connection), or `open` with a route to the page that owns the setting. A fix the system cannot perform cannot be expressed.

### The inputs

Each is read through a port in `central/frame_status/ports.py`. None is new knowledge; the owner of each stays its judge.

| Fact | Its owner today | Port answers |
|---|---|---|
| Is the Frame set up | `central/displays/model.py` `readiness` | unbound, display-changed, position-needed, ready |
| Should its display be on, and why | `central/power/timetable.py` `wanted_at`; `central/why/explain.py` `is_resting` | on or off, because Night off, a hold (by whom) or a test; whether the Pi carried it out |
| Can Central hear its Pi | `fleet/node_bus_presence.py`, `infra/player_contact_store.py` | the hub's state; linked or not and since; last heard |
| What the Pi says about its screen | the health verdict, `fleet/frame_dark.py` `frame_dark` | a dark code and since, or nothing |
| Is its picture on screen | the Player line's `FrameReport` (`showing`, `generation`, `continuing_since`) | showing for Central's generation, or not |
| Does Central black it | `central_errors/compose.py` `impediment` | impeded, clear, or not reported |
| Is something on the way | `infra/player_versions.py` (an install), `fleet/pi_reboot.py` (a reboot), preparation | the step and its count |
| Did planning place a photo | `planning_outcome.py` `issues_of`; the Plan's `unfilled` spans | this Frame's issues and spans |
| Are its photo sources healthy | `photo_library/model.py` `LibraryHealth`, for the connections its playing Scene reads | needs a look, with which problem |
| Its Pi's own health | `fleet/node_observations.py` | throttled, storage low, the wall clock in doubt |
| An override here | power holds; this decision's pauses; the Runtime's takeover Run | which, by whom, until when |

The `FrameReport` is read for status for the first time; today only the Position trial reads it.

### The decision table

First match wins. A row names the word, and what the console says under it.

| # | When | Word | Part |
|---|---|---|---|
| 1 | The Frame is not set up (no Pi, display changed, Position not checked) | none: `todo` with its setup step | — |
| 2 | Central's hub is unreachable or its worker stopped | **Can't tell** since then; one house problem | central |
| 3 | A reboot or an install of its Pi is in progress, or the Pi enrolled and has not reported yet | **Getting ready**, with the step | — |
| 4 | Its Pi is not linked or not heard past the silence limit | **Can't tell** since last heard | pi (never "network": Central says when it last heard, [U6](../requirements.md#failure-visibility-and-recovery)) |
| 5 | Its display should be off and the Pi carried that out, and nothing below is wrong | **Resting** (Night off, a hold by whom, a test) | — |
| 6 | Its display should be off and something in rows 8 to 10 is wrong | **Needs a look**: "Won't show at 07:00" | as that row |
| 7 | Its display should be off and the display did not answer, or shows another input | **Needs a look** | display |
| 8 | The Pi's verdict says dark, by cause (the cause table below) | **Not showing**, or **Getting ready** for a cause that ends by itself | by cause |
| 9 | Central blacks the Frame (its impediment) | **Not showing** | central |
| 10 | The Frame report says its picture is not on screen, with no verdict yet | **Not showing** | photo app |
| 11 | Showing, and: no photo could be placed (an unfilled span), a planning issue, a source's connection needs a look, its Pi's health, or it has played Central's continuation past the limit | **Needs a look** | by cause |
| 12 | Showing for Central's generation | **Showing** | — |
| 13 | Nothing above | **Can't tell** since the last report | pi |

Three points need saying, because the [design language](../design-language.md#6-status-and-severity) does not yet:

- **Rows 6 and 7, a failure during intended darkness.** The amended [U1](../requirements.md#failure-visibility-and-recovery) says "the console shows it at once". "Not showing" means "should be showing and isn't", which is false at 02:00 under Night off, and roadmap 7 must not wake the owner for it. So the word is Needs a look, with a problem card at once, and it becomes Not showing when the display is next due on. **Finding F1:** this is a new rule for design language §6.
- **Row 4 says "pi", never "network".** Central cannot tell a dead Pi from a pulled cable. The words are "Can't reach Pi pw-3f2a since 21:04".
- **A paused Frame is row 12** (see [Pause](#pause)).

**The cause table** maps every dark code of `contracts/node_faults.py` and every `FrameBlackCause` to a word, a part and a fix. A test holds it exhaustive: a new code without a row fails.

| Verdict | Word | Part | Fix |
|---|---|---|---|
| `app_absent`, `app_unresponsive`, `app_resource_exhausted`, `app_descriptor_limit` | Not showing | photo app | Restart app |
| `frame_no_renderer`, `frame_cannot_draw`; `software_renderer` (not dark: Needs a look) | Not showing | pi | Reboot the Pi |
| `screen_not_told`, `no_frame_placed`, `frame_no_plan` | Not showing | central | Open the Frame (its Pi has no instruction for this screen) |
| `frame_no_time` | Not showing | pi | Open the Pi (its clock) |
| `frame_not_started` | Getting ready | — | none |
| `frame_no_photo`, `frame_no_content`, `nothing_to_show` | Getting ready while photos are being prepared for it; else Needs a look | immich, or central | Open the Scene's Photo source |

The split of the last row by "being prepared" is the per-Frame progress [U2](../requirements.md#failure-visibility-and-recovery) asks for after a power cut.

### Since when

- The word is worked out on read; only its beginning is kept. A new table `frame_status`, one row per Frame: the word, the matched row, the problem code, `since`, audit fields. One repository, `infra/frame_status_store.py`.
- **One writer:** a new scheduler step, `status`, after `settle`, its own failure and `/healthz` facet ([`scheduler.py`](../../central/scheduler.py)). It composes every Frame and writes the rows whose word or problem changed, and nothing otherwise ([0033](0033-central-write-load.md)).
- Each change also appends one record to Central's event log (`infra/events.py`, a new `EventKind`), kept a week. The Frame page's Overview reads its "recent problems with times" from there (row G2), with no second history.
- Roadmap 7's "Not showing for 15 minutes" reads `since`. Roadmap 3 publishes the row on Central's hub.

### The house word

The worst Frame word by the design language's order; **All good** when every Frame is Showing or Resting; a Frame in setup does not count. A house problem (row 2; no library connection answers; Central's scheduler failing) is one `Problem` with no Frame, and replaces the per-Frame problems it explains, so the network going down is one banner and not twelve cards.

### Home's read

`GET /v1/operator/home` answers one document:

```
HomeView
  change     int                       # moves when anything below does
  house      HouseStatus
  walls      tuple[WallView]           # each: its Frames' placement and FrameStatus
  overrides  tuple[Override]           # holds, pauses, takeovers, each with who and until
  now_next   tuple[NowNext]            # per Wall: what plays, until when, what follows
```

- **Transport.** The console has no push channel today; every view polls ([runbook](../runbook.md)). Home reads this document on the snapshot's 5 s cadence, at once after an action, and with `If-None-Match` on `change` so an unchanged wall costs no body. Nothing in the shape assumes polling: a push stream sends the same document. That stream is [3b-ii](../roadmap.md#3b-ii-the-live-map)'s, whose "within about 2 seconds" needs it.
- **Finding F2:** the design language says every view updates by push and lists the polled exceptions. Home joins that list until 3b-ii.

## The everyday actions

Each is a route under `/v1/operator/`, asks for `operate`, carries a `request_id` so a double tap is one request, and stamps `by` with the 0035 actor.

| Action | Route | Owner of the state | Way back |
|---|---|---|---|
| Skip, Previous | `POST walls/{id}/skips` (the wall, S1), `POST frames/{id}/skips` (one Frame) | Coordination and the Planner | none (Previous) |
| Never show | `POST never-show`, `DELETE never-show/{photo}` | a new `never_show` list, read by the wall plan | Undo, **Show again** |
| Pause | `POST pauses`, `DELETE pauses/{id}` | a new pause, beside the power hold | **Carry on** |
| Show now | `POST activations` (extended); Back to normal `POST runs/{id}/end` | the Runtime | **Back to normal** |
| Restart app | `POST node/devices/{id}/app-restarts` | a job to the Pi's app broker | none |

### Skip and Previous

Amended by the owner's rulings S1, S2 and S4 (2026-11-24); the design is [Slice 3 design](#skip-and-previous-who-moves-and-who-stays).

- **Skip on Home moves the whole wall** it is pressed on: every Frame of that wall that can move goes to its next photo at the same instant; a Frame that cannot (a hand-placed photo, a deliberately black Frame or an ending, a Pi not answering or playing Central's continuation, nothing to skip) stays, and the answer says which stayed and why. Other walls are not moved. A Frame's own menu keeps a **Skip** for that Frame alone.
- **Previous** is the same, wall-wide on Home and per Frame in a Frame's menu: it shows the photo the Frame showed before, and each press steps back one more.
- **Central never changes an offered layer for it.** The layer on screen is always secured, so a skip adds a layer above it (a **cover**), from the shared instant; the covered layer stays as it was. (As first written: "refused `secured` when the Frame's current layer is secured" and "the next planning pass ends that Frame's current layer": neither can hold, [Slice 3 design](#what-the-code-fixes).)
- **The same photo on two Frames is allowed for a moment** when a skip causes it (S4); the planner still avoids it in everything it places itself.
- It is **acknowledged**, not optimistic: the tile shows "requested" until the Player reports the new photo presented.

### Never show

- A wall-only list. Nothing is written to Immich ([design §12](../operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)).
- New table `never_show`: the photo (connection and Immich asset id), when, by whom. Pure rules in `central/wall_plan/` beside shown history, behind a port; the wall assignment treats a listed photo as not eligible, and an empty pool does not relax that.
- Adding a photo that is on a Frame now also skips it there when Skip can move that Frame. Where the photo is already downloaded for another Frame (an offered layer), it shows there that one last time and never again, and the toast says so (owner, S5, 2026-11-24). The download lock stays a hard rule. (As first written: "On a secured layer it stays until that Run ends".)
- Optimistic, with Undo for 10 s. **Scenes › Never show list** shows every listed photo with **Show again**.

### Pause

The owner's pick: the Frame holds the photo it shows.

- A pause has the power hold's shape: `Pause{pause_id, frame_ids, source, started_at, ends_at}`. Default length 1 hour (setting #86). The hold and the pause share one `source` type, the 0035 actor, so "Paused by Matt until 22:04" needs no second vocabulary. `HoldSource.kind`'s `console` becomes that actor too.
- Central adds a **hold** layer of the same photo above the Frame's layers, from the pause's start to its end; it never extends the current layer (its end is its identity, [Slice 3 design](#pause-the-hold)). No new placement gives that photo to another Frame meanwhile; one already downloaded elsewhere may overlap briefly (S4), so a pause is never refused for it.
- **Not on a video** (S8): Pause is absent from `can` on a video and refused `video` if sent.
- **A paused Frame stays paused when Show now starts** (S6): Show now plays beneath it and appears at Carry on.
- The schedule beneath keeps advancing. **Carry on**, or the pause's end, reveals its current state at once, with the usual fade (S3), like any covered Run.
- Power wins: Night off during a pause turns the display off, and the pause still ends at its time.
- **Finding F3: a paused Frame is not Resting.** The design language defines Resting as "Dark on purpose: Night off, paused, all off", the console design says "A paused Frame counts as Resting", and U1 lists Pause among intended darkness. All three were written before Pause had a behaviour. A Frame holding a lit photo is not dark, and a Frame that reads Resting while it glows would be the console claiming something false. This design words it **Showing**, with "Paused until 22:04 · **Carry on**" beneath. The three sentences change in the build's pull request; U1's is the owner's to approve.

### Show now on chosen Frames, with an end

- `ActivationRequest` gains `frame_ids` (explicit; the sheet pre-selects every current Frame) and `ends` (in 1 hour, at tonight's Night off, or until stopped). Today's `expires_at` keeps its one meaning, a queued request's expiry.
- The Run it starts covers only those Frames; the Scene's lamps and other actuators still take part (owner, S7, 2026-11-25): choosing Frames restricts Frames only.
- **Back to normal returns to the regular show as soon as it is pressed**, mid-photo, with the usual fade; the end time behaves the same, at that time and not at the end of the photo then showing (owner, S3). The only wait is a Frame whose regular photo is not downloaded yet; it changes when the photo arrives. Its lifecycle is downward cancellation at an instant, not natural completion ([Slice 3 design](#back-to-normal-and-a-takeovers-end)). (As first written: "the existing `runs/{id}/finish`: natural completion", which ends at the end of the photo then showing.)
- Default length and Frames are settings #84 and #85, on the sheet.
- Every chosen Frame participates explicitly; a Frame not chosen receives no implied command.

### Restart app

Roadmap G3 asks for "Restart app with progress". Issue #125 removed every automatic restart: three causes start a run, and "a run that dies stays `down` until a person reboots".

- A person pressing **Restart app** is not an automatic restart. This design adds a fourth `StartCause`, `asked_by_a_person`, reached by one new method on the app broker's bus line, called by a Central job as `RebootPi` is.
- The broker stops the current run and starts one. It never repeats the request by itself; a run that dies again stays down.
- Progress is `StepProgress`: requested → app stopped → app started → showing, each with its time, from the broker's existing `app_started` and `app_exited` events. (Built otherwise: the broker states the restart's own steps on a new bus line, [As built](#restart-app-as-built).)
- **Open O1:** the alternative is no Restart app at all, with **Reboot the Pi** as the only fix, which is what #125 left. The default here is to build it: a reboot takes minutes and blacks both of a Pi's Frames for one stuck app.

## Module map

| Path | Owns |
|---|---|
| `central/frame_status/model.py` | `StatusWord`, `FrameStatus`, `Problem`, `Fix`, `Because`, `HouseStatus`, the closed codes |
| `central/frame_status/policy.py` | The decision table, the cause table, the house word |
| `central/frame_status/ports.py` | One port per input above; `FrameStatusLog` |
| `central/frame_status/views.py` | `HomeView` and the Frame page's shapes |
| `central/infra/frame_status_store.py` | The inputs' reads in one transaction; the `frame_status` table |
| `central/home_routes.py` | `GET /v1/operator/home` |
| `central/wall_plan/never_show.py`, `infra/never_show_store.py` | The Never show rule and its list |
| `central/everyday/` (pure), `infra/skip_store.py`, `infra/pause_store.py` | Who moves and who stays, the cover and hold layers, the pause; slice 3 ([module map](#slice-3s-module-map)) |
| `appliance/apps/broker.py`, `central/fleet/app_restart.py` | The fourth start cause and its job |
| `central/console/src/domain/frame-status.ts` | The words for each code, the severity of each word, the chip; components only render |

**Import rule:** `central.frame_status` is pure and points down. It reads the vocabularies of `displays`, `power`, `why`, `central_errors`, `photo_library` and `contracts`; none of them imports it. It imports no `central.infra`, database, `nodeapi`, coordination or Planner (a new import-linter contract, "Frame status is pure").

## The console

## G1, G2, F2, F3, F8 Home — UX spec

**Object and home:** House, and each Frame as a tile · Home (settings catalogue #71, #84–#86)
**Template:** T1 Home · **Save model:** Action (per the rows of each step) · **Delivery:** 3b-i, slices 2 and 3

**Layout** (top to bottom):
- `AppShell` top bar: the house word (`HealthBadge`), everywhere in the console.
- The house word and one sentence: "All good · 6 Frames showing, 2 resting".
- `ProblemCard`s, worst first, each with its one fix button and Details.
- `OverrideBanner`s: a power hold ("Off — by Matt · **Resume schedule**"), a takeover (countdown · **Back to normal**), a pause ("Paused until 22:04 · **Carry on**").
- One `WallMap` per Wall, Frames to scale, read-only, with **Skip** and **Previous** for the whole wall at its head (S1, S2). Each `WallTile`: the Frame's name, its `StatusChip`, what it is doing in one line ("Evenings · until 23:00"), and an actions `Menu`: Skip this Frame, Previous on this Frame, Pause, Never show this photo, Why is this playing?, Open Frame. A tap on the tile opens the Frame page.
- The now/next strip per Wall.
- Quick actions: **Show now**, **All off**, **All on**.

**Settings:** none on Home. The Show now sheet holds #84, #85 and #86 as `SettingRow`s (autosave).

**States:** Loading: skeleton tiles in the walls' shapes · Empty: no Frame yet → the Setup checklist's next step · Error: Central did not answer → one house `ProblemCard`, tiles greyed with since · Can't tell: per tile · Feature states: a Frame in setup → a `todo` chip with its step and no actions; a tile action asked and not yet acknowledged → `AckBadge` "requested"; not acknowledged by its deadline → `ProblemCard` inline with Retry; Skip not allowed → the item is absent, never disabled.
**Live behaviour:** the document is read every 5 s and after each action (F2). Skip, Pause and Restart are acknowledged; Never show is optimistic with Undo.
**Copy:** the six words; "All good"; "Skip", "Previous", "Pause for 1 hour", "Carry on", "Never show this photo", "Won't show again · Undo", "Show now", "Back to normal", "Restart app", "Reboot the Pi", "Can't reach Pi pw-3f2a since 21:04", "Won't show at 07:00". Never "Player", "Output", "Run", "verdict", "plan" outside Details.
**Phone:** one column; tiles two across; the actions menu opens as a `Sheet`; quick actions move to the bottom tabs. Never show is two taps: the tile's menu, then the item (moment of truth 8).

**Patterns:** reused `ProblemCard`, `OverrideBanner` (power-hold), `StatusChip`, `HealthBadge`, `UndoToast`, `AckBadge`, `QuickActionSheet`, `Menu` · changed `OverrideBanner` (takeover and pause kinds), `AppShell` (bottom tabs, the house word) · new `WallMap`, `WallTile`, `StepProgress` (patterns, as the design language already lists for 3b-i); `Icon` (ui).
**Stories:** WallTile/Showing, /Resting, /NotShowing, /CantTell, /NeedsALook, /GettingReady, /Setup, /Requested, /Phone; WallMap/OneWall, /TwoWalls, /Empty; OverrideBanner/Takeover, /Pause; Home/AllGood, /Problems, /HouseProblem, /Loading, /Phone; StepProgress/Running, /FailedStep.
**Acceptance:** moments of truth 8 and 9 · automated: below · bench: Restart app on the test Pi, watched to "showing".
**Departs from DL §8 row:** F8's status cell, "the Frame reads Resting", becomes "the Frame reads Showing; Paused until 22:04" (finding F3). G3's home gains the Home problem card's fix button beside the Pi page.

The other homes take a spec each when their slice starts: **F6 phone layout** (`AppShell`: bottom tabs Home · Show now · Schedule · More below the `md` switch, which becomes a token, closing the design language's listed gap), **F4 Show now** (the sheet's Frames and length), **F3 Scenes › Never show list** (T2, `MediaGrid`, **Show again**), **G2 Frame page › Overview** (recent problems from the event log) and **G3 Pis › Pi page** (Restart app beside Reboot).

**What Home replaces.** The console lands on Home. The shell's attention strip and the Needs attention page fold into Home's problems, so there is one list of what is wrong. The Wall page stays under Frames, for setup.

## Acceptance

- `tests/test_frame_status_policy.py` (unit, pure): one test per row of the decision table, in order, with the row above it also true to prove precedence; the cause table exhaustive over `contracts/node_faults.py` and `FrameBlackCause`; every `Problem` names one of the six parts; the house word, and a house problem replacing the Frame problems it explains.
- `tests/test_frame_status_store.py` (DB): a tick that changes nothing writes nothing; a changed word writes one row and one event; `since` survives a Central restart.
- `tests/browser/test_home_browser.py` (the roadmap's name): each of the six words from fixture states; a 390-pixel viewport with no sideways scroll; every fixture fault's problem names its part in plain words (moment 9); Never show in two taps (moment 8).
- `tests/test_skip_never_show.py` (DB, the roadmap's name): a wall's Skip moves every Frame that can move at one instant and names each that stayed and why; a Frame's Skip moves it alone; Never show skips the photo where it shows, lets a downloaded showing elsewhere play once and names it; a listed photo is never assigned again, across re-plans (a property test beside the wall assignment's). As amended for the owner's rulings; every test is listed in [Slice 3 design › Acceptance](#slice-3s-acceptance).
- `tests/test_pause.py` (DB, controllable clock): the photo stays to the pause's end and no new placement gives it to another Frame; Carry on and the end both reveal the schedule's current state at once; Night off wins; a paused Frame stays paused when Show now starts; not on a video; a double tap is one pause.
- `tests/browser/test_show_now_browser.py` extended: Frames and an end time; Back to normal reveals the schedule's current state at once.
- `tests/node/apps/test_app_restart.py`: the fourth cause starts exactly one run; a run that dies again stays down.
- The console's import scan: nothing in `src/` computes a status word outside `domain/frame-status.ts`.

## Slices

| Slice | Holds | Size |
|---|---|---|
| 1 | Sign-in ([0035](0035-operator-sign-in-through-an-identity-provider.md)) | one pull request |
| 2 | Frame status in Central; Home, read-only; `AppShell` and the phone layout; Restart app | one pull request |
| 3 | Skip and Previous, Never show, Pause, Show now on Frames with an end | one pull request; its design is [Slice 3 design](#slice-3-design) |

Slices 1 and 2 touch disjoint files and can be built in parallel. Slice 3 needs both: its commands ask for `operate` and stamp a person.

## Findings and open questions, decided

The owner took each default on 2026-11-22 (R1), F3's change to U1's sentence included.

| # | What | Decided |
|---|---|---|
| F1 | A failure during intended darkness has no word in the design language | Needs a look until the display is next due on, then Not showing |
| F2 | Home is polled, not pushed, until 3b-ii | Listed as a polled exception |
| F3 | Three documents say a paused Frame is Resting; with a frozen photo it is not dark | Showing, with "Paused until …"; U1's sentence needs the owner |
| O1 | Restart app against #125's "no auto-restarts at all" | Build it as a person's request, never repeated |
| O2 | Is Previous in 3b-i? The roadmap's outcome names only Skip | In, as the same command |
| O3 | The silence limit of row 4 and the continuation limit of row 11 | Today's console threshold for the first; one hour for the second, to tune |

## The owner's rulings for slice 3 (2026-11-24)

Given in chat on 2026-11-24 and 2026-11-25, after a read-only design pass traced slice 3 through the Planner and Coordination. They govern this record wherever it differs, and the requirement they change is amended in [requirements](../requirements.md#live-media-compatibility-and-preparation). Quoted words are his; the rest of each row is the ruling as he gave it in substance, not his words.

| # | Ruling | His words | Replaces |
|---|---|---|---|
| S1 | **Skip moves the wall.** Skip on Home moves every Frame of the wall it is pressed on to its next photo at the same instant. "If possible": a Frame that cannot move (a hand-placed photo, a deliberately black Frame or an ending, a Pi not answering or playing Central's continuation, nothing to skip) simply stays, and the answer says which stayed. A Frame's own menu also keeps a Skip for that Frame alone. Other walls are not moved. | "A 'skip' should progress the whole show together (if possible)." Asked who had asked for a per-Frame Skip: nobody; it came from agent-written documents (roadmap F2 "on a tile", the console design's Frame-page Skip). | A per-Frame Skip on a tile, refused on a secured layer |
| S2 | **Previous is the same as Skip:** wall-wide on Home, per Frame in a Frame's menu; each further press steps back one more photo. | (in substance) | Previous as one step, per Frame |
| S3 | **Back to normal is immediate:** the regular show comes back as soon as it is asked, mid-photo, with the usual fade. A takeover's end time behaves the same: it ends at that time, not at the end of the photo then showing. The only wait is a Frame whose regular photo is not yet downloaded; it changes when the photo arrives. | "'Back to normal' should go back to the regular show as soon as the command is entered" | Back to normal as `finish` (natural completion at the end of the current photo) |
| S4 | **The same photo on two Frames is allowed now and then.** The requirement becomes a strong preference: the planner keeps avoiding duplicates exactly as now, but an everyday action (Skip, Previous, Pause, Back to normal) may cause a brief overlap rather than be refused or delayed. Pause's "photo due elsewhere" refusal and any Previous refusal for a photo queued elsewhere go. | "it's acceptable for different frames to occasionally show the same photo. It's not desirable, but it's ok when it happens." | "Never show the same photo on two frames at the same time" (owner, 2026-10-11) as a hard rule |
| S5 | **Never show:** a photo already queued (downloaded) on another Frame shows there that one last time, then never again; the toast says so. The download lock stays a hard rule. | The download lock: "once a Pi has downloaded a photo for a slot, that slot does not change" | "On a secured layer it stays until that Run ends" |
| S6 | **A paused Frame stays paused when Show now starts;** Show now plays beneath it and appears at Carry on. | (in substance) | open (D4 of the design pass) |
| S7 | **Show now on some of a Scene's Frames:** the Scene's lamps and other actuators still take part; choosing Frames restricts Frames only. | (in substance) | open (D5) |
| S8 | **Pause is not offered on a video** (refusal code `video`). | (in substance) | open |
| S9 | Defaults he did not contest: the Frame page's fit counts and the pool counts leave out Never show photos; Pause's default length is 1 hour (setting #86). | — | — |

## Slice 3 design

Written 2026-11-25 from the design pass's reading of the code and the owner's rulings. Accepted as this record's design for slice 3 by the rulings above; every choice below that is not one of S1 to S9 is a design choice, not his ruling. What is built, and where it differs, is [As built (slice 3)](#as-built-slice-3-skip-previous-and-never-show).

### What the code fixes

Four facts of the code as it stands decide the design (read in `central/coordination.py`, `central/planner.py`, `central/runtime.py`, `player/executor.py`):

1. **Every offered layer is a lock, and the layer on screen is always secured.** `Coordinator._locks` hands the Planner every layer of every unexpired offer; `planner._place` re-emits a lock unchanged except its `REVISABLE` fields (arbitration, `fade_out`, `after_end`). The Player refuses a plan that changes a secured assignment's content (`executor.py` `_content_identity`, "secured assignment content is immutable"), and that identity holds `start` and `end`.
2. **An assignment's identity is its interval.** Its id hashes the Run, the target, the phase and the turn's interval, so a layer cannot be shortened or lengthened and stay the same assignment.
3. **A Run has one clock for all its Frames** (`_Run.cycle_start`, `cycle_end`): the Runtime cannot advance one Frame's turn alone, nor one wall's when the Run spans two.
4. **Finish completes the current turn:** `Runtime.finish` sets `scheduled_finish_at` and the body ends at the turn's end (`_last_turn`); only `cancel` ends at once, and it withdraws the layer on screen with no fade of its own.

So: **Central never changes or withdraws an offered layer for an everyday action. It adds a layer above it** (a **cover** for Skip and Previous, a **hold** for Pause), and it ends a layer early only through one new revisable field, **`cut_at`**, that never touches the layer's content or identity. "Locked layers never change" and "a secured assignment keeps its content" then hold literally, and the download lock (S5) with them.

### Two contract changes

Both in `contracts/models.py`; Central and the Pis ship together, as every contract change since 0032.

- **`Layer.cut_at: Instant | None`**, Central's early end: the layer is drawn until `cut_at`, its fade-out (revised with it to the Scene's Transition seconds, bounded by the layer's span) ending there. It joins `REVISABLE` (`central/planner.py`) and stays out of the Player's `_content_identity`. It may be set once and only ever brought earlier; a revision that removes it or moves it later is refused by the Player (`cut_is_final`) and never sent by Central. Where a layer ends is `drawn_end = min(end, cut_at)`, one function in `contracts/models.py` read by the on-screen extent (`contracts/on_screen.py`), the plan's validity (`planner.py` `result`), `may_still_play` and the Player's draw. A layer whose start is at or after its Run's cut is not offered at all (a withdrawal, as today's cancellation does), and the Planner diagnoses such a lock `lock_cut`, which is not a planning issue.
- **`Layer.action: bool`** (serialised only when true, as `awaits_grant` is), and `stack_order` gains it before `start`: `(priority, root_order, admission_order, action, start)`. An action layer sits above every regular layer of its own precedence whatever their starts, and of two action layers the later start is on top (a second Skip over the first). A Run of greater precedence stays above both. `action` is content identity, never revised.

**The Player's one wait (S3).** At a cut, the Player reveals what is beneath it. When the layer beneath that should show is not prepared (never downloaded, or its place given up for capacity, [execution contract](../execution-contract.md#a-scenes-transition-when-a-photo-starts-to-show)), the cut layer is drawn on past its cut, as 0032's hold draws a superseded composition, until that layer is prepared, and then fades. A Frame never goes black at a cut. One rule, `player/executor.py`, for Back to normal, a takeover's end and Carry on.

### The everyday package

A new pure package, `central/everyday/`, imports only `contracts` (a new import-linter contract, "Everyday actions are pure": no `central.infra`, database, FastAPI, `nodeapi`, coordination, Planner or Runtime). `central.frame_status` may import it (pure, pointing down; the contract "Frame status is pure" adds it to what it may read).

| Module | Owns |
|---|---|
| `model.py` | `SkipKind` (`next`, `previous`); `StayReason` (below); `PauseRefusal`; `Cover`, `Hold`, `Pause`; `Asked` and `AskState` (`requested`, `done`, `late`); the constants `SKIP_LEAD` (2 s), `END_LEAD` (2 s), `ACK_LATE_SECONDS` (30 s), each a placeholder to tune on the bench, exported to the console's contract |
| `policy.py` | `showing_at(layers, at)`: the Frame's topmost opaque media layer by `stack_order` drawn at `at` (the target of every action; it restates the covered rule of `player/visibility.py`, held equal by a test over shared cases); `stays(facts) -> StayReason \| None` and `pause_refusal(facts)`, **the one home of who can move**, called both for `can` and by the command under the lock; `the_shared_instant(now)` (`now + SKIP_LEAD`); `joins(layers, showing)` (Skip's next photo, below); `previous_step(history, chain)`; `cover_for` and `hold_for`, which build the action layer as a `contracts` `Layer`; `asked(...)` |
| `ports.py` | `FrameFacts` (what `stays` reads, below) and the stores' protocols |

**`FrameFacts`**, one per Frame, read in one transaction: its binding and setup; its Pi linked and heard, and `continuing_since` from the Player line's Frame report; its display wanted on or off now (`power/timetable.py`); the layers of its Player's newest valid offer on it, with the Runtime Intent behind the showing layer (hand-placed when the Intent has `asset_refs`; an ending when its phase is `outro`); its live pause; its newest skip.

### Skip and Previous: who moves and who stays

**`StayReason`**, closed, first match wins in `stays`:

| Code | When | Words (console) |
|---|---|---|
| `not-set-up` | The Frame is in setup | "isn't set up yet" |
| `not-answering` | Its Pi is not linked or not heard past the silence limit, or its Frame report says it plays Central's continuation | "its Pi isn't answering" |
| `resting` | Its display is wanted off now (a showing into wanted-off time is never reported, so it could never be acknowledged) | "it's resting" |
| `paused` | A pause holds it | "it's paused" |
| `nothing-to-skip` | No media layer is drawn | "nothing is showing" |
| `black` | The showing layer is a black one, or a Scene's ending | "it's dark on purpose" |
| `hand-placed` | The showing photo is hand-placed (its Intent names `asset_refs`) | "its photo is hand-placed" |
| `no-previous` | Previous only: the Frame's shown history has nothing older to step back to | "it has nothing earlier to show" |

A video moves like a photo. A Frame whose own change starts within the lead is not a stay: its cover is simply short.

**State and owner.** Coordination owns it; one table, `skips` (migration 133), one row per request, the aggregate: `request_id` (primary key), `scope` (`wall` or `frame`), `target_id` (the wall's `surface_id` or the Frame's id), `kind` (`next`, `previous`, `never-show`), `at` (the shared instant), `outcome` (JSON: per Frame, its `Cover` or its `StayReason`), `ends_at` (the last cover's drawn end, for pruning), and the audit fields stamped with the 0035 actor. Repository `central/infra/skip_store.py` `PgSkips`, called inside the Coordination lock. A row is written once and never updated; rows past `ends_at` by a day go in the batched retention (`infra/batched_retention.py`).

**Routes** (in a new `central/everyday_routes.py`, each asking `OperatorAuth.operate`):

```
POST /v1/operator/walls/{surface_id}/skips   {request_id, kind: "next" | "previous"}
POST /v1/operator/frames/{frame_id}/skips    {request_id, kind: "next" | "previous"}

200 SkipAnswer {request_id, kind, at, duplicate,
                moved:  [{frame_id}],
                stayed: [{frame_id, reason: StayReason}]}
409 SkipRefusal {code: "nothing-moved", stayed: [...]}   # no Frame of the wall could move; for one Frame, its reason
404 unknown wall or Frame · 422 invalid_operator_skip · 503 lock_busy
```

Previous is carried in the body's `kind`, not in its own route: one command, one store, one answer. A repeated `request_id` answers the stored row with `duplicate: true` and changes nothing.

**The command,** `Coordinator.skip(scope, target, kind, request_id, by)`, in `Coordinator._transaction`: it reads `FrameFacts` for every Frame of the wall (Frame id order) or the one Frame, takes `at = the_shared_instant(now)` once, and for each Frame either records its stay or builds its cover. The commit wakes the scheduler and the Player sockets (`notify_if_written_in`), so the next planning pass offers the covers within a second.

**The cover, for `next`** (the Frame's own next photo, so the show progresses): `joins` is the Frame's next regular media layer `J` of the showing layer's Run in the newest offer, after the showing one, that is not black, not an ending and not on the Never show list. The cover shows `J`'s photo with `J`'s variant and crop from `at` to `J.start + J.fade_in`, and then `J` itself goes on with the same picture, with no seam (the seam rule, `player/look.py`). So every Frame of a Run steps to its next turn's photo at once and the Run's turns go on as planned. With no such `J` offered (a turn longer than the plan's 5-minute reach, or the next turn is another Run's), the cover is a **pick**: the wall plan fills it with the Scene line's next prepared photo, never the photo left, and it ends at the showing layer's end with that layer's fade-out.

**The cover, for `previous`:** a pick pinned to one photo `P` (`previous_step`): the Frame's shown history (`shown_photos` by `frame_id`, newest `presented_at` first, rows of earlier Previous covers left out) read back from the **anchor**, the photo showing at the first press of the chain. The chain is the Frame's newest skip rows while each is a Previous whose cover is still drawn; a press while it lasts steps one further back (steps + 1), any other photo presented on the Frame ends it. Photos that no longer fit the Frame, are gone from Immich or are on the Never show list are passed over. `P` is placed even when another Frame shows it (S4). Its cover ends where the Frame's next regular layer `N` is fully in (`N.start + N.fade_in`), fading out over `N`'s fade-in. History is kept 400 days (`SHOWN_KEEP_SECONDS`), so the chain ends there at the latest.

**Every cover:** id `identity("cover-", [request_id, frame_id])`; the showing layer's Run, priority, root order and admission order; `action` true; `start = media_origin = at` (a video starts from its beginning); `fade_in` the Scene's Transition seconds; `after_end` that of the layer it joins or leaves. **A wall's covers are one cue:** they carry `awaits_grant` and form one coordination group with its deadline at `at`, so they start whole before the deadline wherever every member's Pi is ready and each joins alone after it ([a cue's two phases](../execution-contract.md#a-cues-two-phases)). That is "at the same instant, if possible" with the machinery 0031 already proves. This extends 0032's rule 3 ("a grant only for a change"): a wall's Skip is such a change (G7, "all pi's together"). A Frame's own Skip carries no grant.

**What the Planner emits.** `Coordinator._plan` reads the live skips and passes `project(covers=...)`. A pure `central/wall_plan/covers.py` turns each into a layer: a `joins` cover is emitted exactly as built; a pick is a slot of the wall plan, walked before every other slot (a person asked for it), with two new `Slot` fields, `excluded` (the photo left) and `repeat=False` (so step 4 never re-places the skipped photo); a Previous pick is a hand-placed slot (`authored=(P,)`), which needs one new read, candidates by asset id, in `PgSceneLines`. Inside the preparation horizon a slot takes only prepared photos: a pick whose photo is not prepared stays unplaced and is re-planned each pass, its start `max(at, the pass's now + SKIP_LEAD)`, so the Frame changes when the photo is ready, as S3's one wait. A cover is never an `UnfilledSpan` (its regular layer stays beneath) and never a continuation candidate (`planner._segments` leaves action layers out). Once offered a cover is a lock like any other; its skip row is its Intent, so it is carried forward while the row is live.

### Duplicates: allowed for an action, still avoided by the plan

S4 turns the two-Frames rule into a strong preference, and the design draws the line once, in `central/wall_plan/assignment.py`:

- **Still forbidden:** every slot the wall plan places itself, a pick cover's choice among free photos included. Step 3 (["never on two Frames at once"](../module-planner.md#the-wall-plan)) is unchanged, and the extents of every action layer count against every placement, so the plan never adds an overlap against an action.
- **Allowed:** an action layer's own overlap with what is already offered or kept elsewhere: a cover joining `J`'s photo early, Previous's pinned photo, a hold, a pick with no free photo left (it takes the line's next rather than be refused), and what Back to normal reveals. None of these is refused or delayed for a duplicate.
- **Tests:** the wall assignment's property test (no photo's extents overlap on two Frames) holds over every pair where neither layer is an action layer, and gains a second property: no layer placed in a pass overlaps an action layer of an earlier one.

### Never show: the list and its last showing

**State and owner.** The wall plan owns the list: table `never_show` (migration 134), primary key `(connection_ref, upstream_id)` (the Immich asset as `library_assets` keys it, so an edit's new revision stays listed), `request_id` (unique), `listed_at`, `by`, audit fields. Pure rule `central/wall_plan/never_show.py`; store `central/infra/never_show_store.py`.

**Routes** (`everyday_routes.py`, `operate`): `POST /v1/operator/never-show {request_id, asset_id, frame_id?}`, answering `{asset_id, skipped_on: [frame_id], stayed: [{frame_id, reason}], last_showings: [{frame_id, from, until}]}`; a `frame_id` whose showing photo is no longer `asset_id` answers 409 `changed` (the photo turned over under the thumb). `DELETE /v1/operator/never-show/{asset_id}` is Undo and **Show again**. `GET /v1/operator/never-show` is the list page's read (`admin`).

**Eligibility, one rule in two places.** In SQL, `_LINE_ROWS` (`central/infra/photo_library_store.py`) gains `NOT EXISTS never_show`, so a listed photo is no line member and the deeper-read loop of `_read_line` cannot starve on it; `PoolLines` takes the same set, held equal by `tests/test_order_sql_conformance.py`. In the pure plan, `project(never=...)`: step 4 never re-places a listed previous photo, a hand-placed slot passes over a listed ref, a kept still is never a listed photo's fallback, a cover never joins or picks one, and continuation lists leave them out. An empty pool is not relaxed: the slot is unfilled as today. The Frame page's fit counts and the pools' counts take the same join (S9).

**Where it shows now.** In the same transaction each Frame drawing it gets a cover (a skip row of kind `never-show`) where `stays` lets it move; a paused Frame drawing it has its pause ended there (Carry on), so the reveal shows the schedule beneath.

**Its last showing (S5).** A layer of it already offered to another Frame (downloaded or downloading for that slot) is a lock and plays: once, since nothing places it again. The answer's `last_showings` names each, and the toast says so. A Frame that cannot move (`stays`) shows it to its end, once, and the answer says why.

### Pause: the hold

**State and owner.** Coordination writes it; the pure rules are `central/everyday/`. Table `frame_pauses` (migration 135), one row per Frame as `power_holds` is: `frame_id` (primary key), `pause_id` (the request id; one request may pause several Frames), `source` (the 0035 actor: the one `source` type the power hold takes too), `started_at`, `ends_at`, `ended_at`, `held` (the hold layer as built), audit fields. Store `central/infra/pause_store.py`. A row changes only at its pause, its Carry on and its replacement by a later pause.

**Routes:** `POST /v1/operator/pauses {request_id, frame_ids, seconds?}` (default setting #86, 3600 s), answering `{pause_id, ends_at, paused: [frame_id], stayed: [{frame_id, reason}]}`; `DELETE /v1/operator/pauses/{pause_id}` (Carry on, every Frame of it) and `DELETE /v1/operator/pauses/{pause_id}/frames/{frame_id}` (one Frame).

**`PauseRefusal`**, closed: `not-set-up`, `not-answering`, `resting`, `paused` (already paused: its banner offers Carry on), `nothing-to-hold` (no media layer, or a black one), `video` (S8). No refusal for a photo due on another Frame (S4).

**The hold layer,** built at the command from `showing_at(layers, at)`, `at = the_shared_instant(now)`: id `identity("hold-", [pause_id, frame_id])`, Run `pause-<pause_id>` (never a continuation candidate), a priority above every rank (`PAUSE_RANK`, one constant beside `central/runtime.py` `PRIORITY_RANK`), `action` true; the held layer's variant, crop and media origin; `start = at`, `end = ends_at`; no fade-in (the same picture: no seam); `fade_out` the Scene's Transition seconds, so its end reveals the schedule with the usual fade; `after_end` `leave_as_is`; no grant. The Planner emits each live pause's hold as given (`project(holds=...)`), records a `photo_selections` row with the held layer's Scene and pass so shown history and the acknowledgment work, and counts its extent against every placement.

**Carry on** sets `ended_at` and the hold's `cut_at = now + END_LEAD +` its fade-out; the next pass re-emits the hold with it (a revisable field). Carry on and the pause's own end both reveal the schedule's current state: the Runtime is untouched, so the schedule beneath kept advancing as covered layers, kept prepared for reveal. **Power wins:** display power is outside the plan, so Night off turns the display off during a pause and the pause still ends at its time. **Over Show now (S6):** the hold's rank is above every Run, so a Show now admitted during a pause plays beneath it and appears at Carry on. A paused Frame reads **Showing**, with "Paused until 22:04" (finding F3).

### Back to normal and a takeover's end

**Lifecycle, said plainly: downward cancellation at an instant, with a fade.** The Run and every descendant stop at one instant; no Scene ending plays and no child is waited for; the Run ends `cancelled`. It is not natural completion, which stays as it is (`Runtime.finish`: the current turn runs out, children finish, the Scene's ending plays, the Run ends `completed`) for every Run that ends by itself: a Program's window, a Scene's duration, a Default Scene's change, Save and apply now's predecessor, and the existing `runs/{id}/finish` route. The two remain distinct ([execution contract › failure behavior](../execution-contract.md#failure-behavior)).

- **The Runtime** gains `Runtime.end(run_id, at)`: from `at` the Run's and its descendants' current Intents carry `cut_at = at + ` the Scene's Transition seconds (a new `Intent.cut_at`, copied to the layer), no turn starting at or after `at` is projected, and at `cut_at` the Run and its descendants are `cancelled`. **Back to normal** is a new route, `POST /v1/operator/runs/{run_id}/end {request_id}` (`operate`), with `at = now + END_LEAD`; it answers `{run_id, at, until}`.
- **A takeover's end** is the same cut, planned from its admission: `_Run.ends_at` is admitted with the Run, so its last turn's Intent carries `cut_at = ends_at` from its first offer and no offered layer is revised for it later; the fade finishes at the end time.
- **What is revealed is the schedule's current state.** The Runs beneath kept advancing while covered ([covered photos](../execution-contract.md#a-scenes-transition-when-a-photo-starts-to-show)); at the cut the Player draws the layer beneath at its logical position (a video at `now - media_origin`) and replays no hidden turn ([current-state reveal](../execution-contract.md#time-and-current-state-reveal)). The one wait is the Player's (above).
- **Why not natural completion with a cut turn:** natural completion waits for children and plays the Scene's ending, which would postpone "as soon as the command is entered"; a design choice, open to the owner if he wants a Show now Scene's ending played when it is ended by hand.

### Show now on chosen Frames

- `ActivationRequest` gains `frame_ids: tuple[Identifier, ...] | None` (None: the whole Scene, as a script sends today; the sheet always sends them explicitly) and `ends: {kind: "in", seconds} | {kind: "night-off"} | {kind: "stopped"}`. Central resolves `ends` to an instant (tonight's Night off from the power policy through `house_time`). The route moves to `operate`.
- `Runtime.activate(..., frame_ids=None, ends_at=None)`. A pure `restricted_to(scene, frames)` in `central/runtime.py` drops the contributions, endings and children's contributions on Frames not chosen and keeps every target that is not a Frame (`actuator:` and the Scene's lamps, S7). `_Run` gains `frames` and `ends_at`, both kept by `_replace` (Save and apply now does not widen the Run back to every Frame, and the successor keeps its end) and by `_Queued`. `RunView` gains them; Home's takeover `Override` reads its Frames and `until` from them.
- `_matching` matches a Scene id and overlapping Frames, so a second Show now of the same Scene on other Frames is admitted, not ignored. Protection is judged on the restricted participants.
- Refusals (422): `no-frames`, `frame-not-in-scene`, `no-night-off` (no Night off tonight), `ends-passed`.
- A Frame not chosen has no contribution, so it gets no layer and no implied command. No Planner change.

### What Home carries

- **`Action`** (`central/frame_status/model.py`) gains `skip`, `previous`, `pause` and `never-show`, each present per `stays` or `pause_refusal` over the Frame's `FrameFacts`, read by `PgFrameStatus` through a new port (`FrameShowing`) over `Coordinator.frame_plans_in`, extended with the showing layer's kind. The command re-checks the same rule under the lock, so `can` and the answer agree on one state; the race between them answers the same closed code.
- **`WallView.can`** gains `skip` and `previous`, present when any Frame of the wall offers it.
- **`FrameStatus.asked: Asked | None`**: `{action (skip, previous, never-show, pause, carry-on, back-to-normal), request_id, at, state}`. `requested` until the Player reports the action's layer `presented` (its `shown_photos` row; for Carry on and Back to normal, a layer of the schedule beneath presented at or after the cut), then `done`; `late` when not done by `at + ACK_LATE_SECONDS`. One source of the acknowledgment: shown history, written by `Coordinator._observe`. It moves Home's `change`.
- **`Override`**: the pause kind is produced (by whom, which Frames, until), and a takeover's `until` and Frames come from the Run.
- `ACTION_PATHS` (`central/home_routes.py`) names each new route. The decision table does not change.

### The console

Words follow the [design language](../design-language.md#7-content-and-voice); every code is worded in one exhaustive `Record` over the generated contract, in a new `domain/everyday-words.ts`.

- **A wall's Skip and Previous:** each `WallMap` on Home carries **Skip** and **Previous** at its head (a new pattern, `WallActions`; accessible names "Skip every Frame on Living room" and "Previous on every Frame on Living room"), drawn only when `WallView.can` has them. They are acknowledged: each Frame that moved shows `AckBadge` "requested" until done. The answer's toast: "Skipped on 5 Frames." or "Skipped on 4 Frames. Hall stayed: its photo is hand-placed." (one clause per stay, worded per `StayReason`). A Frame still requested at `late` gets an inline `ProblemCard`: "Hall didn't change. Its Pi hasn't reported the new photo." with **Retry**.
- **A Frame's menu** (each tile's, and the Frame page's): **Skip this Frame**, **Previous on this Frame**, **Pause for 1 hour**, **Never show this photo**, then Restart app, Reboot the Pi, Why is this playing?, Open Frame; each absent when `can` does not have it.
- **Pause:** acknowledged; the tile's line "Paused until 22:04"; an `OverrideBanner` of kind `pause`: "Paused until 22:04 · Hall, Desk" with **Carry on** (acknowledged).
- **Never show:** optimistic, with `UndoToast` "Won't show again · Undo"; when the answer names a last showing: "Won't show again. It shows once more on Hall, where it's already downloaded. · Undo". **Scenes › Never show list** (T2, `MediaGrid`, setting #71) with **Show again**.
- **Show now** (a `QuickActionSheet`): the Scene, **Frames** (`TargetPicker`, every current Frame of the Scene selected explicitly, #85), **Ends**: "In 1 hour" (#84), "At Night off (22:30)", "Until I stop it", then **Show now**. Its banner, kind `takeover`: "Holidays on 3 Frames · until 22:00" with **Back to normal** (acknowledged).
- Never "Run", "plan", "layer", "cover", "hold" or "Player" outside Details.

### Slice 3's module map

| Path | Owns |
|---|---|
| `contracts/models.py` | `Layer.cut_at`, `Layer.action`, `drawn_end`, `stack_order`'s new key |
| `central/everyday/` | Above: who moves, who stays, the cover and hold layers, the pause, the asked state |
| `central/wall_plan/covers.py`, `never_show.py` | Covers into the wall plan; the Never show rule |
| `central/planner.py`, `central/wall_plan/assignment.py` | `project(covers=, holds=, never=)`; `cut_at` in `REVISABLE`; action layers out of continuations; the duplicate line |
| `central/coordination.py` | The skip, pause and Never show commands under its lock; the live rows into `_plan` |
| `central/infra/skip_store.py`, `pause_store.py`, `never_show_store.py`; migrations 133–135 | The three tables |
| `central/runtime.py`, `central/runtime_store.py` | `end`, `frames`, `ends_at`, `restricted_to`, `Intent.cut_at` |
| `central/everyday_routes.py`, `central/app.py` | The routes; `ActivationRequest` extended; `runs/{id}/end` |
| `central/frame_status/` and `infra/frame_status_store.py` | `can`, `WallView.can`, `asked`, the pause override |
| `player/executor.py`, `player/visibility.py`, `player/look.py` | `cut_at`, the one wait, the stacking key |
| `central/console/src/` | `WallActions`, the menus, the sheet, the banners, the toasts, the list page; `domain/everyday-words.ts` |

### Build order

0. **First, the offer backpressure check** ([below](#open-risks)): `tests/test_coordination.py::test_a_long_layer_never_starves_its_players_offers` against today's code, then the fix if it fails. Nothing of Pause is built before it passes.
1. The contract (`cut_at`, `action`, `stack_order`) and the Player's draw of it, with the [render bench](../runbook.md#the-render-bench-and-pi-testing-before-merge) step for the seams.
2. `central/everyday/` and the Planner's covers, holds and Never show (pure).
3. Coordination's commands, the stores and migrations, the routes.
4. The Runtime's `end`, Frames and end time.
5. Frame status and Home's document.
6. The console.
7. The documents the build changes: the execution contract's `REVISABLE` sentence and current-state reveal, [Planner › the wall plan](../module-planner.md#the-wall-plan) (covers, holds, the duplicate line), 0032's rule 3 (a wall's covers await a grant), the design language's Home members, the runbook, AGENTS.md's map.

### Slice 3's acceptance

**Step 0, the risk first.** `tests/test_coordination.py::test_a_long_layer_never_starves_its_players_offers` (DB, controllable clock): one Player, one Frame on an hour-long turn beside one on 10 s turns; passes driven for 40 minutes; every pass whose proposal changes offers a new revision and none is left out `offer_backpressure`. Then `tests/test_pause.py::test_a_pause_never_starves_its_players_offers`, the same with an hour's hold.

**Unit (pure):**

- `tests/test_everyday_policy.py`: `test_each_stay_reason_in_order` (each row with the row above also true); `test_showing_at_agrees_with_the_players_covered_rule`; `test_skip_joins_the_frames_next_regular_photo`; `test_previous_steps_back_one_more_each_press`; `test_previous_chain_ends_when_another_photo_is_presented`; `test_pause_refusals` (video among them); `test_asked_is_requested_then_done_or_late`.
- `tests/test_planner_wall_assignment.py`: `test_a_joining_cover_is_emitted_unchanged_above_its_layer`; `test_a_pick_cover_takes_the_lines_next_never_the_photo_left`; `test_a_previous_cover_places_its_photo_even_where_another_frame_shows_it`; `test_no_placement_overlaps_an_action_layer` (property); the existing property limited to regular layers; `test_a_listed_photo_is_never_assigned_again_across_replans` (property); `test_a_hold_is_emitted_over_every_run`.
- `tests/test_on_screen_extent.py`, `tests/test_contracts.py`: `test_a_cut_layer_ends_at_its_cut`; `test_cut_at_is_revisable_and_only_earlier`; `test_an_action_layer_stacks_above_its_runs_layers`.
- `tests/test_player_executor.py`: `test_a_cut_layer_fades_out_at_its_cut`; `test_a_cut_layer_is_drawn_until_the_layer_beneath_is_prepared`; `test_a_revision_moving_a_cut_later_is_refused`; `test_a_cover_joining_its_layer_draws_no_seam`.
- `tests/test_runtime.py`: `test_show_now_on_chosen_frames_leaves_the_others_untouched`; `test_show_now_on_chosen_frames_keeps_the_scenes_lamps`; `test_back_to_normal_cuts_the_takeover_mid_photo`; `test_back_to_normal_is_downward_cancellation_and_plays_no_ending`; `test_a_takeovers_end_cuts_at_its_time`; `test_save_and_apply_now_keeps_a_takeovers_frames_and_end`; `test_a_second_show_now_of_one_scene_on_other_frames_is_admitted`.
- `tests/test_frame_status_policy.py`: `test_can_follows_the_stay_rule`; `test_a_walls_can_is_any_of_its_frames`; `test_a_paused_frame_reads_showing_with_its_pause`.

**DB:**

- `tests/test_skip_never_show.py`: `test_a_wall_skip_moves_every_movable_frame_at_one_instant`; `test_a_wall_skip_names_each_frame_that_stayed_and_why`; `test_a_wall_skip_leaves_other_walls_alone`; `test_a_frame_skip_moves_that_frame_alone`; `test_skip_leaves_the_covered_layer_unchanged`; `test_skip_beyond_the_reach_takes_the_scene_lines_next`; `test_previous_shows_the_photo_the_frame_last_reported`; `test_previous_again_steps_back_one_more`; `test_skip_may_show_a_photo_another_frame_shows`; `test_a_double_tap_is_one_skip`; `test_skip_reads_requested_until_the_cover_is_presented`; `test_never_show_skips_it_where_it_shows`; `test_never_show_lets_a_downloaded_showing_elsewhere_play_once_and_names_it`; `test_a_listed_photo_is_never_assigned_again`; `test_an_empty_pool_does_not_bring_a_listed_photo_back`; `test_show_again_returns_it_to_the_line`; `test_fit_and_pool_counts_leave_out_listed_photos`.
- `tests/test_pause.py`: `test_the_photo_stays_to_the_pauses_end`; `test_no_placement_gives_the_held_photo_to_another_frame`; `test_pause_is_refused_on_a_video`; `test_carry_on_reveals_the_schedules_current_state_at_once`; `test_the_pauses_end_reveals_the_schedules_current_state`; `test_night_off_wins_and_the_pause_still_ends_at_its_time`; `test_a_paused_frame_stays_paused_when_show_now_starts`; `test_a_double_tap_is_one_pause`; `test_a_pause_never_starves_its_players_offers`.
- `tests/test_everyday_routes.py`: `test_every_everyday_route_asks_for_operate`; `test_a_repeated_request_answers_the_stored_answer`; `test_back_to_normal_ends_the_takeover_at_once`.

**Browser:**

- `tests/browser/test_home_browser.py`: `test_a_walls_skip_moves_the_wall_and_names_who_stayed`; `test_a_frames_menu_skips_and_steps_back_that_frame`; `test_a_late_skip_shows_a_card_with_retry`; `test_pause_shows_its_banner_and_carry_on`; `test_never_show_in_two_taps_with_undo` (moment 8); `test_never_show_names_a_last_showing`; `test_an_action_not_offered_is_absent`.
- `tests/browser/test_show_now_browser.py`: `test_show_now_on_chosen_frames_with_an_end`; `test_back_to_normal_reveals_the_schedules_current_state_at_once`.
- `tests/browser/test_never_show_list_browser.py`: `test_show_again_takes_a_photo_off_the_list`.

**Bench** (no automated tier): on the test Pi, a cover's seam into its joined layer, a cut's fade into the layer beneath, a hold's seam, a Previous whose photo must be downloaded, and a wall's Skip timed across two Frames.

### Where the rulings cannot be met exactly

- **"At the same instant" (S1)** holds for the Frames whose Pi holds the new plan and its photo by the instant. A Frame whose Pi gets them later (a slow Central, as on 2026-11-15, or a photo still downloading) changes alone when it can, the cue's second phase. That is "if possible" read as the machinery can keep it.
- **"That one last time" (S5)** holds for every Pi that hears Central. A Pi cut off from Central keeps the continuation lists it holds ([0032](0032-the-player-continues-centrals-plan.md)) and may cycle a listed photo more than once until it hears Central again; its next offer's lists leave the photo out. The proposal is to accept that; the alternative, a list sent to the Pis, is a new Node fact for a rare case.
- **"As soon as the command is entered" (S3)** is `END_LEAD` (2 s, for the plan to reach the Pis) plus the Scene's fade, besides the owner's own exception (a photo not downloaded). Ending by hand plays no Scene ending (above).
- **"One more back each time" (S2)** is bounded by the Frame's shown history (400 days), passes over photos that no longer fit or are listed, and waits for the photo's download when the Pi no longer holds it, so a wall's Previous is less often at one instant than a Skip.

### Open risks

- **Offer backpressure, unverified.** `_propose_one` leaves a Player out (`offer_backpressure`) once it has `max_offers` (64) unexpired offers in its epoch. An offer is valid to its longest layer's end, and a new revision is offered whenever its proposal changes, at most about every 30 s renewal. A one-hour hold (or today any hour-long turn beside short ones) keeps every revision unexpired, so that Pi would be offered nothing new after about 32 minutes, for both its Frames. Step 0 proves or refutes it. **The fix if real:** backpressure counts only the offers past the revision the Player's latest readiness names (what it guards against is a Player that does not read its offers), and `_cleanup` deletes an unexpired offer superseded by an acknowledged later revision when every one of its layers not yet ended is in that revision as the same execution and `_may_hold` is false, so `plan_offers` holds no hour of revisions.
- **The contract change ships with the Pis:** a Pi on an older release refuses a plan carrying `cut_at` or `action`; deploy Central, then reboot the Pis, as 0032 did.
- **`stack_order`'s new key** reaches every comparison of layers on both sides; the shared function keeps them one, and the Player's and Central's tests over it must both run.
- **Seams are unseen:** a cover's join, a cut's fade over a photo the Player re-decodes for its reveal, and a late start inside a fade are bench evidence, not CI.
- **An hour's hold pins its bytes on the Pi for the hour** (`may_still_play`), one more resident picture per paused Frame.
- **Shown history counts a joined photo twice** (the cover, then its layer at the seam): it ranks lower sooner, nothing worse.
- **The skipped photo's layer stays reserved on its Frame** until its original end, so it is not offered to another Frame meanwhile.

## As built (slice 3: Skip, Previous and Never show)

Central's side of Skip and Previous (a Wall's and a Frame's) and Never show, built 2026-11-25 on `claude/sharp-franklin-ydvhz2` from [Slice 3 design](#slice-3-design). Pause, `cut_at`, Back to normal, a takeover's end, Show now on chosen Frames and the console's slice 3 pages are not built here; neither is the Player's one wait at a cut. Where the build differs from the design, or decides what it left open:

- **Build step 0, the offer backpressure risk, was real** and worse than read: the DB test `tests/test_coordination.py::test_a_long_layer_never_starves_its_players_offers` (one Player, an hour-long turn beside 10 s turns) found the Pi offered nothing more after 950 s, not about 32 minutes. The fix is the one proposed, with two additions: an unexpired offer the acknowledged revision supersedes stays while it holds a commit of a cue that revision holds none of (a cue that ended since), so a cue's group is kept exactly as before; and a "presented" report under a revision cleanup deleted still counts as shown through the first later offer holding its layer. `test_a_pause_never_starves_its_players_offers` waits for Pause.
- **The contract:** `Layer.action` and `stack_order`'s new key are built (Central and the Player; `action` is content identity on the Player). `Layer.cut_at` is not (Pause and Back to normal need it).
- **`central/everyday/`** holds the vocabulary, `stays`, `showing_at`, `joins`, `previous_step`, `cover_for` and `asked_state` (`Hold`, `Pause` and `PauseRefusal` wait for Pause), and adds a closed `EverydayRefusal` (`invalid_operator_skip`, `invalid_never_show`, `unknown_wall`, `unknown_frame`, `unknown_photo`, `nothing-moved`, `changed`) to the console's contract. Its answers are `central/everyday/views.py`.
- **Who is "answering"** is Frame status's judgement (`central/frame_status/policy.py` `everyday_facts`): its Pi heard within the silence limit, its hub link not known lost (Skip needs no hub: the plan reaches the Pi over its session), and its Frame report not saying it plays Central's continuation. The command reads the same facts under Coordination's lock (`PgFrameStatus.everyday_in`), so `can` and the answer agree.
- **Previous's photo test** judges the fit rule's size-only part at the house's crop limit, not the Scene's own. `can` asks only whether the history holds an earlier photo not on the list (the fit judgement reads candidates the Home poll does not), so a Previous `can` offers may be answered `no-previous`; the code is the same closed one.
- **A cover planned late** starts `SKIP_LEAD` after the pass that plans it, as designed. Two Previous presses within one second whose first pass came late can get the same start; the later press is then not certain to be drawn on top (an open risk, below).
- **Never show** is offered while the Frame shows a photo now, whether or not it can move (a Frame that cannot shows it to its end, once, and the answer names it in `stayed`). Its skip row is kept in `skips` with `scope` `photo` (migration 133 allows it). A paused Frame stays (`paused`) until Pause is built; ending its pause there is Pause's. `last_showings` name a showing's `start` and `until` (not `from`).
- **Retention:** `skips` rows a day past their last cover go in the scheduler's `skip-retention` step. Migration 133 also indexes `shown_photos` by Frame for Previous.
- **`FrameStatus.asked`** is the newest action that moved the Frame, reported until its cover ends (never before it can be late).
- **The console** only words the three new `Action` codes and asks the Pi actions alone from a tile's menu (`isPiAction`); its pages are the console's slice 3.
- **Not built from the design's acceptance list:** every Pause, Runtime and `cut_at` test, the Player's `test_a_cover_joining_its_layer_draws_no_seam` (the executor's `test_a_cover_starting_before_its_runs_next_layer_is_drawn_above_it` covers the stacking), and the browser tests. The wall assignment's existing property needs no change (it places no action layer); `test_no_placement_overlaps_an_action_layer` is a property, and walking covers after the other slots is caught by the deterministic `test_a_slot_planned_with_a_cover_never_takes_its_photo_on_another_frame`.
- **Open risk:** a late-planned cover's start ties a later one's; the stacking key could add the request's instant to break the tie (a contract change).

## As built (slice 2)

Added 2026-11-22, when slice 2 was built in [#158](https://github.com/mcurcio/photo-wall/pull/158). The record above is the design as accepted; this section lists every place the build differs from it or settles what it left open. Nothing here is the owner's ruling unless it says so.

### Central's status

- **`Part`'s code for the photo app is `"photo app"`**, with a space, as this record writes it.
- **`can` is a sorted tuple**, not a frozenset, so the document serialises the same every time; the change number depends on it. In slice 2 it carries only `reboot-pi` and `restart-app`.
- **`Problem` has two more fields:** `frame_id` (none for a house problem) and `explains` (the Frames whose problems a house problem replaces). `HouseStatus.problems` is the whole worst-first list Home draws: the house problems, then each Frame problem no house problem explains.
- **A house problem speaks a word for the house.** Hub unreachable and worker stopped are Can't tell; no library answering, a failing scheduler step and an untargeted planning issue are Needs a look. `Problem` does not carry that word, so the console restates the rule in a two-entry map.
- **A fourth house problem:** a planning issue that names no Frame, so the planning rows the Needs attention page held are not lost.
- **"No library connection answers"** is read as: every connection needs a look, whatever its problem.
- **A fourteenth row, before row 10: nothing is to play.** The table has no row for a Frame that is black on purpose with its display on: no Scene targets it now, or its Scene blacks it. Its Player then states no black cause (`player/frame_black.py`), so no verdict ever comes, and row 10 read it as a photo app failure for good. The build adds the fact the row needs, whether a picture is to play on the Frame now (the Runtime's visible Intent is media: `PgStatusReading.wanted`), and a row `nothing-wanted`: the report says no picture is on screen, no verdict and no impediment speak, and nothing is to play, so the Frame is **Resting** ("Nothing to show right now"). Row 10 now asks that a picture is to play. Found in review; the owner has not ruled on it, and design language §6's Resting says it.
- **Row 3's install has a ceiling and starts at the restart.** As accepted, row 3 matched for as long as `devices.install` was set. Built: only the install's `restarting` step counts, for `REBOOT_ALLOWED_SECONDS` from when the step began (`devices.changed_at`, which only the install's own steps write while a Pi has one). While Central is `downloading` the release nothing has happened on the Pi, so the rows below speak, and a Pi that never came back from an install's restart reads Can't tell since it was last heard (row 4, U6). `ProgressStep` has no `install-downloading`.
- **Row 4 stands aside for the Pi's own verdict.** "Heard" is the photo app's session socket, so a dead or frozen app goes silent too. While Central's hub holds the Pi's link and the Pi's verdict says the screen is dark, row 4 does not match and the verdict speaks (rows 6 and 8): a dead app stays "Not showing · photo app · Restart app" past the silence limit, with one `since` and one event. Not linked, or linked with no verdict, is row 4 as before.
- **Row 2 does not match a wall with no hub configured** (hub state `no_hub`). Such a wall falls to row 13 unless its Pis report.
- **Since when.** A kept row's `since` stands while its key (word, setup step, row, problem code) stands. A new key begins at the time its row's own fact states when Central's clock has one (the hub's last change, when the Pi was last heard, the last report), else at now. Home never writes: between ticks it serves the fact's time or the read's time, and the row follows at the next tick, up to 60 s later. A health verdict, a Frame report, a presence change, a hold or an install does not wake the scheduler.
- **Clocks.** Many inputs are stamped by the database's clock. The reading asks that clock for its own now, judges power and holds against it, and hands the policy each such time shifted by the whole-second difference to Central's clock (0 in deployment).
- **Row 3's reboot** is in progress while it is asked and not yet run, or accepted and the host line's birth is not newer than the answer, for at most `REBOOT_ALLOWED_SECONDS` = 600. The ceiling is the builder's number, to tune.
- **O3's limits as built:** row 4's silence limit is `contracts/liveness.py` `SILENT_AFTER_SECONDS` (51 s; what the console read from the inventory until now), exported to the console as a constant. Row 11's is `CONTINUATION_LIMIT_SECONDS` = 3600, compared between the Player's show time and Central's clock.
- **Preparation per Frame** comes from a new `Coordinator.frame_plans_in`: the media layers not yet over in the Player's newest valid offer, and how many of them its readiness for that offer names prepared or failed. "Being prepared" is: some planned, not all prepared, none failed.
- **A Pi's health:** "throttled" is any `_now` flag at its alarm in `contracts`' `HOST_METRIC_BANDS`, looked up in the store; "clock in doubt" is the kernel's `wall_clock` fact. No judge module was added on Central.
- **Row 7** names the two power results as problems in `frame_status` (`DISPLAY_PROBLEMS`); a test holds that set equal to what `why/explain.py`'s `is_resting` refuses.
- **Planning codes:** four source codes (`source_missing`, `source_empty`, `source_not_ready`, `asset_preparation_failed`) map to source trouble (immich). Every other code, an unknown one among them, maps to a planning issue (central) with the Planner's code under Details.
- **Restated vocabularies:** `PlayingKind` restates `central/why/model.py` `ContentKind`, and `HubState` and `InstallStep` are restated in `ports.py`, because those modules' imports reach `central.fleet`. A test holds each pair equal. So the package does not import `central.why` or `central.photo_library`, though the import rule above names them as read.
- **`Override`:** `by` is none for a hold (`HoldSource` records no person yet), and a takeover's `until` is none (no end time until slice 3). The pause kind exists and nothing produces it.
- **The `change` number** is 48 bits of the SHA-256 of the rest of the document: stable across processes and restarts, and safe in a JavaScript number.
- **A second read**, `GET /v1/operator/frames/{frame_id}/status`: the Frame's status and its newest 32 changes from the event log. No console page reads it yet.
- **`Scheduler` takes `status=`** as a required keyword.
- **Not surfaced:** Output interruptions and "No Panel listed at enrolment" have no row in the decision table, so `FrameStatus` does not carry them.
- **Never raised today:** `pi-storage-low` (Central holds no such fact; it is only a boot stage's refusal), and `software-renderer` and `app-resource-exhausted` (the Pi's health line carries a code only for a dark Output). Their rows exist.
- **More modules than the map:** `central/infra/frame_report_store.py` (the Player line's Frame reports), migration 132, and `central/fleet/link_asks.py` (below).
- **Tables:** migration 132 makes Central's 44, 48 with Procrastinate's four ([0034](0034-central-schema-consolidation.md#the-tables-after-each-migration)).

### Restart app, as built

- **The broker had no bus line** ([0034](0034-central-schema-consolidation.md), S4c: "the broker says nothing to Central"). It now declares the `apps` store line that `contracts/node_link.py` already reserved (fleet pipe), using two of its three streams: a record buffer and a state bucket with one key. Wire names are in a new `contracts/node_apps.py`.
- **Progress does not come from `app_started` and `app_exited`.** Those are node-feed facts that never reach Central, and a restart of an already dead app emits no `app_exited`. The broker states the restart's own steps on its line, each naming the request: `app_stopped`, `app_started`, or the failures `not_stopped` and `not_started`.
- **The broker's answers:** `accepted`; `in_flight` for any request while a restart is in flight; `duplicate` for the newest request once served. Only the newest request id is remembered (in the journal, so it survives a broker restart within a boot). An older id sent again after a newer restart would be served again; Central never sends one twice.
- **Edges:** a dead (`down`) run is restarted with no stop asked. A refused stop ends the restart `not_stopped` with nothing started. A start that raises ends it `not_started` and is never retried. A broker that died between the stop and the start never starts for that request.
- **The route** is `POST /v1/operator/node/devices/{id}/app-restarts` with `{"request_id": "<uuid>"}`. Answers: 200 `{"duplicate": bool}`; 409 `node_app_restart_outstanding` while one is queued for the Pi; 403 `node_device_unavailable`; 422 `invalid_operator_app_restart`; 503 `node_app_restart_unavailable` without a queue.
- **It asks for `operate`,** as this record says an everyday action does: the first route that does (`mount_node_routes(operate=…)`). Its progress read and both of Home's reads take `admin`, which asks `read` on a GET. The reboot route is the fleet's own and still asks `admin` (`configure`).
- **A request reached too late is not carried out.** A `RestartApp` job the worker reaches more than `APP_RESTART_SECONDS` after its ask (the worker stopped, its queue stalled) asks no Pi and is recorded not delivered: by then the read has said timed out and nobody is watching. The reboot job has no such check.
- **The progress read** is `GET` on the same address: `{"restart": AppRestartView | null}`, the Pi's newest restart only: its request id, by whom, a state (`in_progress`, `done`, `failed`), a closed failure (`not_delivered`, `in_flight`, `not_stopped`, `not_started`, `timed_out`) and the four steps with their times. Nothing is stored; it is folded on read from the ask and outcome events, the broker's records, the line's state key as a fallback for a lost record, and the Player's report.
- **"Showing"** is the Player's newest Frame reports saying any Frame is showing, recorded at or after "app stopped", and only after "app started". Central keeps only the newest report, so this time moves when the Player reports again.
- **`timed_out`** is judged on read, on the database's clock, 180 s after the request (`APP_RESTART_SECONDS`). No timer exists. Under Night off nothing shows, so a restart that worked reads timed out there.
- **Step times** are when Central recorded them, never the Pi's clock. "App stopped" and "app started" usually land in one batch with one time.
- **`RestartApp` and `RebootPi` are separate job types,** each one pending copy per Pi, so a queued reboot does not block Restart app. What they share moved to `central/fleet/link_asks.py`; `pi_reboot.py` uses it with unchanged behaviour.
- **The restart's address has one home,** `central/fleet/app_restart.py` `APP_RESTARTS_PATH`: the route is mounted at it and `central/home_routes.py` names it for a Frame's `can`.
- **Its views are in the generated contract:** `AppRestartsView`, the closed `AppRestartRefusal` codes, and the two waits the console's progress uses (`APP_RESTART_SECONDS`, `REBOOT_ALLOWED_SECONDS`). The console words every failure and refusal in `domain/restart-words.ts`; a code it does not know is never shown as it is.
- **Frame status does not show a restart's step.** Row 3 knows a reboot and an install, not a Restart app in progress; Home shows that from its own read of the progress.

### The console

- **Home rides the snapshot.** Its document is read with the snapshot read (`homeApi.js`, `useSnapshot.js`) and sits on it as `snapshot.home`: one mechanism gives the 5 s cadence, the read after an action and the write fence. A failed Home read never fails the snapshot. A status Central has not served is drawn as Can't tell, never guessed.
- **Where Home sits.** In `neutralRoutes.jsx`; the sidebar's groups are Home, Frames (holding the Wall), Show, Fleet, Settings. The header's Menu button is gone: on a phone **More** opens the drawer.
- **Other reads stay on Home's list.** After Central's problems Home lists what Central's status does not carry yet: a Pi's host incidents, room lights in doubt, library connections that need a look, and the photo app's readiness reports. A fault Central already names is not repeated. This record's "one list" holds; its "one read" does not yet. Their words are Home's own (`domain/home-reports.ts`): the box is "Pi 3f2a9c", a Frame is its name, and the fleet page's line ("Player …", "Host Management …") is under Details only. They are drawn without "since": the host read keeps no such time.
- **What was asked of a Pi is kept outside the page** (`domain/pi-asks.ts`), so leaving Home and coming back shows the same steps and asks nothing again; a reload of the tab forgets it. A tile says "requested" only while Central has taken the request and the Pi has not finished it: not while a reboot's dialog is open, and not after the request finished, failed or was refused.
- **While Home's read is failing nothing is offered to ask of a Pi,** in a tile's menu or on a card: `can` and a fix are Central's to say now.
- **No Refresh on Home.** The top bar's Refresh is not drawn while Home is shown; the other pages keep it until the push stream.
- **Reboot from Home is confirmed first** (it cannot be taken back and blacks every Frame of the Pi) and goes through the fleet's one send path. Its steps are Requested, Accepted by the Pi, Showing again, the last from Home's statuses with a 600 s wait. Restart app is not confirmed.
- **A fix of "open Central" draws no button.** The console has no page for Central's own health, so the hub, worker, scheduler and continuation problems show a card without an action.
- **Takeovers are served and not drawn** until slice 3; only power holds are.
- **Tile actions.** The menu holds Restart app and Reboot the Pi when `can` has them, then Why is this playing? and Open Frame; both links open the Frame page. Skip, Previous, Pause and Never show are slice 3. The button is named "More actions for <Frame>".
- **`WallMap`** draws to scale from 640 px of its own width and falls back to tiles side by side when a Frame's size is unknown, two Frames overlap, or the smallest Frame would be under 160 × 112 px.
- **A stale tile is sunk and dashed** with "Can't tell since …", not greyed: fainter text failed contrast.
- **Empty** leads to the Wall ("Add your first Frame on the Wall"), not the Setup checklist, which does not exist yet.
- **Icons are our own inline paths** in `ui/icon.tsx`; `@mdi/js`, the design language's earlier recommendation, was not added.
- **The phone switch is one token,** 768 px (`--breakpoint-md`, `PHONE_BELOW_PX`); the shell and the flow footer used 850 px before. A new `--spacing-tabs` lifts a page's sticky bar above the 56 px tabs. **Show now**'s quick action is hidden on a phone because a tab carries it.
- **Words.** The setup chip says "Needs a Pi". A Pi is named "Pi <last six of its serial>". "Since" and "until" are said to the minute. The photo app's readiness reports name the photo app and the Pi. Refusal words passed through the Reboot dialog still say "Player". The Pi's own Central card now ends "see Home in Photo Wall" (`central/room_light/policy.py`), since the Needs attention page it named is gone.
- **Pattern changes:** `AckBadge` gained optional `words`; `OverrideBanner`'s `kind` is required; `Menu` takes link entries and opens as a sheet on a phone. The catalogue's members are the [design language's](../design-language.md#members-for-home-3b-i-slice-2).
- **Stories built beyond this record's list:** WallTile/Stale; WallMap/NotToScale, /Loading, /Phone; StepProgress/Finished, /Phone; Home/Empty, /NoAnswer, /CantTell, /Held, /WontShow, /RestartInProgress. WallMap/TwoWalls, the OverrideBanner kinds and the Home stories this record names are built.
- **Not built in slice 2:** the Frame page's recent problems (G2); Restart app on the Pi page (G3); stories for the Reboot dialog, "Not restarted", "Not back" and Home's Pi-fault and readiness cards (the browser tests hold them; the catalogue does not draw them).

### Review findings left as they are

Recorded so they are not lost; none is the owner's ruling.

- **A display wanted on that did not answer the power-on** (or shows another input) reads Showing: rows 7 and 11 read the power result only while the display is wanted off, and the table has no row for it. Many displays never answer a power command, so a row needs the owner's word.
- **After a Central start that did not close its sockets,** each photo app's "last heard" is when its socket last opened, so until it reconnects row 4 can say "Can't tell since" a time days ago, and the first tick records it.
- **Two targets on Home are under 44 px on a phone:** the house word in the top bar and a problem card's fix when it is a link.
- **Outside Home, three legacy places colour a Frame's status by a `health--<severity>` class on text** (the Wall plan's tile, the target picker, the Now badges) rather than by the one chip. They read Central's status; only the drawing is old.
- **Home's Pi-fault and readiness cards** are drawn as setup cards, without "since" and "what Photo Wall is doing".

## Verified, and not

Added 2026-11-22, rewritten when slice 2 was reviewed and integrated. What ran, on a development machine with a throwaway PostgreSQL 16 and no Docker, on the tree as committed. CI has not run on it.

**Passed:**

- Static: `ruff check .`; `lint-imports` (39 contracts kept, "Frame status is pure" among them); `scripts.console_types --check`; `scripts/check_docs.py` (151 documents).
- The console: `npm run typecheck`, `lint`, `build` and `build-storybook`.
- The unit tier: 6556 passed, 141 skipped, 1 failed (below). In it: the decision table, the cause table and the house word (`tests/test_frame_status_policy.py`, 39), the broker's side of Restart app (`tests/node/apps/test_app_restart.py`, 26), and the console's model tests (`tests/test_console_*.py`, 218), the ones that pinned `frameHealth`, `wallAttention` and `attention.js` rewritten against `domain/frame-status.ts`.
- The DB tier, with the pinned nats-server: 1243 passed, 0 failed, 29 skipped (12 need the Immich fixture, 9 Keycloak, 5 the PID1 fixture, the rest an interactive fixture or another missing service). In it: since when and the event (`tests/test_frame_status_store.py`, with a Frame no Scene targets read through the real stores), Home's routes, Restart app through Central's routes (`tests/test_app_restart.py`), an install's step and its beginning (`tests/test_player_versions.py`).
- The Node bus tier (`tests/integration -k node_bus`): 63 passed and 1 existing strict xfail; the restart method across a real nats-server pair is among them.
- The browser tier but the catalogue file: 496 passed, 0 failed. `tests/browser/test_home_browser.py` holds 17: the six words and setup, the house word and counts, a 390-pixel phone, each fixture fault naming its part, the hub's house problem, menus following `can`, Restart app step by step, a restart that fails, one Central refuses, one left for another page and found in progress on coming back, Reboot the Pi (nothing asked before the dialog is confirmed, then its three steps) and one the Pi did not take, a failed Home read, a fault during intended darkness ("Won't show …"), now and next with a Frame nothing plays on, and landing on Home in token and provider mode.
- Mutation probes, each failing tests and then restored. Central: rows 11 and 12 swapped, rows 3 and 4 swapped, a cause table row dropped, a write on every tick; the three review fixes undone together (row 4 not standing aside, an install with no ceiling, row 10 without "is a picture to play") fail 4 policy tests. Restart app: a second restart, a start after a later death, an answer always `accepted`, a showing report counted before "app stopped", the pending-copy check dropped. The console, one build with eight changes (any restart read as this request's, no "Not restarted" card, a failed Home read drawn as current on the page and in the top bar, "Won't show" dropped, the now/next strip dropped, actions offered from a stale document, "requested" whatever the request's state): 8 of the 17 Home browser tests fail.

**Failed, and why:**

- `tests/test_build_netboot_bundle.py::test_the_bundle_initrd_is_the_floors_layer_then_the_cached_initrd`: `cpio: not found`. The machine has no `cpio` and none can be installed here; the test does not touch this work.

**NOT RUN:**

- **Bench: Restart app on the test Pi, watched to "showing". NOT RUN.** No Pi was used. Not proven: that PID1 stops and starts the Player's unit and collects the old one; that the broker's sandbox reaches the bus and it stays inside its memory and task limits with the bus client loaded; that the bus's memory fence holds with the `apps` line; that Display hands the Output to the new run and the old planes are released; that the health judge shows no card during a normal restart and withdraws `app_absent` at the new start; the real time to showing and whether 180 s is right; that "showing" does not wait for a second report when the first reaches Central before the "app stopped" record; a restart while Weston restarts or with the unit failed; the real Debian package importing `nodeapi` on the Pi.
- The catalogue walker (`tests/browser/test_console_catalog_browser.py`): not run here (its screenshot baselines are CI's image's). The new and changed stories have no baselines yet, so that leg fails in CI until they are fetched.
- Node packages (`tests/debs`, the build's import check); the PID1 scenarios, which now run a broker with an `apps` session; the arm64 bus memory fence; image smoke; the wall e2e. Each needs Docker or CI.
- A real hub or a real Pi under Central's status: the hub-unreachable, worker-stopped and linked or unlinked paths are held by the pure policy tests and by browser tests over Central's own stores, never a live hub. An install's restart on the Frame's status is held in the policy tests, and its step's time in the store's own test.
- A restart that times out (180 s) and a reboot past its 600 s: built, not exercised. Home's 304 is seen against the real Central in the landing test.

**Still open from the design:** slice 3, whose design pass is now [Slice 3 design](#slice-3-design) (2026-11-25). The bench debt the roadmap records (1b, 1c and 2a's steps marked NOT RUN) stands: Home reports on power behaviour that has not been seen on a panel.
