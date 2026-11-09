# 0031 — The show's clock, late join, and a Frame that says why it is black

**Date:** 2026-11-08 · **Layer:** system (contracts, Central's coordination and Planner, the Player, the health judge, the base image) · **Status:** accepted design, built on the branch `fix/black-frame-after-outage`.

**Amends:** [0009](0009-minimal-base-and-app-package.md) and [0014](0014-reaching-central-from-every-boot-stage.md) R6 (stage 1 is no longer the only thing that steps the clock), and [0017](0017-node-redesign-r3.md) C7 (the base's time client is systemd-timesyncd, not chrony; the freewheel is built, the correction curve is not) and C17 (late join is each Frame's own).

## What happened

On 2026-11-08 the test Pi lost power while the internet was down and came back black. Three separate causes held it black, and the owner asked for all three to be closed ("build them. send me a PR"):

1. The Pi booted with no time source reachable, so its clock stayed at the build's floor, 11 days behind. Stage 1 takes one attempt at the time and the base ran no time client, so nothing ever tried again. The Player compared Central's `issued_at` with that wall clock and refused every plan.
2. After a reboot fixed the clock, the Player became ready 9 s before its group's deadline and missed it. Central skipped the group `not_ready`, a skip was terminal, and nothing was tried until the next cycle, an hour later.
3. All that time the Player drew its dark fallback. The shell counts any plane with alpha above 0 as a held Output, so the health judge raised no card, and the owner saw a black screen with no explanation.

## Requirements (bind)

| # | Requirement | Source |
|---|---|---|
| R1 | "its entire purpose is to show up when a problem occurs so i never have to ask the question 'why is the screen black when i expected it to be on?'" | owner, 2026-10-20 |
| R2 | "a human. dont do automated reboots yet. i want to see every time it fails for now": nothing here restarts or reboots anything on its own | owner, 2026-10-20 |
| R3 | A card shows "when the issue induces blackness on the frame" | owner, 2026-10-27 ([0028](0028-room-light.md#centrals-doubt-travels-2026-10-27)) |
| R4 | "It's all event driven": no timer or poll stands in for an event | owner, 2026-10-24 |
| R5 | Clocks compare only to themselves; order and freshness across systems use sequence numbers, nonces or vector clocks | owner guidance, recorded in the project's memory ("Never compare clocks across systems") |
| R6 | OS dependencies come from rpi-image-gen layers, packages from debhelper | owner, 2026-10-09 ("Packaging via standard tools") |

## Decision

**The Pi's wall clock recovers by itself.** The base's `photo-wall-os` layer requires rpi-image-gen's own `systemd-timesyncd` layer and points it at stage 1's sources: the DHCP option-42 servers the kernel published (written by `/usr/libexec/photo-wall-time-servers` at the unit's start, never failing it), then `debian.pool.ntp.org`. timesyncd steps a large offset untuned and retries a host about every 30 s, so a Pi that booted with no time source has the right time about 30 s after one can be reached. Chrony was not chosen: timesyncd steps a large offset untuned, which is the whole job here. Upstream's unit carries `Restart=always` and a watchdog, which would restart something on its own (R2), so the layer's drop-in sets `Restart=no` and `WatchdogSec=0`. A dead time client costs no picture, because shows no longer read the wall clock (below). Its memory sits in the kernel's line of the memory table ([`appliance/kernel/capacity.py`](../../appliance/kernel/capacity.py)), with PID1, journald and udev.

**The show's timeline runs on the Player's monotonic clock.** `player/show_clock.py` `ShowClock` carries Central's reading forward: `now = central_at_anchor + (mono_now - mono_at_anchor)`. Only the Pi's monotonic clock is subtracted from itself (R5). The `issued_at` check is deleted: plan order is the revision within an epoch, a grant's freshness the readiness sequence it echoes. Staleness is the NTP dispersion rule (RFC 5905): the bound at the last sample plus `DRIFT_RATE` (100 ppm) times its age; healthy while at most 100 ms, about 16 minutes. A sample too slow to measure is counted and the anchor stands, an impossible one changes nothing, a better one renews the bound, and one that disagrees beyond it re-anchors once. There is no forget: a mismatched sample never drops a picture. Central's side reads the Player's report by `received_at` and the readiness sequence it echoes. The scope of R5 is the show timeline: the display power timetable still compares Central's set times with the Pi's wall clock ([0021](0021-power-timetable-and-repeating-schedules.md)), which the time client keeps right.

**Each cue starts in two phases.** Before its deadline its grant is whole, so exact multi-Frame starts stand wherever they can be kept (a member that loses readiness after the Commit and before the start loses its own grant alone and rejoins when ready). From its deadline to its end each member joins alone when its own Player is ready, with the ordinary Commit. A skip means only superseded; a skipped cue that returns to the timeline gets a fresh cohort. Event driven: `readiness()` already runs the commit pass on each report ([`central/cue_start.py`](../../central/cue_start.py), [coordination](../module-coordination.md#cue-start-and-late-join)).

**The Plan says where a photo was wanted and none was placed, and the Player says why a Frame is black.** `Plan.unfilled` (`UnfilledSpan`) is derived by the Planner as wanted minus layers, in one place ([Planner](../module-planner.md#the-wall-plan)). The Player's rule `player/frame_black.py` names one of six causes (no plan, no time, the photo has not arrived, the photo could not be drawn, waiting for Central to start it, no photo for this Frame) and says nothing when the Frame shows something, a pattern or commissioning grid is its own drawing, a black layer is in force or nothing is wanted: Night off, holds, a Scene black on purpose, a kept photo and Central's room-light card are never a statement. The statement travels on the progress probe channel the judge already reads (`frames_black`, a fact `app_frames_black`), on change and at each channel open, never periodically (R4). The judge keeps the health feed's independence from the bus (the owner's 2026-10-20 condition that the overlay always render), and raises one catalogue row per cause after 120 s (`FIRST_PHOTO_WINDOW_MS`, shared with `nothing_to_show`; no measured healthy boot exists to argue for another number). It reuses the card, the overlay, the shell and the health line, adds no hold evidence and no restart path.

## What it gives up, costs and defers

- **No time source, no wall clock.** While no NTP source is reachable the Pi's wall clock stays at the floor: photos play, the power timetable holds at Central's last word, the card's "since" is wrong, and a TLS certificate newer than the floor still stops boot. Offering the router's time server over DHCP (option 42, honoured) closes this. Central-served time stays deferred ([0014](0014-reaching-central-from-every-boot-stage.md)).
- **Amends** 0009 and 0014 R6 ("only stage 1 steps the clock"): stage 1 keeps its floor and its one step. A disciplined clock clears the kernel's unsynchronised state, so the kernel may start copying time to the Pi 5's RTC, which 0014 deferred; harmless, since stage 1 reads the RTC only as a hint against the floor. timesyncd could step below the floor if a server lied; the floor is guaranteed in stage 1 only. With `Restart=no` a crashed time client stays down until reboot, and only the wall clock suffers.
- **A laggard no longer holds its siblings.** A multi-Frame cue starts without a Frame that is not ready, and that Frame joins later: its start is not exact, only its position. A Frame may start partway through its photo. Cues already skipped `not_ready` at deploy get a fresh cohort on the first pass after deploy. No migration. The docs line "Failure at a normal intended start skips that cue" is gone; it had no owner quote behind it.
- **A video may jump once** when the clock re-anchors. The correction curve of C7 is not built: stills need none.
- **Two minutes of unexplained black** before the card, against the hour this replaces. A change is seen up to one probe period (2 s) late, on the probe answer the Player already makes: what makes a Frame black includes time passing (a layer becoming due, a plan expiring), which no event announces, so the report adds no timer of its own and goes on change only. A report lost in a feed gap with no channel change stands until the next change.
- **New wire:** `Plan.unfilled` is omitted when empty, but contracts refuse unknown fields, so a Player on the old release refuses a plan naming an unfilled span until it is rebooted. A new Player on an older base broker ends its probe channel and retries until a reboot aligns them.
- **Deferred:** the `fallback` observation flood and `lock_busy` responses, found on the same night and out of scope.

## Deploying

- **Deploy Central, then reboot the Pis.** Until a Pi runs the new release it refuses a plan that names an unfilled span, and a reboot is the only way a Pi changes release: the gap the owner accepted for the same kind of change in [0022](0022-picture-hardware-and-software-apart.md) R7, with no second path for the old wire. Nothing is rebooted automatically (R2).
- **No iac bump.** The kernel, initrd and boot files are unchanged; the time client is in the base root, which a release carries.
- **Bench, before merge:** the [render bench](../runbook.md#the-render-bench-and-pi-testing-before-merge) on the test Pi (the Player's render path changed), and a real power cut with the internet down. Neither has an automated tier; timesyncd's behaviour is proven on the bench only.

## Considered and not taken

- **Chrony** as the time client (see above).
- **A reboot or a Player restart when the clock was wrong** (R2).
- **Stepping the Pi's wall clock from Central's time** (a second time source and R5's cross-system comparison, for a clock the show no longer reads).
- **A periodic restatement of the black Frames** (a timer where an event, a change or a channel open, exists; R4).
- **The judge reading the bus** for the Player's statement (it would pull `nodeapi` into the judge and let a dead bus silence the overlay).
- **A new timer on Central to retry a missed start** (the readiness report already is the event).
