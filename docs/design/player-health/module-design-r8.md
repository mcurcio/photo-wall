# Player base layer: module design and delivery slices (on r8)

**Layer: module.** Boundaries, contracts (takes / gives / never), data flow, state ownership, wire growth, enforcement, slice plan. Binding frame: `player-shape-final.md` r8 (owner-accepted 2026-10-04). Values and per-slice signatures live in each bead's frozen page (run brief `.claude/runs/player-health-m1.md` for M1). Grounded at `origin/main` 860465c. Supersedes `player-module-design.md` (pre-r6).

## 1. Module map

```mermaid
flowchart TB
    subgraph AU["Authority (M2): appliance/authority/, unit photo-wall-central-link, uid pw-central-link"]
        CLR[runner: three NodeSessions, cadence]
        CLD[display authority<br/>extracted from display_host/service.py:107-285]
        CLS[stage authority<br/>from online_runner.py:38-47, online_broker.flush,<br/>broker_runner.emit_process_evidence, app-link outbox]
    end
    subgraph HE["Health: appliance/health/ (M1), unit photo-wall-health, uid pw-health"]
        HJ[judge: pure facts to verdict, K rule]
        HR[runner: feed readers, health.sock: status + overlay ops]
    end
    subgraph DI["Display: appliance/display_host/"]
        DL[display link = runner.py<br/>+ feed.sock for pw-health M1; permits M2]
        WB[weston.py, domain.py unchanged]
        OC[overlay/ Python: instruction + D, V; render pure draw-list;<br/>cairo painter; pywayland client; replaces diagnostic-client.c]
        SH[native/shell.c + XML v3:<br/>health layer, fallback tint]
    end
    subgraph AP["App lifecycle: appliance/node/ (broker side)"]
        BR[broker_runner main loop: lifecycle actuation,<br/>own session until M2, kill executor]
        AL[app_link: first-packet dispatch;<br/>proof accepted locally; outbox slot]
        PT[probe thread: own selector + timer,<br/>T k S K, facts to feed]
        MG[manager*: unchanged, own session = exemption]
    end
    subgraph HO["Host: appliance/node/host_*"]
        HC[HostCore: own session = exemption; recovery untouched in M1]
    end
    subgraph SK["Shared kernel, below every context"]
        K1[appliance/clock, appliance/boot_store, appliance/feed,<br/>appliance/central_session: session + http, appliance/unix_credentials]
    end
    subgraph CT["contracts/"]
        FC[node_faults: catalogue, first row app_unresponsive]
        NA[node_app_link: result vocabulary, probe_open, probe, probe_answer, relink]
        NP[node_protocol: node_health owner M2]
    end
    G[player/probe_responder on GLibDispatcher, single slot<br/>player/node_app_link: accepts recorded or accepted]
    C[(central/fleet)]
    DL -. feed.sock by cursor .-> HR
    PT -. app-feed by cursor .-> HR
    HR -- overlay instruction --> OC
    OC -- serial presented --> HR
    OC <-- private protocol v3 --> SH
    DL <-- control socket, unchanged --> SH
    G <-- "app-link socket: proof, probe channel" --> AL
    AL --> PT
    BR -- "M1: own app_effect session" --> C
    DL -- "M1: service.py display session" --> C
    FC -.- HJ
```

Import direction (lint-time, verified on a scratch config, see section 6): `(appliance.authority) > (appliance.health) > appliance.display_host | appliance.node`, all above the shared kernel and `contracts`. The kernel imports no context. Node-internal contracts live in the lower context that owns them: OverlayInstruction, D and V in `display_host.overlay`; T, k, S, K and lifecycle feed records in `node.probe`; the health input type (M4) in `health`. Only what crosses to Central or the guest lives in `contracts/`.

## 2. Contracts per boundary

| Boundary | Takes | Gives | Never |
|---|---|---|---|
| Central link ↔ Central (M2) | display, app-effect, node_health grants (owner-scoped `NodeSession`); display decisions; stage commands (`GET /v2/node/app-commands`); app-link verdicts | `DisplayExchange` per Output; the three feeds' **central-audience** records; signed app-links from the broker's outbox; one display session per compositor incarnation | holds HostCore's or the Manager's session; carries a node-audience record (probe, kill, channel); lets one session's refusal stall another |
| Central link → display link, permit op (M2) | Central decision checked for producer, request id, Output, node-clock expiry (today service.py:139-145) | Permit: operation, Output key, app run (process, app epoch, config revision), receipt, expiry on node clock | sends a Permit not derived from a decision; touches the shell socket |
| Central link → broker, stage and grant ops (M2) | Central's stage command bytes unchanged; the app-effect grant bytes | — | rewrites, filters or re-encodes the document |
| Broker ↔ app, app-link socket (M1) | One listener (`/run/photo-wall-app-proof/app-link.sock`, seen by the app as `/run/photo-wall-client/app-link.sock`). **The Player speaks first**; the first packet's `kind` selects the path. `begin`: today's proof path (kernel peer, uid 10004, running app run, grant, 2 s window, app_link.py:104-132). `probe_open`: peer uid 10004 and `driver.current()` pid only; **no grant**; the connected socket is handed to the probe thread | `begin` path: challenge, then result **`accepted`** after the local check, never a Central wait; the `local-app-control` write kept (app_link.py:135-139, read at online_broker.py:174 and advanced into recovery.py:117-135). `probe_open` path: probe nonces every T from the probe thread; **`relink`** when Central permanently refused the run's link | a base credential; a Central wait inside any exchange; frames counted as progress |
| Broker main loop ↔ probe thread (M1) | from main loop, each turn: the current app run (last `driver.current()`), "recovery may be armed" | to main loop: `kill_due` for a run; to the feed: probe facts | the probe thread calls systemctl, HTTP, the store or the driver; the main loop times probes |
| Lower contexts → judge, feeds (M1: broker, display link; M3: HostCore, Manager) | cursor (incarnation + sequence) from pw-health, over the publisher's own feed socket (0660, group `pw-node-feeds`, `SO_PEERCRED` allowlist {0, pw-health} per op) | events with per-read gap, publisher incarnation, audience; published constants T, k, S, K (node.probe), D, V (display_host.overlay) by import | know the judge exists; push to it |
| Judge → overlay client (M1) | the overlay client connects to `/run/photo-wall-health/health.sock`, op `overlay`, peer uid pw-display (10005) only | per Output: OverlayInstruction (serial, tint on/off, two rendered lines); pushed on change and refreshed every V/3 | a fault code as a field, a catalogue row, a threshold |
| Overlay client → judge (M1) | — | PresentedReport (Output, serial) from `wp_presentation` `presented`; `discarded` never counts | a claim of panel pixels |
| Overlay client ↔ shell (M1) | private protocol v3: `get_health_layer` per Output, slate surface, trial `overlay` events (unchanged) | health layer above every other layer, mapped while it has a buffer, untouched by handoff and `invalidate()`; fallback tint whenever an Output is released and no private client holds the manager | a verdict, code or deadline in any message |
| Central ↔ console (M4) | operator auth | catalogue (served), health read model; "since" = receipt minus age | lets the console compute ages from the browser clock |

## 3. Data flow

| Data | Written by | Read by | Lifetime | Ordering |
|---|---|---|---|---|
| Probe nonce / answer | probe thread / app | probe thread | RAM | nonce identity; a stale answer is ignored |
| Probe, channel, kill facts (audience node) | probe thread, main loop | judge only | broker RAM feed ring | feed sequence per publisher incarnation |
| App-link outbox slot | broker (`begin` path) | broker main loop (M1), Central link (M2) | broker `/run`; **one slot per current app run, held until Central answers; never dropped on a transient failure**; cleared on 200 or on a permanent refusal (then `relink` to that run) | latest proof of the run wins |
| `local-app-control` | broker (`begin` path, unchanged) | online_broker.service → recovery advance | broker `/run` | unchanged |
| `recovery-acknowledged` (new, M1) | online_broker.service after a successful control advance | kill predicate | broker `/run` | operation id |
| Display facts | display link | judge (feed.sock), Central (M2) | RAM ring; gap per read, never sticky | sequence per publisher incarnation |
| Verdict, conditions, ring | judge | `status` op (legs, tests); Central (M2) | judge RAM in M1; `/run` in M3-1 | verdict sequence |
| Overlay instruction / serial presented | judge / overlay client | overlay client / judge | RAM both ends | serial |
| Catalogue | release (`contracts/node_faults`) | judge; Central serves console (M4) | release | catalogue digest |
| Permit set, stage document, grant, intent, mute, cursors | as r8 (M2/M4) | | | |

No row orders data across processes by time: every cross-process order is a publisher sequence with an incarnation, a nonce or a serial (R7). All node times are `CLOCK_BOOTTIME`.

## 4. State ownership and restart survival

| State | Owner | Display-link restart | Judge restart | Broker restart | Weston restart | Reboot |
|---|---|---|---|---|---|---|
| Admissions | shell | revoked (Q7) | kept | kept | lost | lost |
| Verdict, conditions, ring | judge (RAM in M1, `/run` from M3-1) | kept | **lost in M1** (non-goal), kept from M3-1 | kept | kept | lost |
| Probe channel, misses | probe thread RAM | kept | kept | reset; the Player reconnects (retry cap 5 s < S); the run gets a fresh startup budget S from broker start | kept | lost |
| App-link outbox slot, `recovery-acknowledged` | broker `/run` | kept | kept | kept | kept | lost |
| Display feed ring | display link RAM | new incarnation; readers resnapshot | kept | kept | kept | lost |
| Overlay instruction | overlay client RAM | kept | republished on reconnect | kept | client respawned; republished | lost |
| Permits, sessions, restart budget | M2 / M3 as r8 | | | | | |

## 5. Wire vocabulary growth (implementation-workflow §3.2)

| Vocabulary | Consumers | How it grows |
|---|---|---|
| App-link result `accepted`; `probe_open`, `probe`, `probe_answer`, `relink` | contracts, player, broker | Three packages, so additive: contracts + Player first (M1-B3: Player accepts `recorded` and `accepted`, opens the probe channel and tolerates today's refusal, answers `relink`), then the broker (B6 probe, B7 `accepted` and `relink`). **No guest-contract version scaffolding:** `base_abi` is a content hash of the whole node-base package (build_node_base_deb.py:79-87), and a Player sealed for another base is refused at verify (environment.py:147-148), so base and Player skew is impossible by construction |
| Private protocol `pw_diagnostic_manager_v1` v3 + `pw_health_layer_v1` | shell, overlay client | base-private, all in `appliance/display_host`; shell first (B4, client still binds v2), client binds v3 in B11. `plugin_abi` stays `frame-v3` (build_node_display_deb.py:62); `graphics_abi` already hashes every installed file |
| Feed record (sequence, kind, value, audience) and read op (`after`, `incarnation`) | display link, broker, judge; Central link (M2) | kernel primitive first (B2b) with the display link as its first consumer; the `events` op keeps its fields and adds `publisher_incarnation`; `stream_gap` becomes per read |
| `NodeProducerV2.owner` + `node_health`; `HealthConditionFact` | contracts, central, console | M2-11 additive as before |
| Display decision: intent, dark periods, mute | contracts, central, authority | M4, capability-flagged |
| Catalogue codes | judge, Central, console | data; a new code is one `contracts` change deployed Central-first; the judge refuses an unknown code |
| `AppProcessFact.state`, `SurfaceFact.state` | — | not grown |

## 6. Enforcement mapping

| r8 rule | Mechanism | Strength | Bead |
|---|---|---|---|
| Contexts point down | import-linter `layers`: `["(appliance.authority)", "(appliance.health)", "appliance.display_host \| appliance.node"]`; parentheses mark a layer optional until its package exists (import-linter errors on a missing plain layer: verified) | lint | M1-B2c; health un-parenthesised in B9, authority in M2 |
| Display never reads the catalogue | `forbidden`: `appliance.display_host` → `contracts.node_faults` (a forbidden target may not exist yet: verified) | lint | B2c |
| Session/HTTP only in Authority (+2 exemptions) | session and http **moved out of `appliance.node`** to `appliance.central_session` (B2a), because a `forbidden` contract whose target sits inside a source package reports nothing for that package (verified: `appliance.node → appliance.node.session` went unreported). `forbidden` sources `appliance.display_host`, `appliance.node` (+`appliance.health` from B9) → `appliance.central_session`; `ignore_imports`: two permanent exemptions (`host_runner`, `manager_desired`) and a **ratchet** of four (`display_host.service` removed in M2-13; `broker_runner`, `app_link`, `online_broker` removed in M2-15), frozen by a test that fails on any addition. Verified on a scratch tree: kept as written; upward import, display→node import and health→session each turn it red | lint + test | B2a, B2c |
| Kernel knows no context | `forbidden`: clock, boot_store, feed, central_session, unix_credentials → every context | lint | B2c |
| Host ⟂ App lifecycle inside `appliance.node` | host-core closure check (build_node_base_deb.py:55) + symmetric app-broker check (no `host_*` in its closure) | build | B6 |
| No network for display link, broker, judge, overlay client | `RestrictAddressFamilies=AF_UNIX` on the judge from B9; the others in M2 | boot + leg | B9, M2 |
| Feeds reachable only by the judge (and root) | per-publisher directory and socket, group `pw-node-feeds`, `SO_PEERCRED` allowlist per op | boot + unit test | B6, B10 |
| `K > k·T + raise + D` and `K > S + raise + D` | the judge refuses to construct; CI test builds it from shipped constants | construction + test | B9 |
| Shell holds no policy | XML carries no code or verdict argument; harness asserts | protocol shape + test | B4 |
| Health layer above app; fallback tint | harness pixel tests (`weston_capture_v1`, `--debug`) | test | B4 |
| Probe answered from the control queue | responder dispatches through the injected `GLibDispatcher` (player/service.py:328-355), single slot; starved-stub unit test; tracer leg + `to_thread` variant | test + convention | B3, B12 |
| Probe facts never reach Central | feed `audience` set at append; Central-bound code never reads the feed in M1; M2's Central link delivers only `central` | construction + test | B2b, B6 |
| No kill while a recovery may be armed (Q1 fence) | pure predicate over the broker's `online` record and `recovery-acknowledged`, checked by the main loop before every kill | construction + test | B8 |
| A publisher never knows its readers | feeds read by cursor | convention | B2b |

## 7. Questions for the owner

**Q1 (R8) — HostCore's switch-recovery reboot.** Unchanged and still open; it does not block M1. **M1 fence:** no change to `recovery.py`, `recovery_linux.py` or the arming in `online_broker.py` (:98-108, :183-184, :211); the broker never kills while a recovery may be armed; restart with backoff moves to M3-5; the tracer ends at kill, then slate. The recommendation (retire the reboot, M3-8) and its alternative stand as written in r8; the owner answers before M3.

**Q2 — First checkpoint.** Scoped by the orchestrator under the owner's "start the build after the planning is complete": the run delivers **M1 only** and stops at the M1 coherence checkpoint. M2 needs a new go.

**Mechanism choices made here (not owner questions):**
1. The Central link (M2) delivers into each lower context's ingress op; the receiver persists in its own `/run`. No cross-uid file writes.
2. Exchange outcomes are pushed into the judge in Health's input type (M2/M4).
3. The broker parses the grant (online_broker.py:53-56).
4. Feeds use one ring primitive with cursor, per-read gap, publisher incarnation and audience. Drop-oldest is counted. **The app-link record is not a feed record**: it sits in its own outbox slot and is never dropped on a transient failure (Central needs it for admission and stage commands: central/fleet/node_display.py:166, node_lifecycle.py:136-138).
5. Host ⟂ App lifecycle is enforced by computed closures. Splitting `appliance.node` is parked.
6. The starvation seam is a fixture-derived `starve` role (build_node_pid1_fixture.py:37-79 pattern) that floods GLib at a priority between redraw and default-idle on `SIGUSR1`; never production Player code.
7. The probe rides the existing app-link socket: the Player speaks first (`probe_open`), with capped slow retries (0.5 s doubling to 5 s), because today's broker serves one connection per loop turn (app_link.py:80-95) and refuses unknown first packets. No new socket, no change to `proof_directory` (app_link.py:27-35).
8. **Probe timing runs on its own broker thread** with its own selector and timer. The main loop blocks on systemctl and HTTP (broker_runner.py:81, :103, :110-112, :121; online_broker.py:242, :249; online_runner.py:39; process_linux.py:23-26; import_worker.py:25, :73), so a probe timed there would miss for reasons that are not the app's. The thread measures; the main loop actuates (kill) after re-checking the run and the Q1 predicate.
9. Startup: an app run's unanswered time counts from its launch (or broker start, if earlier unseen); no miss is reported before launch + S. The judge's K rule includes S, so "no channel since launch" still shows the card before the kill.
10. The Player responder keeps at most one probe callback queued in the shared `GLibDispatcher`; a newer nonce overwrites the queued one; when the dispatcher refuses for capacity, no answer is sent (an honest miss).
11. The overlay client is a pure render module producing a draw-list (unit-tested on macOS without cairo or pywayland), a cairo painter, and a pywayland adapter tested only in the harness. It ships in node-display.deb at the old spawn path (`/usr/lib/photo-wall-display/diagnostic-client`, now a Python launcher), so the shell's spawn code is unchanged; bindings are generated by the pywayland scanner at build. It raises its own `/proc/self/oom_score_adj` at start (unprivileged, no C).
12. The fallback tint keys on "no private client holds the manager resource" (shell.c `diagnostic_resource`), so it lands in M1 without tinting healthy walls whose C or Python client is bound.
13. The judge's socket is `/run/photo-wall-health/health.sock` (directory 0755, socket 0666, `SO_PEERCRED` per op: `status` uid 0, `overlay` uid 10005). Weston's sandbox needs only the path; the app cannot see `/run` (TemporaryFileSystem, process_linux.py:71).
14. The display link's existing root-only `ingress.sock` lives in Weston's 0700 runtime directory (runner.py:145, :181; photo-wall-display.service:28-29), so pw-health reads the display feed from a second listener, `/run/photo-wall-display-feed/feed.sock`, serving only `events`.
15. Tracer split: M1 proves the G7 seam on today's Central path, ending at kill and slate; M2 completes r8's tracer steps 1, 3, the permit-per-run restart and the Central ring.

## 8. Delivery slice plan

Per-bead gate: static + scoped unit; the display harness for any `appliance/display_host` change; one or two local node-pid1 legs for every bead that changes node runtime behaviour. Once per milestone: every unit, DB and browser test, every node-pid1 leg and CI. Sizes are human-equivalent hours, ±40 %. Basis: lines moved or written (service.py 289, broker_runner 133, app_link 144, diagnostic-client.c 215, shell.c +75), tests about 1:1, a fixed 2–3 h per new CI job or fixture role.

**M1 — G7 seam (tracer, part 1).** Observable: leg `unresponsive`: admitted healthy Player answers probes; starved Player gets tint and card over its live output with the serial presented; killed after K; slate under the card. Bead pages: run brief.

| # | Behaviour | Packages | Green alone because | Risk / lens | h |
|---|---|---|---|---|---|
| B0 | Display harness: CI job + Docker local runner; today's slate, private-role refusal, app above slate, trial events, pixel capture, pywayland scanner on the private XML | tests, scripts (+ .github) | asserts existing behaviour | CI | 6 |
| B1 | node-pid1 fixture binds a Frame; leg `unresponsive` (healthy form): real Player admitted through today's Central path | tests (+ .github) | test-only | CI | 5 |
| B2a | Kernel move: clock, boot_store, central_session (session + http) out of `appliance.node`; release_plan paths | appliance, scripts | pure move | none | 3 |
| B2b | Feed primitive (per-read gap, incarnation, audience); display link adopts it | appliance (kernel), display_host | same `events` op, fixed gap | none | 3 |
| B2c | Lint contracts with ratchet + ratchet test | pyproject, tests | config + test | none | 1.5 |
| — | **Architect pass 1** | | | | |
| B3 | Guest contract additive; Player probe responder (single slot); accepts both results; `relink`; starved-stub test | contracts, player | today's broker refuses `probe_open`; tolerated | public wire: 1 lens | 5 |
| B4 | Shell health layer v3 + fallback tint; harness pixel tests | display_host, tests | inert until a client binds v3; tint only without a private client | security lens | 5 |
| B5 | Python overlay client at parity; `diagnostic-client.c` deleted; OverlayInstruction, D, V published | display_host, scripts | harness parity | regression lens | 6 |
| B6 | Broker probe channel + probe thread + broker feed socket; `pw-health`, `pw-node-feeds`; symmetric closure check | node, scripts | Player answers (B3); no kill | none (regression legs) | 6 |
| B7 | App-link accepted locally; outbox slot; `relink` | node | Player accepts `accepted` (B3) | security lens | 4 |
| — | **Architect pass 2** | | | | |
| B8 | Kill after K on the main loop; Q1 predicate; pidfd identity | node | probe facts exist (B6) | security lens | 4 |
| B9 | Catalogue + judge core + K rule + unit + broker feed reader + `status` op | health, contracts (+ scripts) | nothing draws yet | none | 5 |
| B10 | Display feed socket for pw-health; judge per-Output verdict; `overlay` op | display_host, health (+ scripts) | no client connects yet | security lens (allowlist) | 4 |
| B11 | Overlay client draws health layer: tint, card, V card, serial presented | display_host, tests | judge exists (B9, B10) | none | 5 |
| B12 | Fixture `starve` role; leg `unresponsive` complete; `to_thread` variant | tests, scripts (+ .github) | proves B0–B11 | none | 6 |
| D1 | Docs | docs | | | 2 |
| — | **Architect pass 3 = M1 coherence** + milestone gate | | | | |

**M2 — Authority (r8 tracer complete).** As drafted (beads 11–17), plus: M2-13 removes the `display_host.service` ratchet line; M2-15 removes the three broker ratchet lines and moves the outbox slot's delivery to the Central link; M2-14's broker feed carries only `central`-audience records to Central; the permit-per-run restart step (probe h) needs an app restart, which now arrives in M3-5, so that step and probe (h) move to the M3-12 legs; M2 is re-cut at its own gate. 34 h.

**M3 — fault coverage and supervision (node).** As drafted, with: M3-5 now includes the app restart with growing delays (moved from M1) (6 h); M3-10 removed (fallback tint and overlay `oom_score_adj` landed in M1). 49 h.

**M4 — Central and console.** Unchanged. 38 h.

**Named mutation probes** (each must turn red): (a) Player answers probes from a helper thread (`to_thread`): leg variant (B12, run at the M1 gate). (b) Health layer stacked below the app layer: harness (B4). (c) `discarded` counted as presented: harness (B11). (d) K below either bound: judge construction test (B9). (e) Any packet on the probe channel counted as an answer: unit (B6). (f) Broker waits for Central before `accepted`, Central blackholed: unit (B7). (g)–(j) as drafted (M2, M3). (k) Fallback tint skipped: harness (B4). (l) Kill while a recovery may be armed: unit (B8). (m) Probe facts on a Central-bound path: unit (B6).

**Legs:** M1 adds `unresponsive` (extended in M2); M3 adds `crashloop`, `guards`, `unreachable-cold`. M1 also adds the `display-harness` CI job.

**Totals and budget gates:** M1 15 code beads + 1 docs bead, about 70 h; M2 34 h, M3 49 h, M4 38 h; about 191 h. M1 run ceilings, with basis, are in the run brief.

## 9. Parked

- Values beyond M1 (windows, hold-downs, V, D, P, budget, delays, permit lifetime, ring sizes, mute bounds); M1 values are frozen in the bead pages.
- Splitting `appliance.node` into `host` and `app_lifecycle` packages.
- Renaming `photo-wall-display-controller.service` to match "display link".
- Pi memory measurement (A5) before `MemoryMax=` for the Central link and judge.
- `WATCHDOG=trigger` on systemd 257; tty1 visibility with `TTYReset=no`; a periodic repaint pulse that keeps direct scanout.
- Removal of the Player's `recorded` arm once Central-side acceptance moves (M2).
- Mute console UX; log shipping; per-boot base key; pstore (deferred by r8).
- Player fixes independent of this shape: G3, G2.

History: 2026-10-05 module layer and slice plan drafted on r8, grounded at 860465c. 2026-10-05 r9: slicing and feasibility reviews applied (tracer leg first, kernel split into move / feed / lint with a verified ratchet, probe on its own thread with the Player speaking first, app-link outbox and `relink`, Q1 fence, judge before the stale card, fallback tint and overlay OOM into M1, per-read feed gap with incarnation, display feed reachable by pw-health, no guest-contract version scaffolding); M1 re-cut to 15 code beads + docs.
