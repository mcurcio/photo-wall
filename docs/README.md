# Documentation guide

Photo Wall separates product behavior, implementation design, delivery work, and verification. Start with the requirements, then read the documents relevant to the change. The project is implementing its first MVP; a described component or experiment is not evidence that it has been implemented or qualified.

Implementation status and commands: [delivery checklist](implementation-checklist.md), [runbook](runbook.md), and [evidence](evidence/README.md).

## Reading order and ownership

| Document | Owns | Read it to |
|---|---|---|
| [Requirements](requirements.md) | Canonical terminology, product behavior, and scope boundaries. | Understand Frames, Players, Scenes, Runs, and the invariants implementation must preserve. |
| [Architecture](architecture.md) | Proposed components, module responsibilities, deployment, and platform rationale. | Locate a change and understand central/local boundaries. |
| [Execution contract](execution-contract.md) | Proposed agreement between Runtime, Planner, and execution components. | Design preparation, commitment, timing, ownership, failure handling, and recovery. |
| [Implementation plan](implementation-plan.md) | Recommended work sequence, dependencies, and bounded outcomes. | Choose a demonstrable slice and the decisions it needs. |
| [Validation](validation.md) | Acceptance scenarios, qualification methods, evidence requirements, and numerical budgets. | Determine what a test or experiment can establish. |
| [Design decisions](design-decisions.md) | Unresolved choices, alternatives, and criteria for resolving them. | Record a consequential policy or platform choice before dependent work relies on it. |

The [contribution guide](../CONTRIBUTING.md) owns the mandatory engineering principles and [recursive development and agent orchestration](../CONTRIBUTING.md#recursive-development-and-agent-orchestration), alongside repository setup, development environments, fixtures, and pull requests. These development requirements apply to every implementation slice; keep the detailed process there rather than repeating it in component designs.

## Status and maintenance

Requirements define intended behavior. Architecture and execution proposals explain how to realize it; a proposal can change with evidence while preserving the requirements. Open choices need an explicit decision before the affected contract is fixed. Qualification results establish only the configurations and conditions actually tested.

Numerical budgets in validation retain their stated status. An illustrative preparation horizon is not an outage guarantee, and synchronized clocks do not prove simultaneous visible output. Link a claim to its measurement or test record.

Update the document that owns a concern, then adjust links and summaries elsewhere. Record requirement changes explicitly. Keep detailed procedures and evidence out of the requirements, and avoid copying complete scenarios or decision tables across pages.
