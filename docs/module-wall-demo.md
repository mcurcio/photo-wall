# Full media path demo

Status: the harness has been refactored for central release authority and stateless Players. Its focused tests pass. The `Controller and Player software e2e` workflow now runs the full two-Player/three-Output scenario for each pull-request revision, as two parallel fault-segment jobs, each against its own [fixture](module-immich-fixture.md) started with `--setup-only`, beside the `photo-sources` job that runs the named Immich tests against a third; a passing workflow result is still required before final acceptance evidence is recorded.

The demo joins the real Immich fixture, central PostgreSQL application, Procrastinate media worker, media gateway, two Player processes, and three simulated Outputs. Media conversion, queueing, HTTP/WebSocket traffic, exact bytes, session epochs, cache validation, readiness, commitments, and observations are real. `RecordingRenderer` supplies simulated display actuation, so native GTK/GStreamer and physical HDMI remain separate gates.

This is the software behavior gate. It runs independently of appliance construction so controller, worker, and Player regressions receive direct evidence and faster diagnosis. The appliance workflow separately checks that the signed image assembles, boots, and starts this production Player entry point.

## Isolation and authority

The harness creates a marked private state directory and dedicated Compose project. Players can reach central only. The media worker alone reaches the retained Immich fixture; it holds no connection file and mounts nothing private: since roadmap [4](roadmap.md#4-real-photo-sources) the connection is a row in Central's database, pasted through the API as an owner does on Settings › Photo library. Central alone receives PostgreSQL, operator, release, and queue configuration.

The helper roles:

| Role | Network | Does |
|---|---|---|
| `upstream-tools` | upstream only | Drives the fixture (uploads, deletion, permission loss, outage) and writes the handoff: the fixture's internal URL and runtime key, in the `private` volume |
| `operator` | backend only | The operator's HTTP calls with the admin token: `connect` reads the handoff (read-only) and adds the connection through `POST /v1/operator/library/connections`, keeping only the `connection_ref` Central answers; `source` makes a draft of the demo query and saves it as Source `demo`; `check` asks Check now for the connection and the Source |
| `init` | none (`network_mode: none`) | Runs as root once to give each fresh volume to the runtime user; it writes nothing in `private` |

The demo Source asks for favourites taken in a one-minute window at the run's capture start, photos and videos, and **does not** leave out screenshots: the demo proves the wall path, not exclusions, which `tests/test_source_filters.py` proves. `tests/test_wall_demo.py` pins exactly which services mount `private` (`upstream-tools` read-write, `operator` read-only, `init`) and that the worker mounts nothing private.

Each Player enrolls ticketless by its serial-derived device id, creates fresh enrollment credentials, receives a new authority epoch, and stores only volatile session state. Player containers have only tmpfs for reports and cache; no writable volume is attached. Reports must state `persistence: volatile` and show that central, media, appliance, database, queue, and SQLite modules are absent from the Player image.

The full scenario is wired for two Outputs on one Player and a third on another, exact secured assignments, live source evolution, deletion after security, Player restart, central restart, and per-Output behavior. Its current restart step proves a higher authority epoch and reacquisition after disposable tmpfs loss. Focused Player tests separately prove valid-file reuse without a media request, deletion/corruption reacquisition, and rejection of old-session state. The health probe now exports bounded RTT, offset, delay, drift, and rejection counters from the authenticated `/v1/player/time` path. These paths remain unqualified at demo/image level until the final-revision runs record them.

## Observing source changes

After each upstream mutation, permission change, or network fault and recovery, the operator role's `check` asks Check now for the connection and then for the Source ([connection and pool checks](module-media-worker.md#connection-and-pool-checks)), and answers the connection's health and the Source's check as Central served them just before. The harness waits until the Source's check differs from that one (its state, `started_at` or `at`) and has ended as the step expects, then requires the scenario's media and assignment observations. A fault step needs the check to end `failed` with `connection` and the connection's health to have changed to the fault's problem (`missing_permissions` or `unreachable`); recovery needs a check ending `complete` and the health `ok`. While a fault holds, the pool must keep every photo and every Player must keep presenting content (C7: the last pool keeps playing).

Only values Central served are compared: a wall-clock delay, an older check, or another Source's check cannot satisfy this boundary. Periodic checks stay on for normal live evolution. The harness does not call the upstream adapter directly, force pool state, or mark a check finished. Reports keep each `check` answer beside the mutation or fault evidence, so the outcome is tied to the action that needed it. `tests/test_wall_demo.py::test_the_demo_operator_drives_centrals_photo_routes_end_to_end` runs the operator role's own actions against Central over a real database; the whole wall is CI's software e2e.

## Reproducing the current checkpoint

Build the node component set (its `app.squashfs` is the Player's app root; [Debian packaging module](module-debian-packaging.md), CI's `node-components` job), the central image and the media-worker image from the same clean committed revision. The Players run from the app root, imported into their containers. Image construction explicitly uses the daemon `default` builder with `--load`, allowing derived fixtures to reuse locally loaded parent images.

```sh
.venv/bin/python scripts/demo_wall.py run \
  --state-dir /absolute/new-wall-demo \
  --immich-state /absolute/retained-immich-fixture \
  --app-image /absolute/node-components/app.squashfs \
  --revision <final-40-character-revision> \
  --central-image sha256:<exact-central-image-id> \
  --worker-image sha256:<exact-worker-image-id> \
  --scenario full --keep
```

`--scenario full` runs every fault segment in order against one wall: `upstream-faults` (live membership, deletion after security, permission loss, upstream outage) then `central-player-faults` (Central outage past every held lease, Central restart, Player restart). Each segment is also a scenario of its own, after the same setup and baseline; the software e2e workflow runs the two as parallel jobs. To keep those waits short, the demo's Central plans a 15 s horizon renewed every 10 s (a deployment: 300 s and 30 s), so a held lease ends 15-25 s ahead, and the Player runner retries a lost Central within 5 s rather than backing off to 60 s.

Preflight rejects a dirty source tree, revision mismatch, app image revision stamp mismatch (`revision.json` beside the image), or a reference in `components.json` that does not match the image, mutable image tag, missing paired image ID, reused state directory, or unverified fixture. This means an uncommitted workspace cannot produce final evidence. The selected revision, image IDs, source inventory, app image reference, media hashes, session epochs, observations, and phase results are retained in the private report.

The wall helper is assembled from an explicit dependency bundle that preserves Python import layout and records every copied file digest. Its container-side Immich client does not import the host Docker driver or diagnostic recorder. Setup journals `running`, `passed`, or `failed` before and after each image-build, volume, upstream, Central, connection, Source, check, and runtime operation. A failure retains a bounded schema with `phase`, `role`, `action`, and `code`; cleanup has its own failure field and cannot erase the primary operation. The same safe envelope is printed by failing helper roles, while credentials and arbitrary exception text remain private.

Runtime source collection executes a bundled Python module independently in both Central and worker containers. A shared strict result contract validates the application Python and SQL source inventory, its aggregate hash, and the adapter, preparer, and helper hashes before emission and again on the host. The collector hashes every declared helper file, including its own code and result model; changed, missing, undeclared, or unsafe helper sources fail the audit. The host compares both containers against the local source inventory and staged bundle. The `source_audit` phase journals collection or validation failures separately from runtime startup.

`status --state-dir ABS` reads a marked run. `cleanup --state-dir ABS` removes only resources journaled by that run. Omitting `--keep` attempts scoped cleanup automatically and preserves evidence if cleanup cannot complete.

## Qualification limits

The full configuration has explicit CPU, memory, transfer, and time bounds. A passing run qualifies the central/worker/Player network integration with simulated actuation. It does not qualify the rebuilt Pi image, native rendering, PXE, replacement, dual HDMI, thermal behavior, or visible coordination. Those results must be recorded separately and tied to the same final revision.

Standalone harness checks run with `.venv/bin/python -m pytest --noconftest tests/test_wall_demo.py -q`.
