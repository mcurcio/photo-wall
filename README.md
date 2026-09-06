# Photo Wall

Photo Wall is a self-hosted system for a room of physically framed displays. Everyday output should feel like a collection of photographs: mostly still images, restrained video, and occasional coordinated effects. A holiday slideshow can evolve throughout December; two portraits can briefly talk to each other; an overlay can reveal the current background when it finishes.

Immich remains the media library. Photo Wall manages physical locations, display calibration, presentation policy, schedules, and compatible media assignments. A **Frame** identifies a location; a replaceable **Player** supplies its output. Scenes can combine explicitly chosen Frame locations and lights while leaving unrelated targets alone. Players prepare upcoming assignments in a bounded rolling window and render locally.

Players must [start through PXE and appear centrally without local setup](docs/requirements.md#player-provisioning). They receive assets exclusively from the central show system and [remain unaware of Immich](docs/requirements.md#central-media-boundary).

## Project status

**MVP implementation in progress.** Central and media worker services launch with PostgreSQL. The full real-media demo passes with two network Players and three simulated Outputs, including live Immich updates and outage/rejoin recovery. Native Linux rendering has separate integration evidence. The signed appliance builder is committed; final image construction, boot and hardware qualification remain incomplete. See the [setup and recovery runbook](docs/runbook.md), [delivery checklist](docs/implementation-checklist.md), and [acceptance evidence](docs/evidence/README.md).

The proposed foundation is one modular central application and a media preparation worker, with one Python Player process embedding GStreamer and GTK on each Raspberry Pi. Weston hosts the display session. Exact builds, rendering capacity, deployment mechanics, and visible synchronization still require qualification.

## Documentation

Start with the [documentation guide](docs/README.md), or choose a topic:

| Document | Purpose |
|---|---|
| [Requirements](docs/requirements.md) | Product behavior, terminology, and scope. |
| [Architecture](docs/architecture.md) | Components, ownership, and platform choices. |
| [Execution contract](docs/execution-contract.md) | Planning, readiness, commitment, timing, and recovery. |
| [Implementation plan](docs/implementation-plan.md) | Bounded slices and their dependencies. |
| [Validation](docs/validation.md) | Scenarios, experiments, and performance evidence. |
| [Design decisions](docs/design-decisions.md) | Open choices and their decision criteria. |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for required engineering principles, recursive module design, agent orchestration, and evidence expectations. The first implementation tracks are central execution with simulated devices and a physical Player prototype driving two panels. They can progress independently and meet at a concrete execution contract. Focused contributions should advance a demonstrable scenario and document what was verified.
