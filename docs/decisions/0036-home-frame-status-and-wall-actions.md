# 0036 — Home: a Frame's status, the house word and the wall's everyday actions

**Date:** 2026-11-22 · **Layer:** system (Central's read of a Frame's live health; four new everyday commands; the console's landing page and phone layout) · **Status:** accepted design (owner, 2026-11-22: "I'll take your recommendations. Proceed"), not built. It is slices 2 and 3 of [roadmap 3b-i](../roadmap.md#3b-i-home-and-phone); slice 1 is [0035](0035-operator-sign-in-through-an-identity-provider.md). **Builds on:** the six status words ([design language §6](../design-language.md#6-status-and-severity)), the health verdict (issue #126), the power timetable ([0021](0021-power-timetable-and-repeating-schedules.md)), the wall plan ([Planner](../module-planner.md#the-wall-plan)) and [0032](0032-the-player-continues-centrals-plan.md).

**Decision owner:** the owner. His words below are his; everything not quoted from him is a design choice, not his ruling.

## The owner's words

| # | Words | Source | Kind |
|---|---|---|---|
| Q4 | Pause: "Freeze the photo": the Frame holds the photo it shows for the Pause length, then carries on by itself | owner, 2026-11-22 (question card) | a pick, revisable |
| R1 | "I'll take your recommendations. Proceed": every default of this record's open questions and findings stands as the owner's pick | owner, 2026-11-22 (chat) | a pick, revisable |
| — | "Proceed" (to this design, after the recommendation of 3b-i as the next delivery) | owner, 2026-11-22 (chat) | guidance |
| — | "No auto-restarts at all" | owner, issue #125 | guidance, bearing on [Restart app](#restart-app) |
| — | "Amend U1" (intended darkness reads Resting) | owner, 2026-10-12 | a ruling, bearing on [the decision table](#the-decision-table) |

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
- **`can` is how "not offered" works.** Skip is absent from `can` on a secured assignment, so the console never draws a button it would have to refuse ([design §12](../operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)).
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
| Skip, Previous | `POST frames/{id}/skips` | Coordination and the Planner | none (Previous) |
| Never show | `POST never-show`, `DELETE never-show/{photo}` | a new `never_show` list, read by the wall plan | Undo, **Show again** |
| Pause | `POST pauses`, `DELETE pauses/{id}` | a new pause, beside the power hold | **Carry on** |
| Show now | `POST activations` (extended) | the Runtime | **Back to normal** |
| Restart app | `POST node/devices/{id}/app-restarts` | a job to the Pi's app broker | none |

### Skip and Previous

- **Skip** ends the Frame's current photo now and starts the next in its Scene's line. **Previous** puts the last photo the Frame reported shown (shown history) next in line.
- It is refused `secured` when the Frame's current layer is secured; `can` already left it out. On an offered or unsecured layer it acts at once.
- The command is a fact Coordination records under its lock; the next planning pass ends that Frame's current layer at the command's time and re-plans the Frame. The never-on-two-Frames rule is the plan's and holds unchanged.
- It is **acknowledged**, not optimistic: the tile shows "requested" until the Player reports the new layer presented.
- A Frame playing Central's continuation ([0032](0032-the-player-continues-centrals-plan.md)) is out of Central's reach: Skip is absent from `can`.

### Never show

- A wall-only list. Nothing is written to Immich ([design §12](../operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)).
- New table `never_show`: the photo (connection and Immich asset id), when, by whom. Pure rules in `central/wall_plan/` beside shown history, behind a port; the wall assignment treats a listed photo as not eligible, and an empty pool does not relax that.
- Adding a photo that is on a Frame now also skips it there when Skip is allowed. On a secured layer it stays until that Run ends; the toast says so.
- Optimistic, with Undo for 10 s. **Scenes › Never show list** shows every listed photo with **Show again**.

### Pause

The owner's pick: the Frame holds the photo it shows.

- A pause has the power hold's shape: `Pause{pause_id, frame_ids, source, started_at, ends_at}`. Default length 1 hour (setting #86). The hold and the pause share one `source` type, the 0035 actor, so "Paused by Matt until 22:04" needs no second vocabulary. `HoldSource.kind`'s `console` becomes that actor too.
- The Planner extends the Frame's current layer to the pause's end. The photo's time on screen grows with it, so no other Frame gets that photo meanwhile.
- The schedule beneath keeps advancing. **Carry on**, or the pause's end, reveals its current state, like any covered Run.
- Power wins: Night off during a pause turns the display off, and the pause still ends at its time.
- **Finding F3: a paused Frame is not Resting.** The design language defines Resting as "Dark on purpose: Night off, paused, all off", the console design says "A paused Frame counts as Resting", and U1 lists Pause among intended darkness. All three were written before Pause had a behaviour. A Frame holding a lit photo is not dark, and a Frame that reads Resting while it glows would be the console claiming something false. This design words it **Showing**, with "Paused until 22:04 · **Carry on**" beneath. The three sentences change in the build's pull request; U1's is the owner's to approve.

### Show now on chosen Frames, with an end

- `ActivationRequest` gains `frame_ids` (explicit; the sheet pre-selects every current Frame) and `ends` (in 1 hour, at tonight's Night off, or until stopped). Today's `expires_at` keeps its one meaning, a queued request's expiry.
- The Run it starts covers only those Frames and stops at `ends`. **Back to normal** is the existing `runs/{id}/finish`: natural completion, which reveals the schedule's current state.
- Default length and Frames are settings #84 and #85, on the sheet.
- **Risk:** a Run over a subset of a Scene's Frames is new to the Runtime. Every chosen Frame still participates explicitly; a Frame not chosen receives no implied command. The Runtime's part is designed when slice 3 starts.

### Restart app

Roadmap G3 asks for "Restart app with progress". Issue #125 removed every automatic restart: three causes start a run, and "a run that dies stays `down` until a person reboots".

- A person pressing **Restart app** is not an automatic restart. This design adds a fourth `StartCause`, `asked_by_a_person`, reached by one new method on the app broker's bus line, called by a Central job as `RebootPi` is.
- The broker stops the current run and starts one. It never repeats the request by itself; a run that dies again stays down.
- Progress is `StepProgress`: requested → app stopped → app started → showing, each with its time, from the broker's existing `app_started` and `app_exited` events.
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
| `central/pause/` (pure), `infra/pause_store.py` | The pause, its timetable for the Planner |
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
- One `WallMap` per Wall, Frames to scale, read-only. Each `WallTile`: the Frame's name, its `StatusChip`, what it is doing in one line ("Evenings · until 23:00"), and an actions `Menu`: Skip, Previous, Pause, Never show this photo, Why is this playing?, Open Frame. A tap on the tile opens the Frame page.
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
- `tests/test_skip_never_show.py` (DB, the roadmap's name): Skip and Never show act on unsecured assignments; on a secured one Skip is absent from `can` and refused if sent, and Never show starts with the next Run; a listed photo is never assigned again, across re-plans (a property test beside the wall assignment's).
- `tests/test_pause.py` (DB, controllable clock): the photo stays to the pause's end and no other Frame gets it; Carry on and the end both reveal the schedule's current state; Night off wins; a double tap is one pause.
- `tests/browser/test_show_now_browser.py` extended: Frames and an end time; Back to normal reveals the schedule's current state.
- `tests/node/apps/test_app_restart.py`: the fourth cause starts exactly one run; a run that dies again stays down.
- The console's import scan: nothing in `src/` computes a status word outside `domain/frame-status.ts`.

## Slices

| Slice | Holds | Size |
|---|---|---|
| 1 | Sign-in ([0035](0035-operator-sign-in-through-an-identity-provider.md)) | one pull request |
| 2 | Frame status in Central; Home, read-only; `AppShell` and the phone layout; Restart app | one pull request |
| 3 | Skip and Previous, Never show, Pause, Show now on Frames with an end | one pull request, after a design pass on the Runtime's and Planner's part |

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

## Not verified

Nothing here has run. The Planner's and Runtime's part of Skip, Pause and a Run on chosen Frames is stated as a contract; how each fits the hour-ahead wall plan and its locked layers was not traced through the code, and is slice 3's design pass. The bench debt the roadmap records (1b, 1c and 2a's steps marked NOT RUN) stands: Home will report on power behaviour that has not been seen on a panel.
