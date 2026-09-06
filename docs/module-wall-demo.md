# Full media path demo

Status: implemented harness with a passed full integration benchmark. It joins the [real Immich fixture](module-immich-fixture.md), [media worker](module-media-worker.md), [gateway](module-media-gateway.md) and [Player service](module-player-service.md). Media conversion, PostgreSQL, HTTP/WebSocket traffic and cache bytes are real; `RecordingRenderer` supplies simulated actuation. See the [dated evidence](evidence/2026-09-05-full-wall.md) for results and limits.

## Contract and isolation

The harness creates a new marked, private state directory and a dedicated `pw-wall-demo-<random>` project. Its internal wall network contains only Players and central; a separate internal backend contains central, PostgreSQL, worker and operator. Only the worker joins the retained fixture's upstream network. Central forwarding is disabled. No service publishes a host port, and Docker uses copied contexts and named volumes with `nocopy`, avoiding host bind mounts.

An upstream-only helper creates a separate read-only API key and synthetic assets in a unique past capture interval. It verifies extracted timestamps before enabling the source. Only the worker receives the private connection file. The Player image contains the locked Player-only wheel closure and a source-neutral recorder runner; it receives no upstream key, operator credential, central/media module or complete harness. Temporary diagnostic processes separately verify denied DNS and numeric TCP access to Immich while central remains reachable.

The baseline uses one Player and one Output. The full scenario uses two Outputs on one Player and a third on another, eight-second cycles, a 15-second preparation horizon and real UTC within a ten-minute scenario ceiling. Live uploads and favorite changes affect future assignments; secured exact bytes remain locked. Faults affect only this demo's key, worker connection, central container and Player container. The retained fixture and main deployment are preserved.

## Reproducing the checkpoint

The benchmark uses core revision `dda8e98c5c54dc8ca9c007599f8a919eadbd5248`. First prepare the [real Immich fixture](module-immich-fixture.md) and build the [Player-only wheelhouse](module-player-package.md) at that revision. The harness rejects core source drift and a different Player revision. It runs from a checkout with those same central/media/contracts/Player files; later appliance and harness changes do not change that core.

Build central and worker from that checkout, then pass their exact image IDs. The optional overrides must be supplied together. Defaults retain the original benchmark's local IDs; overriding them lets a clean machine use its own equivalent builds without editing source.

```sh
docker build --target central -t photo-wall-demo-core-central .
docker build --target media-worker -t photo-wall-demo-core-worker .
demo_central_id=$(docker image inspect --format '{{.Id}}' photo-wall-demo-core-central)
demo_worker_id=$(docker image inspect --format '{{.Id}}' photo-wall-demo-core-worker)
.venv/bin/python scripts/demo_wall.py run \
  --state-dir /absolute/new-wall-demo \
  --immich-state /absolute/retained-immich-fixture \
  --wheelhouse /absolute/player-wheelhouse \
  --central-image "$demo_central_id" --worker-image "$demo_worker_id" \
  --scenario full --keep
```

Replace the three absolute paths with prepared inputs and a new output directory. Use `--scenario baseline` for the first slice. Both image source inventories and their copied harness hashes are verified at runtime. The immutable image IDs, source hashes, wheel inventory, media hashes, observations and fault phases are recorded in private evidence. Configure Docker's normal client environment for the host; this task's Desktop uses an isolated public client configuration and its explicit socket.

`status --state-dir ABS` reads a marked run. `cleanup --state-dir ABS` removes only that run's journaled assets/key and deployment resources, preserving evidence. Omitting `--keep` attempts cleanup automatically; an upstream cleanup failure preserves the journal for a scoped retry. Retained demo assets share the synthetic account, so clean those demos before rerunning the original fixture's exact all-favorites membership verifier.

## Resource and test boundaries

The full configuration caps worker at 768 MiB, central at 384 MiB, PostgreSQL at 192 MiB and each Player at 192 MiB, each with one CPU. Operator and upstream helpers have separate 128 and 256 MiB ceilings and run sequentially. These are fixture limits, not qualified Pi capacity. The strict 100 ms clock gate remains active; shared-host CPU contention produced correctly rejected samples in earlier attempts.

The full passing run used a 70-second live-presentation wait. The harness now allows 120 seconds for that phase to accommodate held assignments, a four-member rotation and a safely skipped cue; the exact-byte, complete-group and clock predicates are unchanged. That budget change has focused coverage and is not described as another full run. A transient connection refusal while central restarts is retried within the phase deadline; authority, schema and unknown failures are not hidden as startup retries.

Standalone checks run with `.venv/bin/python -m pytest --noconftest tests/test_wall_demo.py -q`. They need no PostgreSQL fixture and cover role containment, complete-group evidence, lock preservation, per-Output outage/recovery, retry classification, immutable image overrides and complete source inventories. Native GTK/GStreamer/HDMI and accelerated calendar/nested-Scene tests remain separate evidence classes.
