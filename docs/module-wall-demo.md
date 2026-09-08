# Full media path demo

Status: the harness has been refactored for central release authority and stateless Players. Its focused tests pass. The `Controller and Player software e2e` workflow now runs the full two-Player/three-Output scenario for each pull-request revision; a passing workflow result is still required before final acceptance evidence is recorded.

The demo joins the real Immich fixture, central PostgreSQL application, Procrastinate media worker, media gateway, two Player processes, and three simulated Outputs. Media conversion, queueing, HTTP/WebSocket traffic, exact bytes, session epochs, cache validation, readiness, commitments, and observations are real. `RecordingRenderer` supplies simulated display actuation, so native GTK/GStreamer and physical HDMI remain separate gates.

This is the software behavior gate. It runs independently of appliance construction so controller, worker, and Player regressions receive direct evidence and faster diagnosis. The appliance workflow separately checks that the signed image assembles, boots, and starts this production Player entry point.

## Isolation and authority

The harness creates a marked private state directory and dedicated Compose project. Players can reach central only. The media worker alone reaches the retained Immich fixture and holds its private connection file. Central alone receives PostgreSQL, operator, release, and queue configuration.

Each Player obtains a central boot ticket, creates fresh enrollment credentials, receives a new authority epoch, and stores only volatile session state. Player containers have only tmpfs for reports and cache; no writable volume is attached. Reports must state `persistence: volatile`, prove the selected release was accepted centrally, and show that central, media, appliance, database, queue, and SQLite modules are absent from the Player image.

The full scenario is wired for two Outputs on one Player and a third on another, exact secured assignments, live source evolution, deletion after security, Player restart, central restart, and per-Output behavior. Its current restart step proves a higher authority epoch and reacquisition after disposable tmpfs loss. Focused Player tests separately prove valid-file reuse without a media request, deletion/corruption reacquisition, and rejection of old-session state. The health probe now exports bounded RTT, offset, delay, drift, and rejection counters from the authenticated `/v1/player/time` path. These paths remain unqualified at demo/image level until the final-revision runs record them.

## Observing source changes

After initial source configuration, upstream mutation, permission changes, or network fault/recovery, the operator helper requests a [source refresh](module-media-worker.md#source-refresh-requests) through the authenticated API and retains its receipt. The harness waits for that same Source's `refresh_completed_revision` to reach or exceed the receipt's `requested_revision`, then requires the expected source status and the scenario's media/assignment observations. Completion and success are separate: a permission-denial step must observe a completed request with `permission`, while recovery requires a new completed request with `ok`.

A wall-clock delay, an old successful snapshot, or another Source's completion cannot satisfy this boundary. Periodic refresh remains enabled for normal live evolution and may satisfy a request through the shared repository lease. The harness does not call the upstream adapter directly, force catalog state, or mark a request complete. Reports retain request receipts beside the mutation/fault evidence so the outcome can be tied to the action that required fresh observation.

## Reproducing the current checkpoint

Build a Player-only wheelhouse, central image, and media-worker image from the same clean committed revision. Image construction explicitly uses the daemon `default` builder with `--load`, allowing derived fixtures to reuse locally loaded parent images.

```sh
.venv/bin/python scripts/demo_wall.py run \
  --state-dir /absolute/new-wall-demo \
  --immich-state /absolute/retained-immich-fixture \
  --wheelhouse /absolute/player-wheelhouse \
  --revision <final-40-character-revision> \
  --central-image sha256:<exact-central-image-id> \
  --worker-image sha256:<exact-worker-image-id> \
  --scenario full --keep
```

Preflight rejects a dirty source tree, revision mismatch, Player inventory mismatch, mutable image tag, missing paired image ID, reused state directory, or unverified fixture. This means an uncommitted workspace cannot produce final evidence. The selected revision, image IDs, source inventory, wheel inventory, media hashes, session epochs, observations, and phase results are retained in the private report.

The wall helper is assembled from an explicit dependency bundle that preserves Python import layout and records every copied file digest. Its container-side Immich client does not import the host Docker driver or diagnostic recorder. Setup journals `running`, `passed`, or `failed` before and after each image-build, volume, upstream, Central, Source, refresh, and runtime operation. A failure retains a bounded schema with `phase`, `role`, `action`, and `code`; cleanup has its own failure field and cannot erase the primary operation. The same safe envelope is printed by failing helper roles, while credentials and arbitrary exception text remain private.

Runtime source collection executes a bundled Python module independently in both Central and worker containers. A shared strict result contract validates the application Python and SQL source inventory, its aggregate hash, and the adapter, preparer, and helper hashes before emission and again on the host. The collector hashes every declared helper file, including its own code and result model; changed, missing, undeclared, or unsafe helper sources fail the audit. The host compares both containers against the local source inventory and staged bundle. The `source_audit` phase journals collection or validation failures separately from runtime startup.

`status --state-dir ABS` reads a marked run. `cleanup --state-dir ABS` removes only resources journaled by that run. Omitting `--keep` attempts scoped cleanup automatically and preserves evidence if cleanup cannot complete.

## Qualification limits

The full configuration has explicit CPU, memory, transfer, and time bounds. A passing run qualifies the central/worker/Player network integration with simulated actuation. It does not qualify the rebuilt Pi image, native rendering, PXE, replacement, dual HDMI, thermal behavior, or visible coordination. Those results must be recorded separately and tied to the same final revision.

Standalone harness checks run with `.venv/bin/python -m pytest --noconftest tests/test_wall_demo.py -q`.
