# 2026-09-29 Show now Source freshness and Player Central-link diagnostic

Show now now presents Central's latest completed catalog status for the live
Sources used by the selected Scene on both Scene and Review steps. A non-healthy
Source can be refreshed from this view. The response distinguishes an accepted
refresh request from worker completion; a `202` receipt does not say the catalog
is current, media is prepared for a Frame, or anything is visible. Historical
Sources absent from the current catalog have no current status or refresh
action. Hand-picked Scene media is identified separately because refreshing a
Source does not change those saved choices. These notices are advisory and do
not gate Show now.

The local Player diagnostic labels its Central control link as `connecting`,
`reachable`, or `retrying`. The retrying label says whether this Player process
has received configuration before. PlayerService owns this state and the
Renderer displays it only on connected unbound Outputs. It describes the last
observed control exchange, not boot health, current readiness, or visible
content. Bound content is unaffected.

## Local verification

Focused checks on the dirty working tree based on `b308838` reported:

- Player link diagnostic tests: **6 passed**; Ruff passed.
- Show now Node-backed test: **1 passed**; production Vite build passed.
- Focused Show now Chromium tests: **3 passed, 16 deselected**.
- Existing Scene flow refresh Chromium regression: **2 passed, 29 deselected**.
- Ruff and diff checks passed.

The browser tests used the local operator test server and PostgreSQL with
synthetic equipment/catalog fixtures. They cover the acceptance receipt, stale
and missing Source handling, authored-media wording, and draft-scoped feedback.
They do not run the production media worker or a real upstream photo library.

## Qualification limits

The link state and screen diagnostic were exercised in software tests only. No
physical Pi or HDMI screen was used, and no Kubernetes deployment was made.
These results do not qualify boot health, Player readiness, media preparation,
visible panel output, or hardware behavior.
