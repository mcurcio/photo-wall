# Kubernetes Player onboarding observation — 2026-09-28

**Evidence class:** live Central HTTP observation and operator report. This is not a
physical display or Player log qualification. The checkout was dirty during
review; no new artifact was deployed.

The operator reported a newly enrolled, unbound Player, heard by Central within
seconds, with one free connected HDMI Output and one Output whose display was not
detected at Player startup. The Equipment roster also said a `v0.12.0` base had
been served but was not yet healthy.

Read-only `kubectl` inspection of the `photos` namespace found the Central
deployment using `ghcr.io/mcurcio/photo-wall/central:v0.12.0`, resolved to
`sha256:4d65e957be93acfb4110589f5c58e649d8d9b51bd8a1de5d740de1ef9c803b61`.
Central's access log showed one Pi completing these requests on
2026-09-29 UTC, corresponding to the local 2026-09-28 observation:

| UTC time | Request | Result |
|---|---|---|
| 03:14:46 | `GET /v1/netboot/base` | 200 |
| 03:14:51 | `GET /v1/app/manifest` | 200 |
| 03:14:54 | `POST /v1/enrollment/register` | 200 |

The same client then posted repeated `POST /v1/player/readiness` requests with
200 responses. No `GET /v1/netboot/manifest` or
`POST /v1/player/base-health` request from that boot appeared in the inspected
60-minute log window. This request path matches the default global `.deb`
bootstrap in [the provisioner](../../appliance/provision.py) and the documented
`PHOTO_WALL_PER_DEVICE_DEB` default in [the runbook](../runbook.md#base-image-auto-mirror-0012):
that path hands no base tag to the Player, so it sends no base-health report.
The pending boot outcome alone therefore does not establish a Player failure.

The successful HTTP reports establish only that Central accepted requests. The
operator's inventory establishes detected versus undetected Outputs at Player
startup. This review did not inspect the Pi's journal, the screen, a binding,
calibration, media delivery, or visible playback. A 200 access-log response
does not by itself prove the report body described a healthy renderer.

## Working-tree change validation

This review changed the local checkout only. The results below qualify the
working-tree code and test fixtures; the Kubernetes deployment remained on
`v0.12.0`, and the Pi did not receive these changes.

| Check | Result and limit |
|---|---|
| `.venv/bin/python -m pytest -q` | 2490 passed, 885 skipped, 25 failed: release-plan tests' `uvx` could not write the default host uv cache under the sandbox. This was an environment failure, not a passing gate. |
| Release-plan retry with `UV_CACHE_DIR` and `UV_TOOL_DIR` in `/private/tmp` | 79 passed. |
| `.venv/bin/python scripts/test_local.py -q --tb=short`, with the writable uv paths and a fresh local Compose PostgreSQL database | 3129 passed, 270 skipped, 1 expected failure, 4 warnings. Browser tests were opt-in and skipped here; the focused browser cases ran separately. |
| Focused Playwright Chromium cases for selected-Frame guidance and all boot-outcome labels | Initial macOS sandbox launch failed before test setup because Chromium could not register a Mach port. A retry with approved unsandboxed execution against the same isolated local database passed all 9 cases. |
| `.venv/bin/python -m ruff check .`; `python3 scripts/check_docs.py`; Vite console build | Passed. Documentation checker validated 92 Markdown files. |
| Linux native renderer image and smoke | First Docker build was denied the sandboxed buildx activity path. Approved local build then passed the Xvfb software-GL smoke and the separate Weston kiosk Output-routing smoke. The smoke checks diagnostic GTK widget visibility transitions, not visible panel pixels. |

The local Compose database was disposable and separate from the live cluster.
The remaining physical check is to deploy a new Player package, boot it on the
Pi, and observe the unbound status page and its removal after binding on the
actual HDMI display.

## First Frame creation follow-up

The operator then reported that drawing a Frame did nothing. Source review found
that the Wall page derived its Surface choices only from existing Frames. On a
new installation, this produced a null Surface; the plan's drag handler returned
without opening the new-Frame form. The earlier browser drag test had seeded an
origin-stacked Frame to provide a `wall` Surface, so it did not cover this case.

The working-tree fix supplies the registry's default `wall` Surface when the
installation has no Frames and labels the empty plan with the drag instruction.
An isolated local Chromium regression started with no Frames, drew one, submitted
it, and verified its stored record and visible tile. The original drag test also
passed: **2 passed**. This is a local browser result; the Kubernetes console
still needs a new deployment to gain the fix.

## Immich Source onboarding follow-up

The operator then reached Photo Sources but found no preconfigured Immich choice.
Read-only inspection of the live `photos` deployment found that the
`seed-connections` init container completed and the worker is running with a
private connection named `immich-main`. The worker's own connection loader,
run as UID 10001, accepted the staged regular file with mode 0600 and found
that connection. The API key is supplied by the `photo-wall-immich` Kubernetes
Secret; its value was not read or printed. The Source form currently derives
suggestions from previously saved Sources, so its first Connection name field
is blank even though the worker has this connection.

The deployed Immich server reports v3.1.0. A read-only check with the worker's
configured connection returned `unsupported_version` because the deployed
Photo Wall adapter accepts only v2.5.6. An isolated request to `GET /users/me`
returned HTTP 403, while `POST /search/metadata` returned HTTP 200 and one
image and one video search row passed the basic head validation. The sampled
image metadata passed the adapter's original-metadata validation; the sampled
video was rejected as `asset_oversize` before duration validation. No media
bytes were requested, no Source was saved or refreshed, and no private URL,
account ID, key, or media identifier was printed. The 403 means the staged key
does not currently satisfy the adapter's required owner check; the tagged
Immich v3.1.0 API declares `user.read` for that endpoint, alongside
`asset.read` for search and `asset.download` for original retrieval.

The working-tree console now explains that Connection name must already be
configured in the worker and does not configure the Immich URL or API key.
The live deployment remains on v0.12.0 until a later release.

After the operator updated the Kubernetes key, the same worker returned HTTP
200 from `GET /users/me`, and the returned account ID matched the configured
owner. The `all-photos:1` Source had two completed refresh attempts with
`status=incompatible`, `diagnostics=[unsupported_version]`, and no successful
refresh. Its Source card said "Awaiting refresh" because it used `last_success`
alone; the local UI change distinguishes "No successful refresh" and displays
the reported issue on that card.

The saved Source selects images from late January 2024 through late September
2026. An isolated read-only image discovery with the existing bounded adapter
returned `source_limit` after two searches; a September 2026-only probe found
199 eligible image rows, and a matching metadata walk validated all 199 image
records; August through September still exceeded the 1,000-candidate limit.
These probes did not save, refresh, or alter the Source.
The current key issue is resolved, but deploying the qualified v3.1.0 adapter
and narrowing the saved Source through Edit are both needed for successful
membership on this wall. No live media bytes or playback were tested.

## Source editing follow-up

The operator found that Source names exposed an internal `:N` revision and that
the console offered only Create and Refresh. The working-tree change presents
plain Source names and adds Edit, rename, and Delete. A save creates a private
immutable revision and moves stored Scene definitions to it; active Runs and
queued activations retain the Source and Scene they captured. Delete is refused
with the dependent Scene names when a Scene still uses the Source. Historical
revisions stop scheduled refresh and release their catalog working set after
no current Source, stored Scene, active Run, or queued activation needs them.
The UI now shows a failed initial refresh and its diagnostic instead of saying
only "Awaiting refresh".

The live Kubernetes Central and worker still serve v0.12.0. These changes have
not been deployed or exercised against live media. The observed September
2026-only image window had 199 eligible records during the read-only probe;
that count can change as Immich membership changes, and a successful refresh
after deployment remains unverified.

Working-tree verification after the Source changes: the full non-PostgreSQL
suite passed with **2,537 passed, 890 skipped, 3 warnings**. The full local
PostgreSQL suite passed with **3,151 passed, 275 skipped, 1 expected failure,
4 warnings**. The Source-specific PostgreSQL cases cover Scene propagation,
active and queued Run snapshots, delete guards, rename, refresh retirement,
lease fencing, reactivation, capacity, and legacy migration. Console flow and
route tests passed **23 cases**; Ruff, documentation links and a production
console build passed. The full Chromium operator-browser suite passed **261
cases, 3 warnings** after an approved macOS sandbox escalation for Chromium's
Mach port; the first sandboxed launch failed before test setup. These results
are local qualification, not a live
Immich refresh, Kubernetes rollout, or physical display observation.
