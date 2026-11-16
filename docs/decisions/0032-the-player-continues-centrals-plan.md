# 0032 — The Player continues Central's plan, and time never gates a photo

**Date:** 2026-11-15 · **Layer:** system (contracts, Central's Planner, wall plan and coordination, the Player, the Node's host facts) · **Status:** accepted design, built on the branch `feat/player-standalone`, on top of [0031](0031-show-clock-and-late-join.md)'s branch.

**Amends:** [0031](0031-show-clock-and-late-join.md): the show clock's 100 ms health threshold is gone (a slow answer is no longer thrown away; no answer for about 16 minutes no longer refuses every start), and with it the `clock` readiness code and the `clock_probe` fault; the time request no longer comes before the plan. **Records as built:** [0017](0017-node-redesign-r3.md) C2 ("the Planner may author 'repeat until E'"): a plan's continuations are that authored repetition.

## What happened

On 2026-11-15 Central's database took 3 to 7 s to commit, and for about a day Central answered the Pis `lock_busy` or 503. The Pi's wall clock was right, yet the Frames froze on one photo, because three things each needed a fresh answer from Central before a photo could change:

1. **The show clock.** A Central time answer slower than 0.2 s was thrown away, and with no usable answer for about 16 minutes the clock read "unhealthy"; the executor's clock gate then refused every start, coded readiness `clock`, and blamed the black on time.
2. **The start grant.** Every photo waited for Central's Commit, and Central refused one unless the Pi's report was younger than 2 s by Central's own receipt and the Pi's clock bound was within 100 ms.
3. **The plan's reach.** Layers reach about 5 minutes ahead; past them the Frame kept its last photo. One failed poll also ended the Player's whole session and cleared its downloads.

## The owner's words

| # | Words | Source | Kind |
|---|---|---|---|
| G1 | "The cluster that central runs on is unreliable right now. Them player should fallback to a standalone mode when central is down" | owner, 2026-11-15 | guidance |
| G2 | "The pi should keep its own wall clock time following NTP, and then take show sync time from central" | owner, 2026-11-15 | guidance |
| Q1 | Picked: the Frame cycles "its own next photos" from Central's plan, "as many as it has downloaded, at the Scene's pace, never a photo another Frame is using" | owner, 2026-11-15 | guidance (a pick, revisable) |
| G5 | "Never show the same photo on two frames at the same time" | owner, 2026-10-11 | owner statement |
| G6 | A card only "when the issue induces blackness on the frame"; a Player still in control "isn't an 'error' state" | owner, 2026-10-27, 2026-10-24 | guidance |
| G7 | A change happens on "all pi's together, by issuing a new preparation state machine invocation across the involved fleet" | owner, 2026-10-24 | guidance |

## Decision

Three design rules (design choices, not requirements):

1. **Wall time and show time each have one judge; neither is a gate.** Wall time is the Pi's own: systemd-timesyncd follows the router's DHCP-advertised servers, then the Debian pool, and Central is never an input. The kernel judges it (`adjtimex`), reported as the host fact `wall_clock` (`synchronised` or `unsynchronised`, [`appliance/kernel/clock.py`](../../appliance/kernel/clock.py) `wall_clock_state`); nothing gates on it. Show time is Central's clock carried on the Pi's monotonic clock ([`player/show_clock.py`](../../player/show_clock.py)): **any answer anchors**, the state the Pi already polls included (it carries Central's time, `State.server_time`), with half its round trip as its bound. A 6 s answer anchors at ±3 s, later good ones tighten it, a consistent one never loosens it, and one that disagrees beyond it re-anchors. The bound travels in every readiness report.
2. **What a Frame shows at a time has one model.** The plan's layers, then its **continuations**: per Output, one per Run on top of that Frame past its layers (`Plan.continuations`: the Run's listed photos, as references to this plan's own layers so each carries its copy and crop; the Scene's pace and Transition; its first turn; `until`, the next change Central knows on that Frame). Slot k shows the k-th listed photo the Pi holds, cycling, as exact layers from one pure generator Central and the Pi both call ([`contracts/continuation.py`](../../contracts/continuation.py)); skipping a photo not held is the authored rule, not the Pi's choice (0017 C1). There is no mode to enter or leave: a continuation starts where the Frame's layers end and never covers what the plan covers. With a new plan the Frame keeps what it draws until a layer of the new plan on that Output can start, then shows it with its Transition: rejoining needs no event and shows no black.
3. **A grant only for a change.** Central marks `Layer.awaits_grant` on a Run's first turn alone (its Intent's `cycle_index` 0), and only such a layer belongs to a cue (G7: a change on all Pis together). The Pi reads it, never re-derives it. Every other layer, and every continuation slot, starts at its instant once secured and prepared. A Commit's freshness is the readiness sequence it echoes, which the Pi checks; Central no longer refuses a grant on a report's age or the Pi's clock bound.

**Never a photo another Frame uses** has one owner, Central's wall plan ([`central/wall_plan/continuation.py`](../../central/wall_plan/continuation.py)):

- **Lists.** A pass builds every Frame's lists together, so no photo is on two lists and none overlaps another Frame's showing over the list's span.
- **Reservations.** Every listed photo of every stored offer at or past the revision the Player last reported stays reserved on its Frame until its `until`, so a later pass never gives it to another Frame while that Pi may be cycling it, partial outages included. An expired offer is kept while it may still hold. A listed photo whose layer keeps it after its end (`keep_this_photo`) may stay on its Frame past `until` until something replaces it, so its reservation and its list span are open-ended.
- **Carried photos.** A photo a Pi still draws or keeps that its current plan does not place there (a rejoin, a kept still) travels in every readiness report (`Readiness.carried`) and is reserved while reported.
- **Per-Frame guard.** Each Frame's hand-over guard is twice the larger of the configured bound and the bound its Player reported, grown by the drift rate (100 ppm) for the time since Central received that report (a cut-off Pi free-runs, and its last report says nothing of it), capped at 30 s (`MAX_GUARD_UNCERTAINTY`), so a loose clock costs photo moves between Frames, never a start.

**The rest of the class on the Pi.** A failed request never ends the Player's session (only a refused registration, a redirect or a request that never left its pool does), and downloads of the held plan's photos continue while Central answers any request; a session's end never empties them. Observations of an earlier revision are sent, and Central records a continuation slot's `presented` against its source photo in the shown history. **The card** shows only when a Frame goes black while content is wanted (it holds no listed photo); a Frame playing its continuation is no error (G6). The Pi states it on its own Player line, as each Frame's `continuing_since` in the `frames` key (contracts/node_state.py), worded for people "playing Central's continuation since 14:02"; health keeps one writer.

The contract changes (E1 to E6) are in the [execution contract](../execution-contract.md): [time](../execution-contract.md#time-and-current-state-reveal), [readiness](../execution-contract.md#readiness-and-commitment), [cues](../execution-contract.md#a-cues-two-phases), [failure behavior](../execution-contract.md#failure-behavior), [what a Frame keeps](../execution-contract.md#what-a-frame-keeps) and [shown history](../execution-contract.md#what-a-frame-shows-its-crop-and-when-a-photo-is-on-screen).

## What it gives up, costs and defers

- **Central and the Pis ship together.** Contracts refuse unknown fields: every state answer now carries Central's time (`State.server_time`), so a Pi on the old release refuses every state answer from the new Central (and with it every plan, reboot command and release switch) and keeps showing what it holds until it is rebooted. No second, old-shaped answer is sent (no backward compatibility, as [0022](0022-picture-hardware-and-software-apart.md) R7, 0029 and 0030 deployed). The Galleria integration's contract copy changes with the `frames` key's new field, so Galleria updates with the release.
- **Shown history after an outage is approximate.** A continuation slot's `presented` is recorded against its source photo's own planned window, once (the history keeps one row per assignment), so later cycles of the same photo during an outage are not recorded and the recently-shown window may not include them: after rejoining, the Planner can repeat a photo the Frame just cycled.
- **The guard's cap.** A Pi cut off for more than about three and a half days has drifted past what the 30 s cap covers; two Frames on different Pis whose continuations list the same photo at different times could then overlap on screen by the drift beyond it.
- **Repeats during a long outage.** A list is the plan's photos of that Run on that Frame, about its 5-minute reach (about 100 photos at 3 s, 30 at 10 s), so a long outage repeats every few minutes (`CoordinationLimits.horizon_seconds` sets the reach).
- **Photos held off other Frames.** Listed photos stay off other Frames while their Pi may cycle them: 5 to 10 minutes of photos per Frame, indefinitely for a Pi switched off but still bound.
- **A Run's first turn still waits for a Commit.** With Central silent, the Frame keeps its continuation at that change; a change past the reach is not followed while cut off.
- **Without NTP** a cut-off Pi's monotonic clock drifts up to 100 ppm (about 8 s a day between two Pis): show sync loosens, photos still change.
- **Deferred:** video sync correction while freewheeling; following Schedule changes made while cut off. **Not planned:** playing through a reboot with Central down (diskless boot needs Central, [0016](0016-central-and-node-relationship.md)).
- **Out of scope, named:** the readiness and observation flood, Central's `lock_busy` under slow commits, the time route authenticating through the database, and a kept picture surviving a new authority epoch.

## Deploying

- **Deploy Central, then reboot the Pis.** Until a Pi runs the new release it refuses every state answer from the new Central and keeps what it holds (its last plan, then its kept photo), and a reboot is the only way a Pi changes release; it cannot hear a reboot command from Central meanwhile, so reboot each Pi by power or over SSH. Nothing is rebooted automatically.
- **No iac bump.** The kernel, initrd and boot files are unchanged.
- **Galleria:** update the integration with the release, so it reads the `frames` key's `continuing_since`.
- **Bench, before merge:** the [render bench](../runbook.md#the-render-bench-and-pi-testing-before-merge) on the test Pi (the Player's drawing changed: continuation slots and the rejoin hold), and Central stopped for longer than the plan's reach. Neither has an automated tier beyond the DB-tier tracer (`tests/test_player_standalone_tracer.py`).

## Considered and not taken

- **The link decides.** The Player notices silence for N seconds and loops what it holds: a timer, a mode driven by the link (the bug class itself), flapping when Central is slow, and the Pi inventing content against 0017 C1.
- **A separate per-Frame rule** (draft 1): a second "what shows at t" beside the layers, so two deciders for the mode, grants, on-screen photos, the after-state and shown history.
- **Deleting grants.** G7 asks for a change on all Pis together; the slow-Central freeze the grants caused is removed by dropping Central's clock and receipt-age refusals and granting only a Run's first turn.
- **An hour-long list.** Central would prepare an hour of copies per Frame (`central/wall_plan/assignment.py`).

## Amendment: one place where the picture changes (2026-11-15)

Owner, 2026-11-15 (general guidance): "There *should* only be one place where the photo can change. If there is more than one, then there is an architectural issue." The rejoin hold drew a superseded composition still, with every layer at full strength, so a Frame caught mid-dissolve was cut to the incoming photo when the hold began, and the kept photo was stored at an alpha of its own. Both now go through the seam rule ([player/look.py](../../player/look.py), [plane renderer](../module-plane-renderer.md#composition-and-strengths)): the hold is the superseded composition kept past its layers' ends (`held`: a layer whose fade-out has not begun keeps its picture, a dissolve or fade-out under way runs on as drawn), looked by `look_at` and `look` like every composition, so holding never changes the picture; the kept photo is looked by `look_at` when the Frame falls back to it. The candidate's black ground (a new grant the shell has not admitted, which the shell holds the screen with) stays the one exception: it is the shell's, not a picture the Player chooses.

The hand-over from a held composition to the new plan's layer of another photo goes through it too (`handed_over`, [player/executor.py](../../player/executor.py) `_hand_over`): the new plan's composition carries the held layers beneath its own until its first opaque layer is faded in, so the new photo dissolves in over the held one, not over the Frame's dark plane, and rejoining shows no black as the Decision says. A held layer is drawn and kept resident while it is, never prepared, started or observed as the new plan's (it stays carried while drawn); once the new layer hides it, it leaves the composition and its picture is released. A new layer that never hides what lies beneath (a PNG, or under full opacity) fades the held picture out over its own fade-in instead.
