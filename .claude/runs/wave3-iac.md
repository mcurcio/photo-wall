# Wave 3: the iac changes for Central's hub (for the owner to merge)

**What this is.** The exact Kubernetes changes `mcurcio/iac` needs so production runs the Node bus hub beside Central (E3d). Nothing here has been applied to a cluster. The owner merges the iac PR; an earlier owner answer holds the production hub until E4, so the PR is opened only when the orchestrator says so.
**Source.** `.claude/runs/wave3-e3d.md` § "Kubernetes change", corrected by errata E-E3D-CC1-2, CC1-5, CC2-3, CC2-4 and FIX-1 (`grep -a 'E-E3D-' .claude/errata.md`). The iac facts below were read from the local iac worktree `iac-worktrees/bump-photo-wall-v0.17.0` (2026-10-04), not from iac `main`; re-check each one against `main` when the PR is cut.
**Workload.** `workloads/photo_wall/__init__.py`: one pod, `replicas=1`, `Recreate`; `central` is the main container, `worker` a native sidecar. Production runs the node factory (`central.node_app:create_app`) through a `command` override.

## The changes

1. **Volume.** Add an `emptyDir` named `hub-config`. Nothing in it needs to persist: the hub starts empty at every pod start (E3b design §12).
2. **Hub container.** Add a native sidecar `hub`, ordered **after** `worker`:
   - image `nats:2.15.0-alpine@sha256:ac8f88a6494bffc2c2a5289a0ca61cb28a9145c11ba5677cf24265d07f46d8d4` (the digest `compose.yaml` pins);
   - command `["sh", "-c", "until [ -s /var/lib/photo-wall/hub/hub.json ]; do sleep 1; done; exec nats-server -c /var/lib/photo-wall/hub/hub.json"]`;
   - mounts `hub-config` **read-only** at `/var/lib/photo-wall/hub`, and an `emptyDir` at `/tmp` (nats-server creates `$TMPDIR/nats` even on the memory store, E-E3B-S3-1);
   - read-only root filesystem, no privilege escalation, all capabilities dropped; the pod's default uid 10001 is fine;
   - memory limit `256Mi`;
   - **no startup probe.** A native sidecar's startup probe holds every container after it, so it would hold `central` (and Pi boot fetches) until the worker has migrated and written its first file;
   - liveness `httpGet: {path: /healthz, port: 8222}` (no `host`: the kubelet dials the pod IP, and the monitor binds `0.0.0.0`; E-E3D-CC1-5), with `initialDelaySeconds: 120` so the wait for the worker's first file is not counted (Compose's `start_period: 120s`);
   - no Service port. The pod is not `hostNetwork` (the workload sets none), so the hub's client (4222), WebSocket (8080) and monitor (8222) listeners are reachable on the pod IP only, never on the LAN VIP.
3. **Worker.** Mount `hub-config` **read-write** at `/var/lib/photo-wall/hub` (an `emptyDir` is world-writable; the worker drops to uid 10001). Add env `PHOTO_WALL_HUB_URL=nats://127.0.0.1:4222` and `PHOTO_WALL_HUB_CONFIG=/var/lib/photo-wall/hub/hub.json`.
4. **Central.**
   - Add env `PHOTO_WALL_HUB_LEAF_URL=ws://127.0.0.1:8080/leafnode`.
   - **Change `--ws-max-size` in `node_app_command` from `1048576` to `16777216`** (E-E3D-CC1-2, CC2-3). The image's own `CMD` already carries 16 MiB, but the workload's `command` override replaces it. A Node's leaf rides this WebSocket, and nats-server sends a connection's whole backlog as one frame.
   - No new port and no new route: a Node's leaf arrives on Central's existing origin at `/photo-wall/bus/leafnode` as a WebSocket upgrade (E-E3D-FIX-1).
5. **Images.** Bump `VERSION_TAG` to the first release cut after this branch merges (it adds `nodeapi` and nats-py to the Central and media-worker images, and the `hub-config` directory to the worker image).

## The leaf's path in production (E-E3D-CC2-4)

A Node's leaf dials the origin its boot **located** (`appliance/netboot_init.py` writes `located.origin` into the handoff), not the cmdline root. In the snapshot, the staged cmdline root is `http://photo-wall/` (`stacks/home/prod/photos/stack.py:457`), and the runbook says stage 1 follows the Gateway's redirect to https. So the located origin is most likely `https://photo-wall.home.mcurcio.com`, and the leaf dials `wss://…:443/photo-wall/bus/leafnode` through the Envoy Gateway's HTTPS route to Central's :8000.

- **Scheme.** wss, with TLS ended at the Gateway. E3c proved a wss leaf through a TLS proxy (E3c S3). No change is needed.
- **Redirects.** The located origin is already the https host, so the leaf's dial does not meet the http→https redirect. Whether nats-server's leaf dial follows a redirect is unprobed, so keep `/photo-wall/bus/leafnode` out of any `path_redirects`.
- **Route timeout. Recommended change:** pass `request_timeout="0s"` on the photo-wall `WorkloadGateway`, as the guppi gateway does for its `/ws` route (`lib/k8s/components/gateway.py`). Without it, Envoy's per-route request timeout can sever the long-lived leaf. The leaf redials and recovers, but every cut makes presence flap and leaves the Node's WALL mirror up to about 60 s stale.
- **First INFO within 1 s.** The leaf's upgrade, Central's dial of the hub and the hub's INFO must all finish within the remote's `first_info_timeout` (1 s) or the leaf redials (E-E3D-S3-3). That holds easily on the LAN. If the bench shows redials, E3c sets `first_info_timeout` on the remote; iac changes nothing.
- **Verify after the deploy.** On a V2 Node, `cat /run/photo-wall-node/bus.env` shows the leaf URL it dials. The Player page should read "Node API link: linked". If it says "not linked", check `GET http://127.0.0.1:8222/leafz` from inside the pod.

## What the owner gives up

- Every Central deploy restarts the hub (same pod, `Recreate`): leaves relink in about a second, and mirrors lag up to about 60 s. This is expected and self-recovering.
- Every leaf byte crosses Central's Python process, and a Central restart drops every leaf for about a second.
- `node_link_records` and `node_link_actions` grow without bound until E5 sets their retention. Nothing is recorded until Nodes run the bus.
- The worker and Central must stay in one pod. Presence compares the worker's look time with Central's read time, and those are one host's clock only while they share a host (E-E3D-S4-3).
