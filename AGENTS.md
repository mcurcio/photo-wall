# Agent guidance

Read [README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md), and the [documentation guide](docs/README.md) before changing the project. Use the owning document for each concern: [requirements](docs/requirements.md), [architecture](docs/architecture.md), [execution contract](docs/execution-contract.md), [implementation plan](docs/implementation-plan.md), [validation](docs/validation.md), and [design decisions](docs/design-decisions.md).

The project is at the design stage, without application setup, build, or test commands. Add and verify them with implementation tooling; do not invent successful runs or hardware results.

## Required development approach

**DRY and SOLID take precedence over YAGNI.** Follow the canonical [design principles](CONTRIBUTING.md#design-principles); YAGNI cannot justify duplicate logic, broken boundaries, or missing required interfaces.

The top-level agent **must act as orchestrator**, owning planning, architecture, dependencies, delegation, integration/review, and final evidence. Follow [recursive development and agent orchestration](CONTRIBUTING.md#recursive-development-and-agent-orchestration): design each module first, delegate bounded leaves or recursive child design, and integrate/review upward.

Follow the [subagent model policy](CONTRIBUTING.md#subagent-model-selection): resolve supported identifiers, prefer Spark for suitable leaves, use capable agents for design/review, and disclose fallbacks. Quality takes priority over usage-pool preference.

## Preserve product behavior

- Frames are persistent locations; Players and Panels are replaceable equipment.
- Scenes own sources and presentation configuration. Programs schedule activation; Runs are executions.
- All affected Frames and Actuators participate explicitly. Omitted targets receive no implied command.
- Hard compatibility precedes preferences, including authored assignments. Empty pools do not relax eligibility.
- Live query results evolve; scheduled and secured assignments retain their content for that execution.
- Covered Runs advance logically and reveal current state. Natural completion and downward-only cancellation differ.
- Operational equipment state remains distinct from authored content.
- [Plug-and-play PXE provisioning](docs/requirements.md#player-provisioning) brings a Player into the central system without local setup.
- [Players are Immich-unaware](docs/requirements.md#central-media-boundary) and obtain media exclusively from the central Photo Wall service.

## Implement and verify

Follow the proposed modular central application, media worker, and single-process Python/GStreamer/GTK Player direction unless evidence supports a change. Keep future projection free of current effects. Distinguish acquired files, playback readiness, capacity, commitment, and observed output; route failures to planning and lifecycle ownership.

Use controllable-clock simulation for central semantics and physical equipment for rendering, continuity, and visible timing. Run relevant checks and report their limits. Update owning documents with consequential choices and evidence.

Keep credentials and private media out of source and fixtures. Use relative documentation links, primary technical citations, and accurate requirement, proposal, and qualification status. Avoid duplicating policies across documents.
