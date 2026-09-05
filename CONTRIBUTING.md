# Contributing to Photo Wall

Photo Wall is at the design stage. Contributions should connect a bounded behavior to an implementation and evidence that it works. The [requirements](docs/requirements.md) define the product; the [implementation plan](docs/implementation-plan.md) identifies useful slices. Central simulation and physical Player qualification can proceed in parallel.

## Get oriented

```sh
git clone https://github.com/mcurcio/photo-wall.git
cd photo-wall
```

Read the [documentation guide](docs/README.md), then the requirements and documents relevant to the change. There are no application setup, launch, build, or test commands yet. The first implementation contribution should establish real commands with pinned dependencies and verify them from a clean checkout. Add configuration examples without secrets, and include storage and migration instructions when persistence is introduced.

## Design principles

**DRY and SOLID are fundamental and take precedence over YAGNI.** Apply them to the design, implementation, tests, and documentation:

| Principle | Required practice |
|---|---|
| **DRY — Don't Repeat Yourself** | Give domain knowledge and behavior one authoritative representation. Reuse domain behavior through its owning module instead of implementing the same rules in several places. |
| **Single responsibility** | Give each module a coherent responsibility and reason to change; separate domain policy, coordination, and platform adaptation. |
| **Open/closed** | Support extension through stable interfaces without requiring changes to unrelated modules. |
| **Liskov substitution** | Alternative implementations, including simulators, must honor the contracts and invariants clients depend on. |
| **Interface segregation** | Expose small interfaces shaped around each client's needs rather than forcing every client to depend on a broad interface. |
| **Dependency inversion** | Direct dependencies toward abstractions. Domain policy defines the contracts; transport, storage, rendering, and equipment adapters implement them. |

Reuse does not mean coupling otherwise independent modules or sharing mutable state indiscriminately. An abstraction need not be a class, and these principles do not require one class per abstraction. YAGNI can remove unused features and speculative generality; it cannot justify duplicate logic, violated responsibility boundaries, or omitted interfaces required by the current architecture.

## Recursive development and agent orchestration

Architect each system or module **before implementing it**. Its design must define responsibility, owned state, input/output contracts, dependencies, invariants and failure behavior, and acceptance checks. Begin with a bounded scenario from the implementation plan and [validation guide](docs/validation.md), including its observable outcome. Record consequential choices in [design decisions](docs/design-decisions.md); experiments may use explicit provisional assumptions.

For agent-led development, the top-level agent must act as the orchestrator. It owns planning, system architecture, dependency coordination, delegation, integration and review, and the final evidence-backed result. Delegate suitable design, implementation, test, documentation, and review work to subagents. A subagent responsible for a complex module may orchestrate its own children using the same process.

1. **Design the current scope.** Establish its contract and map the responsibilities of its child modules before delegating implementation. Identify shared behavior and its authoritative owner so children do not create competing implementations.
2. **Inspect each child.** Assess scope, complexity, dependencies, and unresolved decisions. Delegate bounded leaf implementation when its contract is clear. If it needs further architecture, delegate child design and decomposition, then repeat this assessment recursively. Stop at a meaningful module boundary with an implementable contract and clear acceptance checks; there is no fixed recursion depth.
3. **Give a complete handoff.** Include project context and document links, the objective, owned files, interfaces, constraints, acceptance criteria and checks, and the limits of the delegated scope. Assign disjoint write ownership to parallel siblings and coordinate changes to shared contracts. Do not have siblings edit the same files or independently implement the same behavior.
4. **Implement and verify each leaf.** Connect its required path with a simulator or real adapter. Keep future projection free of current effects and return execution outcomes to lifecycle ownership. Return the outputs and changed files, decisions made, check results, remaining limits, and any contract issue requiring coordination.
5. **Integrate and review from children to parents.** Check each return against its contract, inspect the changes, run the relevant integration checks, and resolve incompatible assumptions before combining dependent work. Repeat upward until the full scenario works. The top-level orchestrator owns the final review and reports what was verified, failures or untested conditions, and the evidence supporting completion.

Update the owning documents as contracts and implementation evolve. Add verified setup/run/check commands and record results without presenting proposed or unqualified capabilities as complete. Delegation changes who performs the work; it does not remove the orchestrator's responsibility for correctness.

### Subagent model selection

The requested project preference is **`codex-spark-5.3-flash`** for suitable lightweight subagent work. OpenAI documents Spark's model identifier as **`gpt-5.3-codex-spark`**; the documentation does not establish an alias mapping for the requested spelling. [OpenAI models](https://learn.chatgpt.com/docs/models).

Resolve the usable identifier from the actual subagent runtime's supported models before dispatch. Do not pass the requested spelling automatically or invent an unsupported name. Prefer available Spark for clear, bounded leaf implementation, mechanical refactors, focused tests, and documentation work. If Spark is unavailable, select a supported model suited to the task and disclose the fallback in the work report.

Use a more capable design or review agent for ambiguous contracts and reasoning across modules. Correctness, architecture, and review quality take priority over using a particular usage pool. Spark has its own usage limits during its research preview; verify current availability and limits rather than assuming they apply to every runtime or account. [OpenAI Spark guidance](https://learn.chatgpt.com/docs/agent-configuration/speed#codex-spark).

## Suggested code organization

Introduce these directories as their code becomes necessary. This layout is a proposal; it does not require a separately deployed service for each directory.

```text
central/       # Installation, definitions, Runtime, Planner, sessions, adapters
player/        # Equipment, networking, cache, executor, renderer
media/         # Preparation worker and variant/delivery support
contracts/     # Versioned execution/API contracts and shared fixtures
appliance/     # Image build, OS services, provisioning, recovery
tests/         # Domain, contract, integration, qualification harnesses
docs/          # Requirements, design, decisions, implementation, evidence
```

Keep domain and time logic independent of GStreamer, GTK, Immich transport, and hardware APIs. The Renderer owns composition and calibration; GTK hosts presentation surfaces. Keep all Player application modules within the proposed single-process design. Record central tooling and packaging choices before treating this layout as a fixed package structure.

Preserve two runtime boundaries in every implementation:

- [Player provisioning](docs/requirements.md#player-provisioning) is plug-and-play PXE: a Player must appear in the central system without local setup.
- [The central media boundary](docs/requirements.md#central-media-boundary) keeps Players unaware of Immich. Players obtain media exclusively through the central Photo Wall service; Immich-specific integration belongs centrally.

## Choose the right evidence

| Environment | Useful checks | Limits |
|---|---|---|
| Development computer | Controllable-clock runtime tests, compatibility fixtures, planning, protocol faults, persistence, simulated Players and Actuators. | Cannot establish HDMI timing, GPU capacity, or boot continuity. |
| Real Immich integration environment | Queries, permissions, metadata, acquisition, variants, live changes, deletion, and outages. | Results depend on the declared Immich version and query scope. |
| Raspberry Pi with two representative panels; later a second Player | Independent Outputs, calibration, transitions, covered-video reveal, resource limits, crashes, and visible synchronization. | Results apply to the recorded hardware, software, modes, and media profiles. |

Keep portable checks runnable without a Pi and establish CI when tooling exists. Hardware checks must declare their bench requirements and produce measurements. Record build/revision, package versions, Pi/panel models, display modes, media profiles, network/storage conditions, procedure, resource use, failures, and conclusions. Distinguish clock health from visible timing, and proposed thresholds from accepted pass criteria. The validation guide owns detailed scenarios and budgets.

## Fixtures and credentials

Use synthetic media or fixtures that can be redistributed publicly, with attribution and licensing information where applicable. Include representative eligible and ineligible assets, malformed inputs, and failure cases. Remove personal metadata and use example identifiers instead of private library records. Keep fixtures small enough for their purpose and document any separately obtained test media.

Keep credentials, tokens, private photos, and sensitive server responses out of source, fixtures, logs, screenshots, and shared appliance images. Supply integration credentials through runtime configuration. Sanitize evidence before including it in a pull request.

## Pull requests and documentation

Keep pull requests focused on a coherent scenario or correction. Explain the resulting behavior, relevant verification, and remaining limitations; link the affected decision or validation record. Add meaningful tests for behavior and failure boundaries, and avoid claiming checks that were not run.

Use stable repository-relative links between project documents. Put each policy in its owning document and link to it elsewhere. Cite primary documentation for technical component claims and identify tested versions. Check links and preview Mermaid diagrams after editing them. Keep requirements, proposals, open decisions, and measured results clearly labeled; update affected documents together when a choice changes.
