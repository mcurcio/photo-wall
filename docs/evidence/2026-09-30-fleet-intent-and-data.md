# 2026-09-30 Player fleet intent and loader OS data seams

**Evidence class:** local software and PostgreSQL integration, plus read-only Kubernetes resource inspection. **Scope:** the draft [Player fleet PR](../player-fleet-implementation-map.md); no deployment or physical Player qualification. This record does not contain credentials, serials or private media.

## Implemented boundaries

- The operator fleet view can record and cancel a bounded maintenance request against the current device generation, policy revision and exact target artifact. The request freezes tag, digest, size, payload format, base ABI and manifest provenance. It is idempotent by request ID, expires, closes on device retirement and has one queued request per generation. The `dispatched` marker is reserved for a future dispatcher; no attempt, drain, command, stop or accepted artifact follows from the current API. The console distinguishes setting the next desired boot version from recording online maintenance intent.
- A separate loader OS data route module can serve frozen target/fallback bytes and append bounded attempt reports under an injected T1/T2 verifier. Every route response is private and uncached. Central's production composition root has **no verifier and does not mount these routes**. T0 serial claims cannot use them; report contents remain claims rather than accepted app or Output health.
- The shared OS-session guard samples time after acquiring its database lock and rechecks it after later attempt and artifact waits. Monotonic elapsed time prevents a backward UTC step from reviving an expired session. The route and store tests include same-boot, replay, expiry, lock-wait and cross-boot cases.

The [fleet design](../player-fleet-control-design.md) owns the intended behavior and the [red/blue refinement](../player-fleet-red-blue-refinement.md) owns the safety boundary. D14 command trust, D16 bound Runtime withdrawal and D17 rollback compatibility remain open in the [decision register](../design-decisions.md).

## Local checks

| Check | Result | Limit |
|---|---|---|
| Portable `pytest -q` without a database URL | 2,771 passed, 1,118 skipped, 23 warnings | Database, Linux-only and hardware-dependent cases explicitly skipped. |
| Full `pytest -q` with an isolated schema per test on local Compose PostgreSQL | 3,546 passed, 342 skipped, 1 expected failure, 24 warnings | This run overlapped the final review corrections. |
| Focused maintenance-request and OS-data routes against PostgreSQL after the review corrections | 14 passed, 3 warnings | Includes the backward UTC step and uncached error responses. |
| Whole-repository Ruff and six import contracts | Passed | Static checks only. |
| `scripts/check_docs.py` and `git diff --check` | Passed after the evidence edits | Documentation links and whitespace only. |
| Console production Vite build with bundled Node 24 | Passed, 122 modules transformed | Build does not prove the operator flow in a browser or deployed Central. |

`scripts/test_local.py -q` was attempted but this managed PR worktree has no private `.env`, so the script stopped before testing. The full PostgreSQL run instead obtained the local Compose password in process from Docker configuration, supplied `PHOTO_WALL_TEST_DATABASE_URL` only to the test subprocess, and did not print or persist it. The tests used fresh random schemas and left deployment data untouched.

## Read-only deployment observation

On 2026-09-30, `kubectl` context `admin@talos` in namespace `photos` showed one ready Central deployment container using `ghcr.io/mcurcio/photo-wall/central:v0.13.0` (image digest `sha256:3a6f45a2dcdd74ed66d89561f3079263b1a9a874fc6398ce9c7cc8efee3fa88d`). The Cilium Gateway exposed HTTP and HTTPS listeners; the inspected Photo Wall HTTPRoute had no authentication filter, and the NetworkPolicy allowed `10.0.0.0/16` to Central port 8000. These resources do not establish an authenticated T1/T2 loader OS attachment. An upstream access layer could exist and was not inspected.

Deployment history retained rollback ReplicaSets with Central v0.9.1, v0.10.0, v0.12.0 and v0.13.0 images. Their presence is a D17 compatibility concern, not proof that Kubernetes will route traffic to each one. No rollout controller or rollback artifact verifier has been identified in this repository. A fleet command path cannot be activated from a local configuration flag while older serving or rollback binaries might omit active drain and attempt fences.

No Kubernetes mutation, Player package update, PXE reboot or physical Output test occurred. The earlier [live Player diagnosis](2026-09-29-kubernetes-central-diagnosis.md) remains the evidence for the v0.12 Player/v0.13 Central state-schema break; these new resource reads do not confirm the Player's local process or pixels.
