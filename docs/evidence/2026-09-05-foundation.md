# Foundation verification — 2026-09-05

Tested code revision: `ee3443c047d9d712c8e7f5c0e34b9fd397efb5d5`.
This is a partial MVP foundation, not end-to-end or physical acceptance.

## Simulated evidence

The full suite includes 8 shared contract/clock checks, 14 SQLite cache checks, 64 Runtime/Planner checks, and 8 real-PostgreSQL registry checks. Runtime/Planner/cache/clock cases are simulated evidence even though their storage or Python code is real. The suite verifies current-state reveal and recording Actuator behavior, pure future projection, nested lifecycle/Program boundaries, admitted snapshots, original eligibility, live candidate changes, byte locks with revisable ordering, stale binding/clock cases, cache pressure, corrupt/partial files, failed unlink, and restart recovery in each implemented module.

Independent reviews: [requirements](../reviews/requirements-initial.md), [contracts](../reviews/contracts-review.md), [registry](../reviews/registry-review.md), plus the focused Runtime/Planner reviewer findings and fixes documented in their [module](../module-runtime.md) [records](../module-planner.md). These are scoped reviews; an integrated correctness review is still required once execution/media/appliance paths exist.

## Clean-checkout integration evidence

Created a detached worktree at the tested revision, with no existing `.venv`, `.env`, or database volume. Ran the documented flow using distinct Compose project `photo-wall-clean`, HTTP port 8011, and database port 54330 to preserve the development deployment:

```sh
uv sync --frozen
python3 scripts/configure.py
# Set only local port overrides in this disposable .env.
docker compose -p photo-wall-clean up -d --build --wait
.venv/bin/python scripts/test_local.py -q
.venv/bin/python -m ruff check .
python3 scripts/check_docs.py
curl --fail http://127.0.0.1:8011/healthz
```

Results: **94 passed**, no skipped tests, one upstream Starlette/AnyIO deprecation warning; lint passed; relative file links in 21 Markdown documents passed; both containers healthy; HTTP health returned `{"status":"ok","database":true,"protocol":1}`. The full test run took 4.55 seconds in this clean checkout. PostgreSQL tests create/drop only their own random schemas. Tests include actual nonce replay/rotation/expiry, concurrent conflicting binding claims, delayed binding retry, Frame preservation/retirement, calibration revision/generation checks, preview expiry/revert through a locally retained configuration, consistent configuration response, and migration/restart behavior.

Environment: macOS ARM64 host, Docker Engine 29.1.3, Docker Compose 5.0.0, CPython 3.12.11, PostgreSQL 16.9; Python dependencies/hashes in `uv.lock`. Public Docker pulls initially stalled in `docker-credential-desktop`; the task stopped that specific stalled invocation and used a temporary empty Docker client config outside Git with the installed Compose plugin. Saved Docker credentials were unchanged.

Central container built from this clean checkout: `sha256:c8a2a48e4e6f3b6d3b3a50338d8508e144ef839705b0d9edfea1acd2c5bc52e3`. This is **not a Pi appliance image**. Base image digests are pinned in Dockerfile/Compose. The independent development service was rebuilt successfully too, preserving its PostgreSQL volume.

## Browser and physical limits

The operator page loaded with central health in the in-app browser; login layout was visually inspected. Full browser workflow verification remains pending: automatic approval review rejected login even with the public fixture token for the disposable localhost:8010 app and separate temporary database schema. User approval was requested. Source inspection found and fixed the preview-form reset defect, but that is not browser execution evidence.

No real Immich integration, Player WebSocket execution, embedded native rendering, image build/boot, PXE, dual-HDMI continuity, appliance update/rollback, or visible cross-Player timing is established by this record. No Pi artifact identity/checksum exists. Physical bench access and visual capture remain requested and unavailable. The [checklist](../implementation-checklist.md) preserves those gates and the remaining engineering work.

Secrets: `.env` remained ignored and mode 0600; staged changes were checked against the generated deployment values before commit and contained none. All test identities/media metadata are synthetic. Binary artifacts stay outside Git.
