# Architecture

> **Status: proposed architecture.** The [requirements](requirements.md) define product behavior. [Decision 0006](decisions/0006-central-authority-and-stateless-players.md) fixes central durability and the stateless Player boundary; unresolved choices are tracked in [design decisions](design-decisions.md), with delivery work in the [implementation plan](implementation-plan.md).

## System boundaries

Photo Wall owns installation configuration, Scene execution, compatible media assignments, calibration and display history. Immich remains the media library. Build the domain core because persistent Frame locations, live sources, nested Runs, per-target overlays and rolling assignments need one coherent model. Reuse media and infrastructure components around that core. Dependencies must support free, self-hosted deployment.

Start with one modular central application, a Procrastinate media worker, and one Python Player process per Raspberry Pi. PostgreSQL and the task queue exist only in central components. Players render centrally selected media from current timed instructions and retain no authoritative state. Authoring and operations use a web UI.

```mermaid
flowchart TD
    UI[Web authoring and operations] --> CENTRAL
    IMMICH[Immich] --> CENTRAL
    subgraph CENTRAL[Central application]
        REG[Installation and definitions]
        RUN[Runtime]
        PLAN[Planner]
        SESSION[Commitment and Player coordination]
        REG --> RUN
        RUN --> PLAN
        PLAN --> SESSION
    end
    PLAN --> WORKER[Media preparation worker and file gateway]
    IMMICH --> WORKER
    SESSION <--> PLAYER
    WORKER --> PLAYER
    subgraph PLAYER[One Python Player process per Pi]
        NET[Session and equipment agent]
        CACHE[Cache]
        EXEC[Executor and time mapping]
        RENDER[GStreamer and native composition]
        GTK[Persistent GTK surfaces]
        NET --> CACHE
        NET --> EXEC
        CACHE --> EXEC
        EXEC --> RENDER
        RENDER --> GTK
    end
    GTK --> WESTON[OS kiosk display host]
    WESTON --> HDMI[One or two HDMI Outputs]
    SESSION --> ACT[Actuator adapters]
    ACT <--> DEVICE[Home Assistant or device controllers]
```

These are responsibility boundaries, not a service per box. The [execution contract](execution-contract.md) defines preparation, commitment, intended-time execution and feedback to both Runtime and Planner.

## Central modules

| Module | Ownership and interface | Failure responsibility |
|---|---|---|
| Installation registry | Desired topology, bindings, typed target groups and calibration revisions; accepts separate equipment observations. | Retire stale equipment authority while preserving Frame identity. |
| Definitions and activation ingress | Versioned Scenes, sources and Programs; normalize schedules, manual requests and Sensor events into Activation requests. | Reject invalid definitions and deduplicate activation according to the request policy. |
| Runtime | Active Run tree, logical progression, lifecycle and per-target intent. | Apply execution outcomes to completion/cancellation; preserve current-state semantics during projection. |
| Catalog and Planner | Refresh live source metadata, enforce compatibility, select assignments and exact variants, project preparation needs. | Distinguish source failures from empty results; revise unsecured work or choose authored fallback. |
| Commitment and Player coordination | Sessions, plan revisions, readiness, execution authorization and observation routing. | Reject stale work and apply defined missing-participant/reconciliation policies. |
| Media worker and gateway | Atomically enqueue through Procrastinate, acquire selected files, prepare/publish authoritative variants and deliver authorized exact bytes. | Preserve publication recovery and stale-attempt fences; report acquisition and capacity failures. |
| Provisioning and releases | Recognize equipment observations, issue boot tickets, store accepted/candidate releases and consume trials. | Bind health to the current boot/session and select the centrally accepted release after a failed trial. |
| Peripheral adapters | Normalize Sensor events and execute resolved Actuator intent or equipment commands. | Report failures without independently resolving Scene priority or target ownership. |

Use PostgreSQL for all durable Photo Wall state: installation intent, Runtime/Planner/coordination records, media lifecycle, queue jobs, equipment recognition, and release/trial policy. Modules expose named operations and transaction-bound repository ports; they do not call another domain's private methods or SQL. Typed Pydantic transports cross process/domain boundaries. Psycopg pools own central connections, and atomic operations such as media request plus task defer share the caller's transaction.

## Player and rendering

The Player is one process with one display-resource owner. Its session module holds only current plans/configuration and a fresh authority epoch; the equipment agent reports Outputs; the disposable cache secures exact bytes; the executor is the sole local authority owner; and the Renderer owns decoding, composition, effects and calibration. A Player has no database, durable identity, execution journal, update slots, or authoritative cache metadata. Diagnostics collect current observations. Weston, clock discipline and process supervision remain OS services.

Embed GStreamer through Python/PyGObject rather than launching a media-player executable. Python orchestrates; native elements and GPU operations handle sustained media processing. The initial [native implementation decision](decisions/0005-native-platform-and-registration-fallback.md) permits bounded appsink buffer uploads into GLArea composition, with no Python pixel-processing loops; its copy cost must be qualified before claiming a device capacity. Nested Runs need not map to nested pipelines or processes.

The Renderer uses this conceptual order:

```text
decode → layout/crop → Scene composition → visual effects
       → aperture/geometric calibration → environmental/photometric correction
       → output presentation
```

GTK owns persistent windows and hosts the Renderer's result. Composition and calibration have one owner; geometry is not irreversibly baked into source media. Frame placement/aperture survives equipment replacement, while output mappings and Panel-specific correction may require revalidation. Prefer renderer-level orientation so each Output can be configured independently.

Start calibration with translation, scale, rotation, crop and four-corner projective mapping; add mesh warping only if physical evidence requires it. Design preview/commit/revert separately from ordinary playback and configuration adoption. A known SDR working space and disabled audio are recommended initial simplifications; expanded color and audio behavior require explicit scope decisions.

Keep GTK operations on its main GLib thread, following [PyGObject threading guidance](https://pygobject.gnome.org/guide/threading.html). Downloads, disk validation and conversion must not block it. Qualify the GLib/networking integration, decode-to-GPU path and embedding sink on a pinned build. Two Outputs, overlays and crossfades share the Pi's decoder, graphics and memory budget; advertise a measured whole-Player capacity profile alongside individual Frame profiles.

## Media preparation and cache

Hide [Immich API](https://api.immich.app/) contracts behind a central adapter with a declared tested version range. Periodically refresh active queries into a bounded metadata working set. Source refresh, planning lookahead and cache retention are separate settings. New matching assets affect future uncommitted assignments; query edits follow authored-revision policy.

Check source orientation and quality before preference ranking, then verify the chosen presentation variant. Upscaling cannot make an ineligible original qualify. Reuse qualifying Immich derivatives; permit wall-specific conversion only under the chosen derivative policy. Cache selected originals/variants centrally, with stable content identities and integrity metadata.

Push upstream-neutral manifests and intent through the control channel; let Players pull authorized files from the central show system's gateway over HTTPS, preserving the [central media boundary](requirements.md#central-media-boundary). The gateway resolves upstream identities and supplies bytes itself. Downloads occur ahead of activation, including explicit standby preparation when configured.

Use bounded temporary files with an in-memory cache index and process-local pins. An optional cache directory may survive restart, but every candidate is untrusted until its content-addressed name, exact size, and SHA-256 are revalidated. Never evict a file pinned by current process authority. Release pins when current work no longer needs them; a month-long Run does not pin its history. Cache deletion or corruption invalidates local readiness and triggers reacquisition of the same centrally secured assignment. It never causes reenrollment or a new selection. File acquisition and imminent decoder preparation remain distinct resource budgets.

## Credentials and operational state

Generate a fresh process key and credentials at startup, scoped to current configuration, authorized media, and reporting endpoints. Trusted provisioning observations such as Pi serial or MAC can help central recognize equipment; they are operational matching information, not cryptographic identity. PostgreSQL maps recognized equipment to persistent records and operator-owned Frame bindings. Apply least privilege to the [central media integration](requirements.md#central-media-boundary). Authenticate administrators separately, encrypt network channels, and scope MQTT credentials so a Player cannot issue arbitrary home-control commands.

Maintain desired configuration revisions separately from observed/applied state. A report must not overwrite operator intent. Enrollment, claim/retirement, credential rotation and replacement recovery need an explicit trust workflow. Bindings authorize Outputs to serve persistent Frames; a replaced Player must not regain control using an old instruction.

Keep maintenance, blanking, Panel power, Player shutdown, reboot and updates distinct from authored Scene state. Respect power dependencies and graceful shutdown where required. [Home Assistant's WebSocket API](https://developers.home-assistant.io/docs/api/websocket/) and MQTT are integration options for selected controls and observations; they are not the source of truth. Scene definitions address logical Actuators rather than vendor commands.

## Appliance operation

Implement [automatic Player provisioning](requirements.md#player-provisioning) with one common Ubuntu 24.04.4 Raspberry Pi arm64 image delivered over PXE, following the [accepted platform decision](decisions/0005-native-platform-and-registration-fallback.md). Use the [Raspberry Pi network-boot documentation](https://www.raspberrypi.com/documentation/computers/remote-access.html#network-boot-your-raspberry-pi) to define the supported hardware precondition. The bootstrap obtains a central release ticket, verifies and copies the selected rootfs into RAM, and needs no writable persistent volume.

Boot establishes trusted time and networking, asks central to recognize the equipment observation and select a release, then enrolls a fresh Player session and restores current central bindings/configuration. Unknown equipment remains visible and unbound. Cold boot requires provisioning, release, enrollment, control, and media connectivity. Central consumes candidate trials before ticket issue and promotes only current boot/session health; failed trials reboot and receive the centrally accepted image on the next PXE boot.

Ordinary Scene/media changes preserve windows and valid content or configured fallback, without exposing OS UI or resetting HDMI mode. Recoverable playback failures should preserve the visible composition. A native crash can terminate the whole Player; configure the surviving kiosk host to show black while supervision restarts it. Last-picture retention across that crash would require another mechanism. Test compositor, GPU, power and panel startup behavior separately.

Health must describe presentation, not just a live process: current bindings/revisions, Runs and visible media, readiness, cache pressure, clock uncertainty, scheduled/observed timing, dropped/late frames, decode failures, thermals and recent faults. Keep diagnostics remotely accessible and use physical [validation](validation.md) for output continuity and synchronization claims.

## Component choices

| Responsibility | Starting point | Qualification focus |
|---|---|---|
| Central API and persistence | [FastAPI/Pydantic](https://fastapi.tiangolo.com/features/), [PostgreSQL](https://www.postgresql.org/docs/current/tutorial-transactions.html), Psycopg pooling, [Procrastinate](https://procrastinate.readthedocs.io/). | Domain ownership, atomic task defer, durable execution and restart behavior. |
| Media | [FFmpeg/ffprobe](https://ffmpeg.org/ffprobe.html), [libvips](https://www.libvips.org/). | Exact variant profiles, source fidelity and conversion lead time. |
| Player | [PyGObject](https://pygobject.gnome.org/), [GStreamer](https://gstreamer.freedesktop.org/documentation/), GTK 3. | Independent Outputs, transforms, fades, seeks and capacity; GL facilities remain an interface-bound experiment. |
| Disposable cache | Content-addressed temporary files plus in-memory metadata. | Complete verification, bounded pressure, deletion/corruption recovery, optional byte reuse. |
| OS services | [Weston kiosk shell](https://wayland.pages.freedesktop.org/weston/toc/kiosk-shell.html), [chrony](https://chrony-project.org/), [systemd service reference](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml). | Output routing, clock mapping, watchdogs and boot/crash continuity. |
| Image build | [rpi-image-gen](https://github.com/raspberrypi/rpi-image-gen). | Pinned artifact, provisioning, enrollment and rollback. |

Qualify the exact combination before fixing production media or timing guarantees. Change profiles, composition paths or equipment allocation if measurements require it while preserving the domain and execution contracts.
