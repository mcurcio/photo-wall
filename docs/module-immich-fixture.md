# Disposable Immich integration harness

Status: bounded real-server fixture passed 2026-09-05; initial failures and the corrected run are recorded below. Delivery [4](roadmap.md#4-real-photo-sources) (pull request [#81](https://github.com/mcurcio/photo-wall/pull/81)) gave it the photo-source content set, a published mode for the named tests and the `photo-sources` CI leg; its adapter checks moved into those tests. The [media module](module-media.md) owns the upstream contract. This harness owns disposable setup, synthetic content, isolated network probes, fault actions and sanitized evidence. It does not implement central selection, delivery, commitment or rendering.

## Boundary and interface

[scripts/immich_fixture.py](../scripts/immich_fixture.py) provides host commands `run --state-dir ABSOLUTE_NEW_DIRECTORY [--keep] [--setup-only] [--publish]` and `cleanup --state-dir DIRECTORY`. The host uses argument-array Docker calls and an exclusive private state directory. A marker records a random `pw-immich-fixture-*` project name. Cleanup validates the marker and removes only that project's containers, networks and named volumes; fixture files and evidence remain in the private state directory. Existing state is never silently reused for a new run. `--keep` retains the dedicated services for later central integration. `run --setup-only` stops once the upstream is set up (owner, partner, runtime key and the content set below). The adapter checks the harness used to run itself (`verify`, `ADAPTER_ROLES`) are gone: what they proved (live changes, deletion, permission loss, outage) is now proved by the named tests, which drive Photo Wall's own client, store and worker against this Immich. The Player-probe boundary checks stay in every run.

Container roles are separate from that host driver. [scripts/immich_actions.py](../scripts/immich_actions.py) owns the synthetic upstream client and mutations; [scripts/immich_runtime.py](../scripts/immich_runtime.py) exposes setup and the fixed health endpoint. Their explicit image bundle excludes the Docker driver, process-group control, and private Docker diagnostics. An isolated import-closure test checks that boundary, and the derived image copies only the explicit runtime bundle. Reusable `FixtureHost.compose(...)` supports later end-to-end orchestration without copying topology or credential setup. No service uses host bind mounts. Docker-managed `setup` storage holds the generated administrator session. Docker-managed `runtime` storage holds only the restricted runtime key/configuration, fixture expectations, and acquired originals. Both are initialized for unprivileged UID 10001. Player probes mount neither volume. `export_runtime()` copies the runtime state into the host's private state directory for subsequent integration, without printing its key. Evidence contains only synthetic labels/hashes, counts, failure codes, image identities, explicit network memberships and probe outcomes.

The [fixture Compose](../tests/integration/compose.immich.yml) pins Immich v3.1.0 (the follow-up to roadmap 5a moved it from v2.5.6, because production runs 3.1; `immich_actions.initialize` requires exactly 3.1.0 and no prerelease) to the resolved index digest `sha256:b434cb9287eea1471c9974845914d4dd328c9c2d652e446ed4930f99944f0ceb`, with Valkey at its refreshed digest. The `linux/arm64` manifest is looked up from that index when the image is pulled. PostgreSQL/extensions and Valkey retain the [tagged official Compose](https://github.com/immich-app/immich/blob/3be8e265cd5bd6ca921ff9e665e274c5de45caa0/docker/docker-compose.yml) pins. There are no fixed container names, upstream host ports, external volumes or external networks. The optional machine-learning service is omitted and machine learning is explicitly disabled before uploads; this is a query/acquisition fixture, not the complete official deployment.

Only the central adapter probe is dual-homed. Its IP forwarding is disabled, capabilities dropped and root filesystem read-only. Its HTTP listener implements one static health response and cannot forward requests. The fixture driver and Immich dependencies are on `upstream_net`; the Player probe is on `wall_net`. Both networks are internal. The Player probe has no credentials, source mount, host networking, Docker socket, host-gateway alias or proxy configuration. It must reach central health and fail both Immich DNS and TCP to Immich's inspected numeric container address, before and after acquisition. A health endpoint establishes network reachability only; it is not evidence of Photo Wall media delivery.

## The photo-source content set

`immich_actions.initialize` uploads one content set with the admin session and records each item's label, Immich id and expected facts in `/runtime/library.json`; `tests/support/immich_library.py` (`load_immich_library`) reads it for the tests. Machine learning stays off, so people are made with manual faces (`POST /faces`).

| Labels | What they prove |
|---|---|
| Owner "Fixture Owner"; partner "Fixture Partner" sharing with the owner (`/partners`) | partners' photos are left out unless chosen (#32); a key of the partner is `other_user` |
| `anna-ben-1`..`-3` (Anna and Ben, 2024–2025), `anna-only`, `ben-only`, `anna-ben-old` (2019) | people (#16, #17: all must appear) and relative dates (#20) |
| `screenshot-anna-ben` (PNG named "Screenshot 2025-03-01.png"), `scan` (PNG with no camera) | the screenshot exclusion (#31) both ways |
| `archived-anna-ben`, `hidden-anna-ben`, `locked-anna-ben` | never included (#29, #30) |
| `stack-primary` (Ben only) and `stack-copy` (Anna and Ben), stacked with the first as primary | a stack joins only through a matching primary (C9) |
| `edited` (cropped through `PUT /assets/{id}/edits`) | edits fetched, thumbnailed and sized; the revision carries the edit |
| `partner-anna-ben` | partners (#32) |
| `paris-1`, `paris-2` (GPS 48.8566, 2.3522, reverse geocoding on) | place (#18) |
| Albums "Garden" (`garden-1`, `garden-2`) and "Holidays" (`paris-1`, `anna-ben-1`) | albums (#15, #26) |
| Tags "Family" and "Family/Christmas" on `anna-ben-2` and `xmas-1`; favourites `anna-ben-1`, `garden-1` | tags with nesting (#23, #28), favourites (#22) |
| `same-instant-1`..`-8` (one capture instant) | keyset paging loses nothing at equal times (page size 3) |
| `video-1` (2 s MP4 made by ffmpeg in the fixture image) | videos (#25) and video preparation |

The runtime key has exactly the permissions Photo Wall requires (`central.photo_library.policy.REQUIRED_PERMISSIONS`); `tests/test_immich_fixture_content.py` pins the two equal without importing `central` into the harness bundle, and holds the expected pool membership of every label for every named-test query. The recorded counts are Immich's own statistics after setup, read through the runtime key.

**Published mode.** `--publish` adds `tests/integration/compose.immich-published.yml`, which puts Immich on one more, non-internal network with a single port bound to `127.0.0.1`; the topology check allows exactly that binding, and only under `--publish`. The run writes `<state>/published.json` (`{"base_url": "http://127.0.0.1:<port>/api"}`) and copies the admin session to `<state>/runtime/admin.json`, in a private 0700 state directory that holds no real secret. A test is marked `immich` by its fixtures (any test that reaches `immich_library`), whatever its tier; it takes the `immich_library` fixture from `PHOTO_WALL_IMMICH_FIXTURE=<state dir>` and skip without it ("set PHOTO_WALL_IMMICH_FIXTURE", an allowed CI skip because the `photo-sources` leg runs them).

**The `photo-sources` CI leg** (in the [software e2e](../.github/workflows/software-e2e.yml), replacing `immich-adapter`): install ffmpeg, start the test database, run one fixture with `run --keep --setup-only --publish` (with `--base-image` of the pipeline's worker image, whose ffmpeg makes the content set's video), then `scripts/test_local.py -m immich -n 4 --dist loadgroup --ignore=tests/browser`, upload sanitized evidence, and clean up. Every `immich` test joins one xdist group (`tests/conftest.py`), because the fixture is one shared library the tests change; a test removes what it uploads in `finally`, and the tests' asynchronous client runs on one event loop per test process (`tests/support/immich_library.run`). The `db` leg runs `-m "db and not immich"`. Cost: one Immich start per leg and about 30 s for ffmpeg; the leg likely takes about 5 minutes, over the about-4-minutes-per-leg budget. The wall scenario's fixture is pasted into Settings through the API like any other connection ([wall demo](module-wall-demo.md)).

## Acceptance and failure behavior

The paragraphs below record the harness's first bounded run (2026-09-05) and its adapter checks as they were then; the explicit source-limit check they name is gone (owner, 2026-10-09: no limit on a Source), and the rest moved into the named tests above.

The first bounded run creates its own account, disables ML, issues a runtime key with exactly `user.read`, `asset.read`, `asset.download`, and uploads generated geometric JPEGs. Fixtures have labels, asymmetric color regions, explicit capture dates, original dimensions and all eight EXIF orientations; no external image or private metadata is used. The fixture code grants these generated images for unrestricted redistribution. Query probe size is deliberately small to force the adapter's bounded whole-result refetch with equal capture timestamps. Polls have deadlines and test observed metadata readiness instead of sleeping for a guessed extraction duration.

Checks require declared version/owner validation, complete metadata refresh, original byte download with exact SHA-1/SHA-256, original orientation dimensions, upload and mutation denial using the runtime key, newly uploaded older-capture and new-capture matches, a favorite change, genuine empty source, explicit source-limit failure, deletion before download, and permission loss. The retained local download remains exact after upstream deletion; central/Player content-lock persistence remains a separate integration gate. Runtime credentials and raw API errors are never printed. Expected HTTP failures report numeric status/coded results; unexpected failures produce a bounded harness error. Host command output is not echoed on failure because it may include private configuration. A shared Docker recorder privately spools command output, then writes only sanitized bounded stdout/stderr tails with the operation and exit code; it never records command arguments. The software E2E workflow uploads that file only on failure. Service startup and each API/adapter operation have explicit timeouts.

The harness supports later root-owned gateway/worker/Player integration while preserving the two-network boundary. Video profile conversion, central acquisition jobs and authorized gateway transfer, live Scene behavior, committed byte retention, full outage/restart faults and physical Pi qualification remain outside the initial fixture result.

## Run

From the repository root with Docker Engine/Compose and the locked Python environment:

```sh
.venv/bin/python scripts/immich_fixture.py run --state-dir /tmp/photo-wall-immich-check
```

Set standard `DOCKER_HOST` and `DOCKER_CONFIG` explicitly if the test host needs them. The fixture uses no preexisting Immich configuration. The default cleans up the project's services/volumes even on a failed check; use `--keep` only when continuing integration against that disposable instance. Then run:

```sh
.venv/bin/python scripts/immich_fixture.py cleanup --state-dir /tmp/photo-wall-immich-check
```

## Executed evidence

The first attempt on 2026-09-05 built its image and started PostgreSQL/Valkey, then Docker Desktop left application containers in `Created` without an application process or `State.Error`. A separate credential-free, networkless Python container reproduced the failure; memory was not exhausted. No account or media upload had occurred. The fixture launch clients were stopped and only its disposable services/volumes were cleaned up. Private stage evidence remains at `/private/tmp/photo-wall-immich-fixture-first/evidence.json`. The root orchestrator then restarted Docker Desktop after confirming that running containers belonged to this task, restored the development services and verified a new networkless Python container started. This is a recorded test-host failure and recovery, not an Immich application result.

The fresh retry at `/private/tmp/photo-wall-immich-fixture-recovered` reproduced the mount-time stall: only mount-free Valkey started. The harness was therefore changed to copy its script into the image and use Docker-managed volumes exclusively, avoiding Docker Desktop host file sharing. After a second task-scoped Docker recovery, `/private/tmp/photo-wall-immich-fixture-volumes` started all four services. Thus the bind-free setup passed startup on this host; the mechanism of the Desktop stall itself was not independently diagnosed.

The real server returned v2.5.6 (the version the fixture ran then; it now runs v3.1.0), the fixture created ten synthetic JPEGs, disabled ML, and issued a key whose returned permissions exactly matched the three required read permissions. The first Player probe passed central health reachability and failed both upstream DNS and numeric TCP as required. However, the first adapter membership check **failed** after its 120-second deadline: nine favorite assets existed, eight shared a capture timestamp, and page size three repeatedly returned only eight unique candidates with two duplicate rows across the two walks. There were no pending/rejected rows, and the adapter incorrectly reported `ok` without a diagnostic. The missing synthetic label was `orientation-1`. This quiescent reproduction disproves an assumption that repeated offset pagination necessarily converges across equal capture timestamps. Root was notified; the fixture preserves the equal-time test and does not relax expected membership. The run's sanitized `evidence.json` and `runtime/refresh-failure.json` record the failure. Private evidence/runtime exports remain; its obsolete services and volumes were removed after the corrected run passed.

Independent follow-up checks on the same instance used the adapter's existing default page size 100, fitting the synthetic set in a single response. `default-page-evidence.json` remains explicitly partial and does not erase the page-size-three regression:

| Check | Actual result |
|---|---|
| Original identity/geometry | Nine favorites downloaded with exact SHA-1/SHA-256/size and all eight EXIF orientations. Total 18,315 bytes; acquisition median 8.594 ms, maximum 25.857 ms. These tiny JPEG timings are not a video or deployment throughput budget. |
| Restricted runtime key | Upload and favorite update returned 403; genuine empty query returned `ok`; an exceeded member limit returned `source_limit` with no partial assets. |
| Live changes | Newly uploaded older-capture and new-capture fixtures plus a favorite change yielded twelve exact originals, without changing the source definition. This is adapter membership evidence, not yet an ongoing Scene result. |
| Deletion | Membership dropped to eleven. However, the selected deleted asset's detail and original endpoints each returned 400, which the original adapter mapped to `upstream_schema/incompatible`; the harness failed rather than accepting that classification. The [tagged access helper](https://github.com/immich-app/immich/blob/3be8e265cd5bd6ca921ff9e665e274c5de45caa0/server/src/utils/access.ts) deliberately uses HTTP 400 for missing or inaccessible IDs. Root selected the explicit `asset_unavailable` correction, pending rerun. |
| Permission loss | Reducing the runtime key to `user.read` yielded `permission/upstream_permission` with no candidates; original permissions were restored. |
| Upstream outage/recovery | Stopping only Immich yielded `unavailable/upstream_unavailable`; restart returned `ok` and eleven verified original downloads. |
| Network boundary after acquisition | Player DNS and numeric-IP TCP denial both passed again; the fixed central health endpoint remained reachable. |

Recorded platform was `linux/arm64`, Python 3.12.11, httpx 0.28.1, Pydantic 2.11.4 and Pillow 12.3.0. Upstream containers reported PostgreSQL 14.19 (`14.19-1.pgdg12+1`), Valkey 9.0.1 and Node 24.12.0. Evidence contains actual service image IDs/digests and module/lockfile hashes. Root selected a bounded whole-result refetch policy for tied pagination and the selected-asset-400 correction. (Dated record: delivery 4 removed `--page-size`; the named tests set a small page size on Photo Wall's own client, and keyset pages replaced the whole-result refetch, [media module](module-media.md#live-discovery-pagination-and-metadata).)

### Corrected strict run

The fresh default-page-size-three command passed from **21:58:03.945 through 21:59:24.846 UTC on 2026-09-05**:

```sh
DOCKER_CONFIG=/private/tmp/photo-wall-docker-public \
DOCKER_HOST=unix:///Users/matt/.docker/run/docker.sock \
.venv/bin/python scripts/immich_fixture.py run \
  --state-dir /private/tmp/photo-wall-immich-fixture-corrected --keep
```

These Docker client settings are this host's public-registry/socket configuration, not deployment requirements. The full sanitized record is `/private/tmp/photo-wall-immich-fixture-corrected/evidence.json`. Its source state explicitly records HEAD `ee3443c047d9d712c8e7f5c0e34b9fd397efb5d5` plus **uncommitted implementation changes**, rather than claiming the committed foundation alone contains this adapter.

| Exact execution identity | Value |
|---|---|
| Central adapter fixture image | `sha256:885d387693a0b7acd946f291c0400e31a48d3e975232c4c01904997e473fade4` |
| Running `media/immich.py` SHA-256 | `a4f6068aff39bf625c9c4088bd0c8eec258daba5d830afdaf7cf49f157e6d595` |
| Running `media/models.py` SHA-256 | `4e17631766a8091e5b388022de882c82662424ee88ff76b2a2d364c9a8da14cd` |
| Running harness SHA-256 | `35c163d7f802dee6d1d0d95ebcf20955105c229039bf2c4b208a95b48867903e` |

All nine initial favorites, including eight equal-capture originals, were discovered and hash/geometry verified; both discovery and EXIF requests used a small probe plus bounded complete refetch, totaling four search requests. Live changes yielded twelve verified originals; deletion yielded eleven, selected deletion reported `asset_unavailable`, and the prior local original retained its exact hash. Empty/limit checks, runtime upload/update denial, reduced-key permission failure, upstream outage classification and recovery to eleven verified originals passed. The Player probe passed central health reachability and failed upstream DNS/numeric TCP both before and after acquisition. The initial nine tiny JPEG acquisitions had median 11.3 ms and maximum 40.369 ms; these measurements qualify only this synthetic fixture.

No central media gateway delivery, conversion, Player execution, Scene continuation, content-lock persistence, Pi boot or physical rendering result is claimed by this fixture. The corrected disposable project `pw-immich-fixture-935f8f8e2d6a` is retained for the next integration slice; its private runtime export contains a fixture-only key and must not be committed. The three earlier fixture projects and their volumes were cleaned up, preserving their private evidence files. Scoped cleanup is available through the documented command using the corrected state directory.
