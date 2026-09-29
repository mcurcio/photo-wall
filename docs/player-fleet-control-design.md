# Base-owned Player visibility and app updates

**Status:** architecture proposal for review; no implementation or deployment change. **Scope:** F1–F4 and F6 in the [v0.13 production-readiness intake](production-readiness-v0.13.md). F0's immediate application-protocol repair is in the [control-protocol design](player-protocol-compatibility-design.md). This proposal changes parts of the existing [PXE/base selection](decisions/0012-netboot-base-auto-mirror.md) and [central reachability](decisions/0014-reaching-central-from-every-boot-stage.md) behavior; the decisions below require owner review before implementation.

## Why the present path fails operationally

The v0.13 base contains a provisioner that fetches and installs a `.deb`, starts the Player unit, then exits ([provisioner](../appliance/provision.py), [bootstrapper build](../scripts/build_bootstrapper_deb.py)). Once it exits, no base-owned process checks in while the app is being replaced or fails. Central's Player `last_report_at` means **accepted app readiness**, so absence conflates app protocol failure with a powered-off Pi. The global manifest may serve a v0.12 app while netboot serves a v0.13 base; global auto-promotion stops after a bound Player and cached package ([release sync](../central/content_catalog/sync.py)). Promotion does not change an already-running process. The optional per-device `.deb` path pairs app and base tags but also couples app rollout to OS selection ([runbook](runbook.md#base-image-auto-mirror-0012)).

The [live diagnosis](evidence/2026-09-29-kubernetes-central-diagnosis.md) found a served v0.13 base, v0.12 global app manifest, successful enrollment, no accepted readiness, no base-health rows, and a `failed` base outcome with no known-good target. None of those facts proves the panel is dark or that the base image actually failed. The design must represent what each layer has observed separately.

## Owned state and boundaries

| Owner | Durable or current facts | May do | Must never infer |
|---|---|---|---|
| Base OS management agent | Current kernel boot ID, base tag/build, own protocol version, check-in sequence, provisioning phase, bounded fault, Player unit state; volatile package staging. | Reach Central and report while the Player package is absent, installing or crashing; execute a current-boot update instruction after preflight. | Frame authority, accepted app readiness, media readiness or pixels from OS reachability. |
| Central fleet control | Fleet app policy, optional per-device app override, exact package availability, current attempt, previous accepted app, base-selection/health facts, receipt times. | Choose desired app and OS independently, issue/queue idempotent actions, fence failed attempts, publish operator diagnostics. | Installed/running app from a served manifest, or app health from a base check-in. |
| Player application | Fresh enrollment authority, app protocol/capabilities, readiness, output observations and execution. | Report app and presentation facts under the existing token and epoch. | Durable update policy or OS image selection. |
| Base error display | Short local boot/install/start failure state, independent of the replaceable `.deb`. | Keep a visible error page until verified app takeover or reboot; update or age its diagnostic text as evidence changes. | Scene content or confirmed output after the app takes over. |

Keep Frames, bindings, Scenes, Programs and Runs under their existing owners. A base observation never creates or revives a Player session. Retired equipment may still be seen at the PXE/OS layer but never regains app authority by checking in. Preserve the [stateless PXE Player requirement](requirements.md#player-provisioning), [execution authority](execution-contract.md), and one shared Central discovery/direct-request rule from [decision 0014](decisions/0014-reaching-central-from-every-boot-stage.md). The base carries no Immich integration.

### F1: resident base check-in

Package a small `photo-wall-os-agent.service` in the **base/bootstrapper**, not the replaceable Player `.deb`. Start it after networking, before the first package attempt; keep it running after the provisioner exits and through `dpkg` and Player service restarts. The current provisioner and online updater should share **one** package transaction helper for fetch, checksum, install, start and rollback. The agent owns phase reporting; a root-owned bounded volatile `/run` record can bridge the installer and agent. This avoids two implementations of app installation.

Proposed additive `POST /v1/appliance/check-ins` envelope, capped around 2 KiB:

```json
{
  "schema": 1,
  "kind": "pi",
  "serial": "bounded reported serial",
  "kernel_boot_id": "Linux boot UUID",
  "serve_id": "Central-issued opaque boot serve ID",
  "base_tag": "v0.13.0",
  "base_digest": "verified squashfs digest",
  "sampled_boottime_ms": 12345,
  "agent_build": "bounded version or digest",
  "phase": "installing_app",
  "attempted_app_tag": "v0.13.0",
  "attempted_app_sha256": "hex digest",
  "fault_code": null
}
```

For PXE, stage one sends its kernel boot ID and a bounded request nonce with base selection; retries of the same boot/selection are idempotent and must not count as a new failed boot. Central issues a monotonic per-device serve sequence plus an opaque serve ID, selected tag and digest with the base response. Stage one verifies the downloaded base bytes and passes those fields through a validated, volatile handoff to the base agent. The current base route records a serve before HTTP streaming finishes and stage one passes no such context, so neither `last_served_tag` nor a response header alone proves receipt. A flashed or legacy base without this handoff may report reachability but cannot establish exact served-tag acceptance. The serve sequence orders separate boots; a kernel UUID alone does not.

The phase vocabulary covers `base_ready`, `fetching_app`, `verifying_app`, `installing_app`, `starting_app`, `player_unit_started`, and `retry_wait`; a fault is a bounded code, never raw logs, URLs or credentials. `player_unit_started` means systemd accepted start, **not** app protocol acceptance. Central returns only receipt status, no Frame/media authority or update command. Candidate cadence is 10 seconds plus immediate phase changes, with bounded request time and jittered retry; choose final age thresholds from measurements. This service uses the existing cmdline-first Central locate/transport path and does not follow a redirected POST to an untrusted origin.

This is **observational serial-claim telemetry** on the owner-approved trusted LAN, consistent with existing netboot/enrollment identity limits. Central computes its device key from the normalized serial, checks a recent matching PXE serve ID for exact-boot claims, caps and rate-limits input, sets per-device/global cardinality quotas and a history TTL, and uses Central receipt time for freshness. A legacy or flashed device without a serve record remains an observational claim only. Check-ins never create `players` or advance the release frontier. The endpoint cannot serve commands or grant app authority. A boot-scoped token may be useful for a separate desired-state channel, but it does not turn a spoofable serial into cryptographic device identity. The command channel's issuance/scope must be settled before F4 implementation.

Store boot observations separately from `players` and accepted `player_feedback`. Key them by device, Central serve sequence/ID and kernel boot ID; accept only monotonically newer uptime samples within a boot. The greatest valid Central serve sequence becomes current and retained superseded boot IDs cannot regain current status after delayed delivery. A non-PXE observation without a serve sequence cannot supersede an exactly correlated PXE boot. Client wall time is not authoritative. A newer app may report the kernel boot ID for an exact join; legacy Player enrollment uses a different random boot context ID, so its OS/app join is by device and time and must be labelled uncertain. Preserve an explicit **OS telemetry unavailable** state for old bases, rather than calling them disconnected. A new base talking to old Central treats an unknown check-in route as nonblocking and still provisions the app; a new Central accepts the base protocol of every staged boot tree until those trees are retired deliberately.

### F2: layered operational status

The console uses one projection of independently timestamped facts:

| Layer | Evidence | Honest UI state |
|---|---|---|
| Base reachability | Last accepted OS-agent check-in and its age, or legacy-unavailable | `Base heard recently`, `Base last heard …`, or `OS telemetry unavailable`; never `displaying`. |
| Provisioning | Current-boot phase and bounded fault from the base | `Installing app`, `Start failed`, `Waiting to retry`, with attempted tag/digest and receipt age. |
| App session | Enrollment epoch, negotiated control schema, accepted readiness and last rejection when known | `App enrolled, no accepted report (cause unknown)` until a specific protocol/fault is observed; `App incompatible` only with evidence. |
| Presentation | Last Output observation, its collection time and any confirmed visible-state evidence | Label current v0.13 connected/mode data **reported at last Player start** until live hot-plug/mode telemetry exists; distinguish all of it from Central's intended Scene/Run. |

Test fresh OS plus fresh app with stale startup-only Output data, and show each age separately. A fresh OS report with silent app means the box reached the base layer; it does not prove the app works. Fresh app readiness with stale OS telemetry suggests an agent fault or legacy base, not a disconnected Player. When Central itself is unreachable, the UI can only show last receipt times. Include unbound and retired equipment in diagnostic views without confusing it with Frame attention. The base-owned error display is a separate F2 submodule: the current Weston/Player units ship in the replaceable `.deb`, so a package failure page must have its own base display path and physical qualification.

## F3/F4: app policy and activation

Keep **app selection distinct from base pinning**. Proposed Central policy has a fleet desired app tag plus an optional per-device override for canaries; each policy change has a monotonic revision. Record the exact digest and size selected, not only a tag. `deployable` release metadata does not prove bytes are mirrored or compatible with the booted base. A device record distinguishes Central `desired`, Central `offered/served`, agent `downloaded`, digest `verified`, `installed`, authenticated `running`, `app protocol accepted`, `sustained healthy`, and `output observed`, each with its owner, attempt/boot ID and timestamp. A 200 response begun is not proof that the client received all bytes. The current `last_good_tag` in the app-release policy means a previously promoted on-disk package, **not** a health-qualified rollback target; introduce a separate accepted-app record. A new versioned desired-app manifest uses precedence **per-device app override → fleet desired**, independent of the base pin, and reports the exact selected tag/digest. Old provisioners retain their existing global `/v1/app/manifest` or paired `/v1/netboot/manifest` semantics until their boot trees retire; an app-only canary requires the new base agent/manifest path. Do not silently reinterpret those legacy routes.

| Attempt phase | Central/base action and durable result |
|---|---|
| Queued | Operator selects fleet or per-device desired app; an offline device retains the revision for next boot. |
| Preflight/mirroring | Verify exact target and previously accepted fallback digests, base/app ABI and protocol compatibility, bounded RAM/cache space, and the declared Run interruption policy. For an online update, stage and verify **both** `.deb` files in RAM before stopping a healthy app. |
| Ready/commanded | Base agent polls desired revision; an optional push is only a wake hint. An online action is fenced to current boot and attempt. |
| Downloading/verified/installing/starting | One base-owned package transaction reports each phase. Keep the verified fallback bytes until the new app is accepted; a half-configured `dpkg` result triggers a defined repair/reinstall or reboot path. |
| App accepted/committed | New app enrolls under a fresh authority epoch, negotiates a supported control schema, and sustains accepted readiness. Only then may its tag become a rollback candidate. Output observation is reported separately. |
| Failed/recovering | Fence the failed target for this device/attempt; restore a previously accepted, obtainable, base-compatible app, or enter `awaiting_operator` with the base agent and error display alive. |

Cold boot and online update use the same Central desired revision and package transaction. A reboot re-reads Central policy; no durable local app slot is assumed. If Central fails before stopping the current app, leave that app running. If it fails afterward, restore the already staged and verified accepted fallback; refuse a healthy online activation when that fallback is absent or incompatible. A first install with no accepted app instead keeps the base agent and error page available, and reports the outcome on reconnect. A duplicate, delayed, or reordered command/report cannot supersede a newer desired revision or boot. A package is not accepted merely because `dpkg` succeeded or systemd started it. A failed app target is fenced for that device and policy revision. On the next boot, if fleet policy still names the fenced revision, choose a compatible accepted fallback; with none, serve no failed app and enter `awaiting_operator`. An explicit operator retry of the same tag creates a new policy revision after preflight and can clear the fence; a distinct verified newer revision supersedes it. Keep this app fence separate from the OS base-image fence so each failure has one owner.

The first console slice may expose existing release list and **global** promotion, but must state that the target becomes eligible on a later boot **once mirrored** and show desired versus the digest actually served. During mirroring, the legacy manifest may still return the previous on-disk package. Full F3 adds per-device desired/canary state and observed installed/running versions; do not use the OS base pin as an app-only control. For `Update now` or reboot, default to a **bounded maintenance queue**, not an indefinite wait for idle: looping Programs may never leave a device idle. Expiry leaves the action queued with a reason. An explicit `interrupt now` action must name every bound Frame/Output and its Run/lease effect before execution; operational equipment withdrawal must not rewrite authored Scene or Program state. The exact policy for a multi-Frame Run and secured assignments remains an owner decision. An offline device can simply queue the desired revision. The OS agent, not the replaceable Player app, executes update/reboot so an incompatible app does not block recovery.

## F6: base-image health and no-known-good recovery

Separate these predicates:

1. **Base reached OS:** an agent check-in with a validated stage-one serve handoff proves the verified image booted far enough for networking and the agent, tied to that boot; a legacy uncorrelated check-in proves only a serial claim. It does not prove package installation or rendering.
2. **Base accepted:** an explicit base-owned self-check of critical mounts, network/agent and required base services, plus repeated healthy observations separated by a candidate grace period for the exact verified serve ID/tag/boot. A single heartbeat cannot advance the fleet's latest-verified frontier; the final self-check and duration require physical evidence.
3. **App accepted:** the selected package establishes a compatible app session and sustains readiness under its authority epoch.
4. **Output observed:** the Player reports the actual Output separately.

The current app-owned `/v1/player/base-health` is absent on the default global package path, so a repeat boot can be labelled failed without a base-owned positive signal. Replace that dependency with base-owned evidence while preserving exact served-tag and stale-boot fences. Classify `booted but unaccepted`, `no report/unknown`, and `observed failure` distinctly; a missing report becomes failed only after a named timeout or a **different kernel boot** under the selected policy, never a retry of the same base fetch. Preserve existing `known_good_tag` records with **legacy Player-reported provenance** as fallbacks during migration, without relabelling them new-policy base acceptance or advancing the new frontier from an uncertain join. Define whether latest-verified **fleet** base selection requires a canary's base and app acceptance; the proposed default is **both**, so an OS that pings Central but cannot run the supported Player cannot advance the fleet. Device-local base recovery may use a separately accepted base even when the app later fails. Do not reuse the app policy's `last_good_tag` as a base known-good record.

If a base fails before the agent starts, Central may see only a new PXE request or no new report; preserve a bounded fallback through a separately known-good base or an operator pin. If a device has **no** known-good base/app, Central should fence the failed target and return a named `recovery_required` response rather than re-serving the same bad bytes. Stage one must show its pre-OS diagnostic and retry at a bounded cadence while Central shows `awaiting_operator`; an operator pin or newly verified rescue base is needed to boot. The existing policy instead re-serves the failed target and stage one reboots, so this is an explicit PXE/server policy change, not something the OS agent can solve. A failure before any usable base starts cannot produce an OS check-in or base-rendered error page.

## Rollout and acceptance gates

1. Land additive Central OS observations and a stable v1 check-in endpoint first. Old bases omit reports; existing app operation stays intact. Land the base agent in a new boot tree second; an old Central's 404 must not stop provisioning. Stage and test the new base on a canary before relying on OS liveness in the console.
2. Land the F0 application-protocol compatibility repair independently. Then add app hello/negotiation and the desired-app policy/attempt records. Do not drop legacy state support merely because the new base agent exists.
3. Qualify base reporting through missing `.deb`, manifest 503, checksum mismatch, `dpkg` failure, app start failure, strict-state parse failure, app crash/restart, Central/ingress outage, and agent crash/restart. In every case, check that OS observations continue when networking is available and never create app authority.
4. Qualify one-device canary, old and new manifest paths, global promotion with an offline device, update while a looping Program or multi-Frame Run is deferred or explicitly interrupted, central restart at every attempt phase, cache loss after app stop, fallback bytes withdrawn, no-known-good startup, and rollback. Use exact release artifacts and the Kubernetes ingress; use physical Pi/panel tests for boot continuity and visible error/return to playback. Controllable-clock tests establish ordering and expiry, not pixels.

The proposed 10-second check-in and approximately 45-second stale threshold are candidate budgets, not accepted requirements. Do not claim F1–F6 complete from a green Central HTTP endpoint or simulated Player. Record final decisions in [design decisions](design-decisions.md), revise the owning [architecture](architecture.md), [runbook](runbook.md), and [validation](validation.md) as contracts settle, then implement in bounded slices.

## Decisions for owner review

| Decision | Proposed default | What would change it |
|---|---|---|
| Base check-in trust | Serial-claim, observation-only endpoint on trusted LAN; correlate PXE reports with a recent Central serve ID, enforce quotas/TTL, and return no Frame or update command. | A stronger device-identity requirement or exposure outside the trusted LAN. |
| Desired-state command scope | Separate current-boot management session/token and monotonically versioned pull of desired state; push only wakes the agent. | Proven simpler route with equivalent replay, authority and offline behavior. |
| App rollout | Fleet default plus per-device app override; base pin remains independent. | A product choice to couple every app and base release and forgo app-only canaries. |
| Acceptance/rollback | Sustained app readiness qualifies a fallback; an installed or merely promoted package does not. A healthy online update requires both target and fallback bytes verified in RAM before stop. No known-good app leaves the base agent alive awaiting operator. | Physical evidence of a safer bounded automatic strategy. |
| Base frontier | A canary must pass correlated base self-check **and** compatible app acceptance before advancing the fleet; OS heartbeat alone is insufficient. Legacy known-good remains a labelled fallback. | An explicit decision that base rollout is independent of app ABI and output risk. |
| Online interruption | Bounded maintenance queue by default; expiry is visible. `Interrupt now` names all affected Frames and has an explicit multi-Frame Run/lease policy without changing authored state. | An approved maintenance-window-only update model. |
