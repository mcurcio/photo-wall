# Operator UX loop: Program edits and measured Frame placement — 2026-09-28

**Evidence class:** local software and browser integration. **Code revision:**
`e6d12a7` on draft [PR #38](https://github.com/mcurcio/photo-wall/pull/38),
following onboarding and Source changes at `0d9e255`. No Kubernetes rollout or
physical display qualification occurred during this loop.

Read-only review of the console found that correcting a Program required Remove
and Create, which could affect a due or running Run. It also found that Frame
placement required pointer dragging, with no direct way to enter measured wall
geometry. These were chosen as the next two independent improvements.

The Program editor opens an upcoming Program at Review, keeps its identity,
offers changes to Scene, window and priority, and uses a transactional compare
against the complete Program originally opened. Central refuses a changed,
missing or already started Program with 409 instead of silently replacing it.
The editor retains stored time values exactly or explains that they cannot be
edited in that browser. The existing Program `PUT` remains available to API
callers; the console uses the guarded replacement route.

The Wall plan now offers **Add frame with measurements** and **Edit placement**.
Both the measured and drag-created paths feed one form using millimetre values.
Selected Unplaced Frames can be positioned through the same editor. The form
checks finite positive dimensions, profile orientation and the origin reserved
for Unplaced Frames before submitting. A drag move still preserves physical
size; measured editing can also change it.

| Check at `e6d12a7` | Result and limit |
|---|---|
| `.venv/bin/python -m pytest -q` with writable uv cache paths | 2,541 passed, 894 skipped, 3 warnings. Browser, database and platform opt-in checks were skipped here and run separately where available. |
| `.venv/bin/python scripts/test_local.py -q` with the local Compose PostgreSQL database | 3,157 passed, 277 skipped, 1 expected failure, 4 warnings. Browser tests were opt-in and skipped in this run. |
| Full local Chromium browser suite via `PHOTO_WALL_BROWSER_TESTS=1 ... scripts/test_local.py -q tests/browser` | 263 passed, 3 warnings. Chromium required approved macOS Mach-port access outside the sandbox. The suite covers Program replacement and measured Frame creation/reposition; it observes the local service and disposable database, not a Player display. |
| Console production build with bundled Node v24.19.0; Ruff; `scripts/check_docs.py`; `git diff --check` | Passed. Documentation checker covered 92 Markdown documents. |

The first PR commit, `0d9e255`, passed its hosted portable/PostgreSQL, Linux
media, base-image, netboot tracer and two-Player demo jobs. The hosted pipeline
for this follow-up revision is separate and remains to be checked after push.
No real Immich refresh, media download, HDMI output, or physical commissioning
was performed for this revision. The live Kubernetes installation remained on
v0.12.0.
