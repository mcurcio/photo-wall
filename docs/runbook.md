# Development setup and recovery

Status: central, PostgreSQL, and the Procrastinate media worker launch together. Earlier two-Player/three-Output and native evidence established the MVP shape. Final committed-revision evidence remains required for the complete demo, authenticated browser walkthrough, current exact-image native paths, and automatic central rollback; physical qualification also remains outstanding. Follow the [delivery checklist](implementation-checklist.md) and [evidence](evidence/README.md); do not treat earlier local-state evidence as acceptance of the current design.

## Local launch

Prerequisites: Git, Docker Engine/Desktop with Compose, and Python 3.12 for development tests. Docker runs the central Python dependencies and PostgreSQL; no paid runtime service is required.

```sh
git clone https://github.com/mcurcio/photo-wall.git
cd photo-wall
git checkout feat/runnable-mvp
python3 scripts/configure.py
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8000/healthz
```

Open `http://127.0.0.1:8000`. Read the operator token from the private `.env` file and enter it in the operator interface. The script creates `.env` with mode 0600 and never overwrites it. No credentials are committed. The development listener and database port bind only to loopback. Appliance deployment requires the separately configured HTTPS/PXE trust boundary; this local listener is not that deployment.

The operator interface lists Players and Outputs, creates persistent Frames, binds equipment, retires a Player, and previews/commits/reverts calibration. It also creates immutable Sources and Scenes, schedules Programs, starts/finishes/cancels Runs, and shows source/worker health. Program timestamps use the browser's displayed local time zone. Configure the private upstream connection on the worker before creating a Source with its connection ID. The disposable browser walkthrough below covers these controls; final-revision delivery evidence remains separate. With the scheduler enabled, `/healthz` is green only when the database is reachable and a scheduler tick completed successfully within the last 10 monotonic seconds; `starting`, `coordination_unavailable`, `stale`, and `stopped` states return 503 with fixed sanitized status fields. Explicit test mode can disable the scheduler and retain database-only health semantics. A green `/healthz` reports service liveness, not observed presentation.

The worker starts with an empty private connection list and remains healthy while idle. Its configuration is described in [the worker module](module-media-worker.md); a deployment must provision that file as UID 10001, mode 0600 in the `connections` volume and restart `worker`. Never put an upstream API key in operator forms, Source definitions, Player configuration, Git or command-line arguments. The [Immich fixture](module-immich-fixture.md) generates its own synthetic media and disposable private configuration for reproducible adapter tests.

To provision a real worker connection, use the Compose service's mounted
`connections` volume and feed a separately prepared private file through
Docker standard input. This keeps the API key out of shell arguments, history
and Docker output. Prepare the source file outside the repository with the
deployment's approved secret editor or secret manager, and make it mode 0600
before using it:

```sh
python3 - <<'PY'
import os
from pathlib import Path
path = Path.home() / ".photo-wall-connections.json"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.fchmod(fd, 0o600)
os.close(fd)
print("Created private connection file with mode 0600.")
PY
```

The command refuses to overwrite an existing file. Do not place that file in
Git or a shared temporary directory; populate it with the deployment's approved secret
editor or secret manager, keep it for the next command only, and remove it
with the deployment's secret-management procedure afterward.

Use this shape as the file contents, replacing only the values in the approved
private editor. `base_url` must end in `/api` (or `/api/`), and `ca_file` is a path
inside the worker container when a separately provisioned private CA is
required:

```json
{"schema":1,"connections":[{"connection_id":"immich-main","base_url":"https://immich.example.test/api","owner_id":"00000000-0000-4000-8000-000000000000","api_key":"PASTE_KEY_HERE","allow_http":false,"ca_file":null}]}
```

Validate and publish the staged file as the worker UID. The existing worker
loader performs the schema, ownership, mode, size, URL/TLS-policy and
secret-shape checks; it does not replace an actual upstream TLS connection
test. A failed check removes the staged file without printing its contents.

```sh
docker compose run --rm --no-deps -T --user 10001:10001 \
  --entrypoint /bin/sh worker -c '
set -eu
trap "rm -f /etc/photo-wall/private/.connections.json.new" EXIT
umask 077
cat > /etc/photo-wall/private/.connections.json.new
chmod 0600 /etc/photo-wall/private/.connections.json.new
python -c "from pathlib import Path; from media.worker import load_connections; load_connections(Path(\"/etc/photo-wall/private/.connections.json.new\"))" >/dev/null 2>&1
mv /etc/photo-wall/private/.connections.json.new /etc/photo-wall/private/connections.json
' < ~/.photo-wall-connections.json
docker compose restart worker
docker compose exec -T worker stat -c '%u:%g %a' /etc/photo-wall/private/connections.json
```

The final command must report `10001:10001 600`. Updating this file takes
effect after a worker restart. Do not use `cat` to display the private document,
an inline heredoc, or a command argument for the private document or API key.
Worker diagnostics are sanitized, but never send the private document to a log
command. The fixture helpers remain disposable test configuration and do not
provision a deployment worker.

In the Scene form, keep live source selection for a changing collection, or
select **Choose a photo or video for each Frame** to keep a specific current
asset for each participating Frame. Each chooser lists only media compatible
with that Frame. The Scene and its references save together; source freshness,
membership and compatibility are rechecked centrally. Playback still requires
prepared media.
The [authored-media contract](module-authored-media.md) describes retention and
capacity. Development checks for this form require Node.js and execute its
event flow with a synthetic DOM; this is separate from the authenticated
browser walkthrough.

For immediate activation, choose a priority and whether an already active Scene
should be ignored, restarted, or queued. Queue requests require an expiry;
**Force** is an explicit override. Queued and ignored requests report their
actual outcome without claiming that a new Run started.

`PHOTO_WALL_HORIZON_SECONDS` defaults to 300 seconds. In the scheduler's PostgreSQL transaction, the media repository records each bounded preparation request and defers its exact job ID through Procrastinate. The separate worker publishes verified derivatives into the `media` volume. Central mounts it read-only and serves exact authorized bytes; Players never receive an upstream URL or credential. Procrastinate owns dispatch, retry timing, and queue-worker liveness. Photo Wall schedules only domain preparation, source refresh, and publication/storage maintenance tasks; it retains publication recovery, reservations, and stale-attempt fencing. The worker has a read-only runtime, a private writable media volume, and container CPU/memory limits.

Boot selection exists at `/v1/bootstrap/boot`, root images are served from `/appliance/rootfs-<sha256>.squashfs`, and fresh key-proof enrollment uses `/v1/enrollment/challenge` plus `/v1/enrollment/register`. The single-process Player entry point is `python -m player.service --config /etc/photo-wall/public.json`; see [service configuration and runtime requirements](module-player-service.md). The Player reads its RAM boot context, creates a new process key, and receives a new central authority epoch. A recognized returning equipment observation restores central bindings; unknown equipment remains unbound. `/v1/player/time` supplies independent authenticated clock samples and `/v1/player/boot-health` binds release health to the current ticket/session. Physical Pi/PXE and complete current-image qualification remain pending. Startup-only DRM discovery currently requires a Player restart after connector topology changes.

The `database` volume persists all authoritative state, including Procrastinate jobs and release/trial records. `media` holds central authoritative media blobs and preparation files, and `connections` holds private worker configuration. Central and worker startup apply forward Photo Wall SQL migrations under a database advisory lock and check hashes of already-applied migrations; they also install/upgrade Procrastinate's versioned schema. Do not edit an applied Photo Wall migration; add another numbered migration. Both containers run as an unprivileged user. The exact Python dependency graph is in `uv.lock`.

## Player provisioning: flash and go

The baseline Player path (0008) is flash-and-go: no boot ticket, no pre-registration. Central advertises itself over mDNS (`_photowall._tcp`) once its process starts, unless `PHOTO_WALL_MDNS_ADVERTISE=false` is set. If the deployment serves central on a port other than the default `8000`, also set `PHOTO_WALL_HTTP_PORT` to that real port, or the advertisement points a discovering player at a dead port.

1. **Flash the generic image** to an SD card or USB drive (build it per the [README's provisioning section](../README.md#provision-player-appliances); there is no published release artifact yet) and boot the Pi on the same trusted LAN as central.
2. **Watch the pending queue.** The Pi derives its `device_id` from its own hardware serial, finds central by mDNS (only because no explicit origin is configured on the card), and enrolls over plain HTTP. It appears in the operator inventory (`/v1/operator/inventory`, or the operator UI's Players list) as **unbound** — no prior registration required.
3. **Bind** the pending Player's Output to a Frame from the operator interface, the same control used for any equipment change, then calibrate it.
4. **Unbind** (`DELETE /v1/operator/frames/{frame_id}/binding`) releases a Frame's binding without retiring the Player — the serial can be re-bound later, to the same or a different Frame. This is distinct from **retire**, which is permanent and refuses the serial's re-enrollment.
5. **Reboot** of an already-bound Pi re-associates with its Frame automatically by serial; no operator action is required unless the binding itself needs to change.

Netboot (PXE) is the opt-in enhancement path in place of flashing — see the [PXE service module](module-pxe-service.md). Certificate-based identity and pinned/explicit transport trust are further opt-in enhancements described in [decision 0008](decisions/0008-generic-image-and-serial-identity.md); they are designed but not yet implemented, so do not rely on them today. Physical Pi boot of either the flash image or the netboot tree is not yet hardware-qualified.

## Player provisioning: netboot and promote the app (0009, in progress)

[Decision 0009](decisions/0009-minimal-base-and-app-package.md) is the adopted target for the netboot tier: a minimal base OS image that carries no application, plus the Player shipped as a downloadable `.deb` that central serves. Nothing is signed — the owner ruled a home LAN has no threat model, so the sha256 published alongside the `.deb` is a corruption check, not an authenticity proof. Once the boot-chain wiring below lands, the operator flow is:

1. **Stage the base image in your TFTP tree.** Unpack the published base OS bundle (kernel, DTBs, initramfs, `base-<revision>.squashfs`) beneath the boot-server root exactly as any other boot tree — see [PXE service setup](module-pxe-service.md). The base carries no Player code and no deployment config; it exists to run the bootstrapper (`appliance/provision.py`) that fetches everything else.
2. **Boot the Pi and watch the pending queue.** The bootstrapper discovers central by mDNS, downloads the app manifest and the `.deb`, installs it, and starts the Player, which enrolls by serial — it appears **unbound** in the same operator inventory (`/v1/operator/inventory`) as the flash path.
3. **Register and promote the app in central.** Copy the `.deb` bytes to central's `PHOTO_WALL_APP_ROOT` as `app-<sha256>.deb` out of band (central never accepts the bytes over the request body — this mirrors how a signed release artifact is staged today), then:

   ```sh
   curl -X POST http://<central>/v1/operator/app \
     -H 'Authorization: Bearer <admin-token>' -H 'Content-Type: application/json' \
     -d '{"version": "<version>", "sha256": "<sha256>", "size": <size>}'
   curl -X PUT http://<central>/v1/operator/app/current \
     -H 'Authorization: Bearer <admin-token>' -H 'Content-Type: application/json' \
     -d '{"sha256": "<sha256>"}'
   ```

   `POST /v1/operator/app` records the `{version, sha256, size}` pointer (422 on malformed input); `PUT /v1/operator/app/current` promotes it as the one global "current app" (404 if that sha256 was never registered). Every Player fetches the newly promoted `.deb` on its next reboot; already-running Players are unaffected until then.
4. **Bind** the pending Player to a Frame and calibrate, exactly as in the flash-and-go flow above.
5. **Update the app later** by repeating step 3 with a new `.deb` — no re-imaging, no re-signing, no boot-tree edit. There is no auto-rollback: if a promoted `.deb` crashes on boot, the dark screen is the signal, and recovery is re-promoting the previous sha256.

**Where this actually stands.** Central's app-package endpoints (`central/app_packages.py`, `central/app.py`), the minimal base image build (`scripts/build_ci_base_image.py`), the `.deb` build (`scripts/build_player_deb.py`), and the bootstrapper (`appliance/provision.py`) are each implemented and pass their own tests in isolation. **The PXE boot chain that would load the minimal base and hand off to the bootstrapper — with no boot ticket and no signature — is not yet wired**: today's initramfs still runs the old signed boot-ticket protocol described in [decision 0008](decisions/0008-generic-image-and-serial-identity.md#the-netboot-tier-d1-and-its-config-decoupling), so a netboot deployment today still boots that signed, combined image, not this one. Do not follow the steps above against a real fleet yet; they describe the design 0009 targets, and this section will be reconciled with the [appliance builder](module-appliance-builder.md) module once the wiring lands.

## Player provisioning: promote a release from GitHub (0010)

[Decision 0010](decisions/0010-github-release-sourcing.md) removes the manual sha256 dance above for the common case: central **watches the project's GitHub Releases**, records every semver release as a candidate, and lazily mirrors the `.deb` into the same `PHOTO_WALL_APP_ROOT` store Players already fetch from — but only when *you* promote a version. Discovery is automatic; promotion is a deliberate operator action. Nothing is signed; the sha256 is a corruption check only. See [the operator release-sourcing flow](module-player-package.md#operator-release-sourcing-0010) for the model.

**Enable it (config env).** Release sourcing is opt-in: it activates only when `PHOTO_WALL_APP_ROOT` is set on the **worker** (it is the worker that reaches the internet and mirrors bytes). Central and the worker must point `PHOTO_WALL_APP_ROOT` at the **same shared `.deb` store** (the worker writes `app-<sha256>.deb`; central serves it), exactly as the manual path already requires.

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `PHOTO_WALL_APP_ROOT` | central + worker | (unset) | Shared `.deb` store; setting it on the worker enables release sourcing |
| `PHOTO_WALL_RELEASE_REPO` | worker | `mcurcio/photo-wall` | `owner/name` of the GitHub repo whose releases are polled |
| `PHOTO_WALL_RELEASE_TOKEN` | worker | (unset) | Optional GitHub token; unauthenticated polling is rate-limited to ~60 requests/hour |
| `PHOTO_WALL_RELEASE_PRERELEASES` | worker | off | Truthy to also track GitHub prereleases (drafts are always skipped) |
| `PHOTO_WALL_RELEASE_POLL_SECONDS` | worker | `900` | Poll cadence in seconds |

**List, promote, refresh (all admin-authenticated).** These reach central's operator API; substitute your central origin and admin token:

```sh
# List tracked releases: tag / version / mirror_state / deployable / promoted / current.
curl --fail -H 'Authorization: Bearer <admin-token>' \
  http://<central>/v1/operator/app/releases

# Promote a version. 200 if its bytes are already mirrored (current advances now);
# 202 pending if central must mirror the .deb first; 404 unknown tag; 409 undeployable.
curl -X POST -H 'Authorization: Bearer <admin-token>' \
  http://<central>/v1/operator/app/releases/<tag>/promote

# Poll GitHub now instead of waiting for the next cadence (coalesced; 202).
curl -X POST -H 'Authorization: Bearer <admin-token>' \
  http://<central>/v1/operator/app/releases/refresh
```

All three return **503 `release_sourcing_unconfigured`** when `PHOTO_WALL_APP_ROOT`/the release queue is not wired.

**Promoted vs. current (pending).** A promote records your **chosen tag** immediately. If that release is already mirrored, the served **current** pointer advances in the same request (`200 promoted`). If it is not yet mirrored, the request returns `202 pending`: the worker downloads and verifies the `.deb`, and `current` advances only once those bytes are on disk — the previously current version keeps serving until then, so Players are never broken. A pending promote whose uplink is down stays pending; it completes automatically when connectivity returns (no re-promote needed). Watch `mirror_state` in the list to see it move `discovered → mirroring → mirrored`, and `current` flip to the new tag.

**Offline / air-gapped.** The manual stage-by-reference path (`POST /v1/operator/app` + `PUT /v1/operator/app/current`, [above](#player-provisioning-netboot-and-promote-the-app-0009-in-progress)) still exists as an escape hatch when central cannot reach GitHub but you have the `.deb` on hand. A manually staged package simply won't appear in the release list.

## Operator API: reposition and remove Frames

The operator console edits the wall plan through two admin-authenticated routes on central (both `Depends(admin)`, like every `/v1/operator/*` route). They change no schema and add no migration — the placement columns (`surface_id`, `x_mm`, `y_mm`, `width_mm`, `height_mm`) already exist on the `frames` row.

`PATCH /v1/operator/frames/{frame_id}` applies a **partial** placement. The body accepts `surface_id`, `x_mm`, `y_mm`, `width_mm` (> 0), and `height_mm` (> 0); any omitted field keeps its stored value — this is a merge, not a replace. Central re-runs the same orientation-coherence guard as Frame creation against the merged dimensions and the stored profile, returning **422** when the resulting aperture orientation would disagree with the display profile. An id that no longer exists returns **404 `unknown_frame`**; on success the response echoes the merged placement. Placement is **last-write-wins with no concurrency token** — two operators dragging the same Frame silently overwrite each other and the plan corrects on the next snapshot — because geometry is operator-only metadata: it never reaches a Player, it is independent of calibration (whose corners and crop are normalized to `[0,1]`), and a move is trivially re-dragged. The route therefore **does not bump `generation` or `configuration_revision` and never invalidates calibration**.

`DELETE /v1/operator/frames/{frame_id}` removes a Frame, but only a clear one. It refuses with **409 `frame_in_use`** when a live Run (phase body or outro) targets the Frame — finish or cancel that Run first — and with **409 `frame_bound`** when an Output is still bound to it — unbind first (`DELETE /v1/operator/frames/{frame_id}/binding`). An unknown id returns **404**. On success it deletes the Frame and returns **200 `{"status": "deleted"}`**. The guards protect one invariant: you cannot delete a Frame a Player is currently bound to serve.

## Operator console: placing, moving, and deleting Frames (and the Unplaced tray)

In the redesigned `/console` the wall plan is the home: each Surface is a flat millimetre plan and its Frames are drawn as rectangles from their `x_mm/y_mm/width_mm/height_mm`. You build and edit that plan by direct manipulation — the console turns each gesture into one of the operator routes documented above ([reposition and remove Frames](#operator-api-reposition-and-remove-frames)) or the existing `POST /v1/operator/frames`. Selecting a Surface *filters* the plan to that Surface's Frames; a Surface is a bare text label, not something you act on. This mirrors the "reading & building the wall" walkthrough (J3) in the [console design](operator-console-ux-design.md).

**Place a new Frame.** Drag a rectangle on the empty plan, then enter the Frame's **display profile** — pixel width/height and diagonal (plus whether it is video-capable). The console sends one `POST /v1/operator/frames` carrying the dragged `surface_id`, `x_mm`, `y_mm`, `width_mm`, `height_mm` and that profile, so the Frame is **created at the position you drew**. It **never lands in the Unplaced tray** — only Frames with no distinct geometry do that (below). Central runs the same orientation-coherence guard as every Frame creation: if the aperture orientation would disagree with the profile (a portrait matte declared against a landscape panel, or vice versa) the create is refused and nothing is added. The profile values are **Frame facts** — operator-declared and persisting across a later panel swap — not live display readback.

**Move a Frame.** Drag an existing rectangle to reposition it. The console rides `PATCH /v1/operator/frames/{frame_id}` sending only `surface_id`, `x_mm`, and `y_mm`, so **a move never resizes** — `width_mm` and `height_mm` keep their stored values. Placement is **last-write-wins with no lock**: a concurrent move of the same Frame by another operator simply wins, your losing drag is overwritten, and the plan corrects itself on the next snapshot refresh. Because geometry is operator-only metadata — it never reaches a Player and is independent of the calibration corners (normalized to `[0,1]`) — a move **carries no concurrency token and never invalidates calibration or bumps `generation`**. This is the one deliberate exception to the console's "every write carries its token" rule; a mis-drag costs only a re-drag.

**Delete a Frame.** Deleting rides `DELETE /v1/operator/frames/{frame_id}`, and only a clear Frame is removed. It is **refused while the Frame is bound** — the message reads "unbind it before deleting" (409 `frame_bound`; unbind from the Frame's [Binding facet](#operator-console-onboarding-binding-and-auto-recovery)) — and **refused while a live Run targets it**, reading "finish or cancel the Run before deleting" (409 `frame_in_use`). With neither guard tripped the Frame is removed from the plan. The guards protect one invariant: you cannot delete a Frame a Player is currently bound to serve.

**The Unplaced tray.** Frames created by the old UI all sit at `wall`/(0,0) with no distinct position; rather than pile them at the origin as permanent clutter, the console collects such geometry-less Frames in an **Unplaced tray** beside the plan, as a list. From the tray you can **drag a Frame onto the plan** to give it a position — that sends the same `PATCH` as a move, and the Frame **then leaves the tray** and joins the plan — or **delete it** under the same two guards above. Either way it stops being permanent clutter.

A freshly placed or moved Frame **settles to its final position on the next snapshot refresh**: the plan re-fits per Surface (it scales each Surface's millimetre space to the viewport), so a rectangle drawn or dragged optimistically can shift slightly once the authoritative snapshot returns, while its stored millimetre geometry is exactly what you entered.

## Operator console: onboarding, binding, and auto-recovery

New hardware surfaces in the console before it does any work. Following [decision 0006](decisions/0006-central-authority-and-stateless-players.md) — central is the source of truth and a Player is a swappable box behind a Frame — a Player that enrolls but is not yet bound to a Frame lands in the **Pending rail** beside the wall plan; a Player you retire from service moves to the **Retired rail**. Both rails read from the same `/v1/operator/inventory` snapshot the plan does (`PlayerInventory.is_bound`, `retired_at`), so a new or replacement Pi appears in Pending the moment it enrolls, with no separate queue to poll.

**Bind a Frame to a pending Output.** Select the Frame in the wall plan, open its **Binding** facet, and pick a pending Output; the same facet **unbinds** an Output that is bound. Each write rides the existing admin-authenticated `PUT` / `DELETE /v1/operator/frames/{frame_id}/binding` routes — no new endpoint, no schema change, no migration — and carries the Frame's `generation` as an **optimistic token**. If the Frame changed underneath you (a concurrent bind, unbind, or retire bumped its `generation`), the write is refused with **409 `binding_generation_conflict`** and the facet says **"This Frame changed — reload and review its binding."** rather than silently overwriting a binding you never saw. Nothing is applied on a refused write.

**After a bind, the display needs re-commissioning.** A successful bind (or unbind) bumps the Frame's `generation`, clears any preview, and **invalidates calibration** — so the Frame tile shows an amber **"Review required"** and offers a **"Commission the display"** CTA that opens the [Commissioning facet](#operator-console-commissioning-calibration-and-conflict-states). This is a **forced re-validation, not data loss**: `bind`/`unbind`/`retire` set `calibration_valid=false` but **never write the `calibration` column**, so the committed calibration blob (and any future color field riding it) persists across the swap and is available to re-confirm. Only a fresh **commit** overwrites it.

**Auto-recovery banner (returning known Pi).** When a Pi central already knows reboots, it re-enrolls by hardware serial and central self-heals it straight back to its old bindings — so it reappears already bound (`is_bound=true`) instead of dropping into the Pending rail. The console marks this with a one-line banner: **"Recovered — already bound (serial match, not identity)."** Read that wording literally: recovery is a **serial-match convenience, not cryptographic identity** — the serial is not a secret and central does not prove the box is the same box, only that it presents the same serial. The banner is **suppressed on a true first run** (there is no discrete "recovered" flag in the payload; the console infers recovery by diffing the retained prior snapshot's `authority_epoch`, and with no prior snapshot it does not guess). The per-boot **`authority_epoch` bump is never surfaced as an alert on its own** — every boot mints a fresh epoch, so on its own it is normal, not an incident; it only participates in inferring recovery.

## Operator console: commissioning, calibration, and conflict states

In the redesigned `/console` (still parallel to the existing `/` page until cutover), select a Frame in the wall plan to open the Frame Inspector, then open its **Commissioning** facet — the layer where you set up the display behind a Frame. It is reachable **only in Wall mode**; calibration is a hardware concern deliberately hidden from show programming, which sees only a Frame-health badge. Everything below rides the existing admin-authenticated `POST /v1/operator/frames/{frame_id}/calibration` route — no new endpoint, no schema change, no migration.

**What the facet shows (read-only, T0).** Four honest readouts, none of them a control:

- **Committed calibration** — the SDR gain, rotation, corners, and crop currently in force on the panel.
- **Frame facts** (from `FrameProfile`) — pixel width/height, diagonal, and video-capable. These are **operator-declared at Frame creation and persist across a panel swap**, so they are labelled *Frame facts*, not live display facts.
- **Live Display readback** (from `OutputReport`) — whether the bound Output is **connected** and its reported resolution. This is the *only* live readback the panel offers; nothing richer (EDID, model, refresh rate, HDR, active-area, bezel, overscan) exists anywhere in the system.
- **Bound equipment** — which Player/Output currently serves the Frame.

The **panel color correction** and **display power / parameters** areas render **"not yet available."** They are capability-gated and no wired path enables them today: panel color is a T1 concern and display power/CEC is a T2 cross-layer epic. No control on the facet implies a stored field that does not exist.

**Calibrate by direct manipulation.** In the *Adjust calibration* editor, drag the four corner handles and the two crop handles, or type the coordinates directly; SDR gain and rotation are separate draft controls. Every edit stays a **local draft** that a background inventory refresh never overwrites. Each geometry change is validated by the **same convex test the server enforces** (the `1e-6` epsilon and clockwise winding): a folded or too-thin quad snaps the handle back with the inline message **"corners must form a convex aperture"**, and an empty crop snaps back with **"crop must describe a nonempty rectangle"** — and **no request is sent**. A rejected drag changes nothing, on the client or the server.

**Preview → 30-second lease → commit / revert.** **Preview** pushes the draft to the panel under a server lease that expires in **30 seconds**; the facet shows the *server's* countdown ("Previewing on the panel — lease expires in Ns"). There is **no auto-renew**. If you do nothing, the lease lapses, the panel returns to its committed calibration, and the facet says so — **"Panel is back on committed. Re-preview to keep trying."** — while keeping your "trying" values so **Re-preview** re-pushes them in one click without re-entering anything. **Commit** saves the draft as a new calibration revision; **Revert** drops the preview and returns the panel to committed immediately. Preview and Commit require a bound Frame.

**One shared preview slot, last-writer-wins.** There is a **single preview slot per Frame and no lock** — the console never implies you have exclusive control of the panel. A second tab or operator who previews or commits the same Frame overtakes you. While the facet is open the console polls the inventory (~5s) so these changes surface as explicit states rather than a silent overwrite:

| What happened | What you see | What to do |
|---|---|---|
| Your lease expired with no interaction | "Panel is back on committed. Re-preview to keep trying." | Re-preview to keep adjusting; your trying values are retained. |
| Another tab/operator committed or re-previewed the same Frame (the single slot was taken) | "Committed elsewhere / your preview was superseded — re-review." | Reload the fresh state and review before writing again. |
| Commit refused — another commit advanced the calibration revision (stale `expected_revision`) | "Another session changed this frame's calibration — reload and re-review." | Reload and re-review; your stale commit was **refused**, not silently applied. |
| Commit refused — the Frame's binding changed via bind/unbind/retire (stale `expected_generation`) | "This Frame's binding changed — its display is no longer under your control; reload." | The display is no longer yours to calibrate; reload and re-check the binding. |

Every write carries **both** concurrency tokens (`expected_revision` and `expected_generation`) against the baseline captured when you opened the facet, so a stale commit is refused with a **409** rather than silently overwriting state you never reviewed. (A preview or commit against a Frame whose binding was removed underneath you is refused with "This Frame is no longer bound to a display — bind it before calibrating.")

## Tests and local development

Install the free `uv` Python package manager, then:

```sh
uv sync --frozen
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 scripts/check_docs.py
```

The portable command reports PostgreSQL integration tests as **skipped** unless `PHOTO_WALL_TEST_DATABASE_URL` is set. To run all tests against the local Compose database:

```sh
.venv/bin/python scripts/test_local.py -q
```

That wrapper reads local `.env` as data, never sources it as shell code. Each PostgreSQL test creates a random `pw_test_*` schema and removes only that schema. It preserves registry data in the deployment's public schema. A custom integration server may be supplied through `PHOTO_WALL_TEST_DATABASE_URL` with permission to create/drop test schemas. Keep it pointed at a development server.

CI installs the locked dependencies, lints, checks local documentation links, builds/launches Compose, runs the PostgreSQL suite, and checks central HTTP health. It separately runs all preparation tests inside the pinned Linux worker image, so missing host FFmpeg cannot silently remove that gate. Passing CI does not establish physical Pi/PXE, real Immich, rendering or visible timing.

All CI worker builds reuse the architecture-matched native media base through
the [shared dependency workflow](module-appliance-ci.md#shared-service-and-test-dependencies).
Application edits do not permit a missing OS dependency to be rebuilt. To
prepare an unchanged missing definition explicitly, dispatch `checks.yml`,
`software-e2e.yml`, or `appliance.yml` with `prepare_base=true`; select `scope=full`
for appliance media qualification. Fork runs require the definition to have
been published by a trusted run. Ordinary local Compose builds retain their
explicit cold native target.

The real-browser walkthroughs of the [binding/commissioning](../tests/browser/test_operator_binding_browser.py) and [showrunner content](../tests/browser/test_operator_showrunner_browser.py) surfaces drive the production React operator console (served at `/`, aliased at `/console`) and its HTTP API against their own temporary PostgreSQL schemas. Install the locked development dependencies and their matching Chromium build, then run:

```sh
uv sync --frozen
.venv/bin/python -m playwright install chromium
PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser \
  --browser chromium --tracing retain-on-failure --output artifacts/operator-browser
```

CI runs these checks in the official Playwright Python 1.62.0 Noble container,
pinned by digest in `checks.yml`. That image already contains Chromium and its
Linux dependencies, so CI does not run a browser APT installation. A separate
container environment installs the repository's frozen Python dependencies and
connects to the job's local PostgreSQL fixture. Reports and failure traces are
written to the existing artifact directory. Ordinary test runs skip browser
checks unless opted in. Playwright 1.62.0 and pytest-playwright 0.9.0 are pinned
in the development dependency group and `uv.lock`; neither enters production
services or the Player package. Update the image and locked Playwright version
together so the browser binaries match. See the official
[container contract](https://playwright.dev/python/docs/docker) and
[pytest runner](https://playwright.dev/python/docs/intro).

The registry walkthrough proves rejected/accepted authentication, reconnection after a rejected token, rejection of delayed failures from an earlier login attempt even when the same token is reused, two-Player/three-Output inventory, Frame creation/binding, calibration preview/revert/commit, stale-tab conflict recovery, replacement/retirement, and persistence through a fresh server/connection pool. It checks preview expiry using controlled time. The content walkthrough creates a Source, saves live and per-Frame authored Scenes, checks compatible prepared-photo choices and current selection guidance, schedules and removes Programs using the browser's local time zone, and starts, ignores, queues, naturally finishes, and cancels Runs. Definitions, Programs, and Run history survive a fresh server/connection pool.

All operator mutations use browser controls, and every page rejects uncaught JavaScript errors. The fixtures supply simulated equipment and generated public JPEGs through production source-refresh, acquisition-request, and publication transactions with an explicitly synthetic recipe and preparation metadata. Elapsed time advances through the production Runtime owner; natural finish waits for the current cycle boundary. The server binds an ephemeral loopback port, preserving the separate full demo's network isolation. These checks qualify operator controls and persistence. They do not run an upstream adapter, conversion worker, background scheduler, Player, or renderer, and do not qualify scheduled playback, PXE, or physical output. Run browser checks separately from the ordinary suite: synchronous Playwright owns an event loop for its session, while ordinary Player integration tests create their own loops.

The bounded schema-2 `operator-browser.json` report records named assertions, pass/failure status, browser version, PostgreSQL/fixture scope, generated-media and controlled-time inputs, checkout revision, dirty state, and GitHub event/SHA. CI always uploads available reports and retains traces only for failed tests. Pull-request runs identify the synthetic merge checkout; dispatching `MVP checks` on the PR branch records the dispatched commit instead. A dirty local run is diagnostic evidence, not final committed-revision acceptance. Reports and failure traces contain only the disposable fixture's public test token and synthetic records; keep unrelated deployment data out of the fixture.

## Recovery

```sh
docker compose restart central worker
docker compose logs --tail 100 central worker database
docker compose up -d --build --wait
```

Restart preserves the database volume. `docker compose down` stops this deployment without removing the volume. Back up PostgreSQL using `pg_dump` before migration or deployment changes; restoring production backups has not yet been qualified. Never use `down --volumes` on a deployment whose registry must be retained.

If an Output moves, bind the destination persistent Frame. Returning recognized equipment automatically receives its centrally stored binding after fresh enrollment. For replacement equipment, explicitly change the binding from the old equipment Output to the new registered Output; observations alone never transfer operator intent. Frame geometry survives, generation increases, and playback requires revalidated calibration. Starting or reconnecting a Player rotates session credentials/authority without creating another Frame or changing desired geometry.

Preview carries a 30-second expiry and both proposed/committed settings in current process memory so the Executor can revert during a running-process outage. Commit and revert use optimistic revision and binding-generation checks. A stale browser must refresh before retrying. Partitioned equipment respects the bounded plan lease and rejects obsolete work when it obtains fresh session authority. Cold reboot requires central time/release/enrollment/control/media connectivity. A surviving cache file can avoid a media request only after the new process validates it against the current assignment; it cannot restore authority.

The [real Immich fixture](module-immich-fixture.md), [full media-path demo](module-wall-demo.md), [Player-only package builder](module-player-package.md), and [central release contract](module-appliance-release.md) provide commands and evidence boundaries. The [appliance builder/bootstrap](module-appliance-builder.md), [GitHub ARM image workflow](module-appliance-ci.md), and [headless image e2e gate](module-appliance-e2e.md) describe exact-artifact checks and their limits. Earlier signed image and hosted boot evidence remains useful for artifact identity and generic-VM behavior, but its durable-Player/local-update assumptions are superseded. Complete current-image native rendering, valid-cache reuse, corrupt-cache reacquisition, real automatic reboot/central rollback, and physical measurements remain pending until recorded against the final revision.
