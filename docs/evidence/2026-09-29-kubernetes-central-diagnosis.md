# 2026-09-29 Kubernetes Central: read-only diagnosis

**Evidence class:** live deployment inspection and exact-release source comparison. **Window:** 2026-09-29, approximately 18:05–18:45 UTC. No deployment, database, device, release, or authored-content writes were made. No Player-local journal or physical screen was available. This record omits tokens, serial numbers, private asset identifiers and raw deployment logs.

## What is running

- In the inspected Talos Kubernetes namespace, one ready `photo-wall` pod used Central image `ghcr.io/mcurcio/photo-wall/central:v0.13.0`. A restartable init-container sidecar runs `media-worker:v0.13.0` and is currently running. PostgreSQL has two ready instances.
- Central's `/healthz` reports healthy database, protocol and scheduler. The media operator endpoint reports a recent worker check-in, no worker error, and the `immich-main` connection. The worker is present and currently healthy.
- The global app release is **v0.12.0**, promoted automatically. v0.13.0 is discoverable and deployable but is **not promoted**. A direct read of `/v1/app/manifest` returns v0.12.0. The device's last served PXE base tag is **v0.13.0**. Thus the current boot pairs a v0.13 base and Central with a v0.12 app package. In v0.13's `central/content_catalog/sync.py`, auto-promotion intentionally stops moving once a bound Player and produced package exist; this explains why adding v0.13 to the catalog did not upgrade the global app choice.

## Why the Player appears disconnected

The Central access trace shows a successful PXE locate/base request, a successful v0.12 app manifest/package download, successful enrollment, then repeated `GET /v1/player/time` and `GET /v1/player/state` responses with HTTP 200. The state requests follow the v0.12 session retry delays of roughly 1, 5, 15 and 60 seconds. In the latest one-hour log window, 52 state GETs appear, with **zero** readiness POSTs and **zero** base-health POSTs. Inventory has one bound, enrolled Player and **no accepted `last_report_at`**. The current reachability problem is therefore at the application protocol/reporting layer, not a loss of IP connectivity to Central.

The exact release sources identify a deterministic schema break:

1. `v0.13.0:central/coordination.py` always includes `identify_output` in the `/v1/player/state` response, including `null` when no identification is active; `v0.13.0:central/app.py` returns that payload directly.
2. `v0.12.0:player/service.py` defines `State` with only `configuration`, `plan`, `commits` and `revocations`. Its base `Model` in `v0.12.0:contracts/models.py` sets `extra="forbid"`.
3. The v0.12 service calls `State.model_validate(body)` before starting its control/readiness loops. The additional field raises a validation error and the session retries. This predicts the observed 200/state/retry pattern and absence of readiness reports. The console derives connectivity from accepted app readiness, so it displays the Player as disconnected.

This is the strongest supported cause, with one remaining verification limit: Central does not expose the **actually running** Player package version or Player-local exception. The boot downloaded v0.12, but a Player journal or direct package inspection is needed to confirm the exact installed/running binary and its `extra_forbidden` error. A mixed-version compatibility fixture is needed before closing [bead F0](../production-readiness-v0.13.md#fleet-connectivity-and-upgrades).

## Other live findings

| Finding | Observation and consequence | Next design/qualification owner |
|---|---|---|
| Missing independent base health | `device_base_health` has no rows. The device is recorded as `boot_outcome=failed` for v0.13.0, with no known-good base or pin. v0.12's global app-package path intentionally hands forward no base tag and posts no base-health. v0.13's boot policy can mark a pending base failed on a repeat boot request and, without known-good, serve the same desired tag again. The precise event that marked this device failed is not logged, so a real base failure is unproven. | F1/F2/F6: base-owned heartbeat and boot outcome with explicit unknown versus failed state, bounded recovery. |
| Broad Source is unusable | `all-photos` is `incompatible` with `source_limit` after examining 1,100 items. This is a real instance of the reported cardinality failure, not a theoretical cap. | S0/S1: resumable, bounded discovery of an unbounded logical Source. |
| Current Scene has an invalid upstream reference | The worker knows only `immich-main`. Source `test` points at connection `test` and is `unavailable` with `connection_unknown`; the only authored Scene, `test-scene`, refers to that Source. Source `test2` is healthy with 193 valid items. There are zero Programs, no active contribution or visible content, and the only recorded Run is cancelled. Even with a healthy Player, this configuration currently has nothing scheduled to display. | Operator content repair and S3/X3 UX: validate a connection before saving and show a Scene's broken dependency and first display blocker. No authored change was made here. |
| Output mode is ambiguous | The bound `HDMI-A-1` Output reports `connected=true` but 0×0 mode dimensions; `HDMI-A-2` is disconnected. This may be missing mode telemetry rather than an actual panel failure. The screen and local Player logs must be checked before diagnosing HDMI. | X2/F5: physical mode observation and reporting qualification. |
| Pod readiness is weak | Central's Kubernetes readiness and liveness probes only open TCP port 8000. The worker sidecar has no readiness probe. The current worker is healthy, but pod readiness alone cannot tell whether Central dependencies or media preparation are usable. | X5: HTTP/dependency readiness contract and worker degradation reporting. |
| Backups exist, restore remains unqualified | The `photo-wall-nightly` CloudNativePG scheduled backup's latest 2026-09-29 run completed; both DB instances are ready. This verifies a recent backup object, not restore ability. | X4/F5: preflight and a separately tested restore. |

The v0.13 console has a count-only **Preview matches** action during Source creation and a 15-second **Identify display** action for a pending, unbound Output. Neither shows Source assets nor identifies an already bound Output. The [backlog](../production-readiness-v0.13.md) scopes those UX gaps accordingly.

## Limits and next evidence

The inspection used Kubernetes reads, Central's read-only operator endpoints and a read-only database transaction. It did not prove the Player's actual package version, local validation exception, HDMI pixels, PXE tree bytes or a recovery action. Capture the Player journal/package and physical output; then reproduce the release pair under a mixed-version test. Do not infer production readiness from Central's HTTP 200 or a TCP-ready pod.
