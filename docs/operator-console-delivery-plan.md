# Operator Console Redesign — Delivery Plan (T0 pass)

**Date:** 2026-09-13
**Status:** Delivery working state (NOT the gate artifact). The design of record
is [`operator-console-ux-design.md`](operator-console-ux-design.md); every slice
below traces to it. This file is the vertical-slice/bead plan the owner reads for
go/no-go on the T0 build. It is produced per the `implementation-workflow` model:
green-alone beads, compile-unit boundaries, one frozen page per slice,
depth-before-breadth, docs as their own beads, one full verify per bead.

**Scope (owner-decided).** IN: the full console redesign (J1–J4), the Q4 minimal
backend (`PATCH` + `DELETE /v1/operator/frames/{id}`), and **T0 only** of the
Display/Commissioning dimension. OUT (separate future programs, not sliced here):
T1 (photometric field) and T2 (CEC/actuator dispatch). Where a T0 slice leaves a
seam for T1/T2, the seam is noted and only the gated-off state is built.

---

## 1. Verify-gate reality for this repo

Every bead's "green-alone" is defined against the gate below. There are **two
tiers**: a fast portable+Postgres gate that every bead runs, and a browser gate
that every *frontend* bead additionally runs.

### 1a. The Python gate (exact commands)

Run from repo root, against a running local Compose Postgres. Mirrors
`.github/workflows/checks.yml` (job `portable-and-postgres`) and `AGENTS.md`.

**Step 0 — build `.venv` (it does NOT exist today).** `ls .venv` returns "No such
file or directory" at branch tip, yet every command below is `.venv/bin/...`. Build
it FIRST or the first command hits "no such file":

```
uv sync --frozen                       # builds .venv (uv==0.7.8, Python 3.12.11)
.venv/bin/python -V                     # must print 3.12.11
```

`uv sync --frozen` installs from the committed lock WITHOUT rewriting `uv.lock`
(verified against the memory note; the rewrite hazard is `uv run`, not `uv sync
--frozen`). Confirm `git status` shows `uv.lock` unchanged after sync.

Then the gate proper:

```
.venv/bin/python -m ruff check .
.venv/bin/lint-imports
python scripts/check_docs.py
python scripts/configure.py            # writes .env if absent; idempotent (see §7.2 — absent .env CRASHES loudly, it does not skip)
.venv/bin/python scripts/test_local.py -q --tb=short
```

- `test_local.py` reads `.env` for `PHOTO_WALL_DB_PASSWORD`/`PHOTO_WALL_DB_PORT`,
  **exports `PHOTO_WALL_TEST_DATABASE_URL`** = `postgresql://photo_wall:…@127.0.0.1:54329/photo_wall`
  into the pytest child env (`scripts/test_local.py:20`), and runs `pytest`. The
  `registry` fixture (`tests/conftest.py:31-33`) **skips** every DB test unless that
  URL is set, so Postgres must be up (`docker compose up -d --wait`) or the suite
  silently under-tests. **This is why the bead gate MUST route through
  `test_local.py` (or export the URL itself), never bare `pytest`** — see §1e.
- **AGENTS.md steers agents WRONG for this gate.** `AGENTS.md:5` tells agents to run
  bare `.venv/bin/python -m pytest -q`. That path sets NEITHER
  `PHOTO_WALL_TEST_DATABASE_URL` (so every DB test skips, `conftest.py:31-33`) NOR
  `PHOTO_WALL_BROWSER_TESTS=1` (so every browser test skips, §1c) — a green-but-empty
  run. **The bead run brief OVERRIDES AGENTS.md:** use `test_local.py` for the DB
  tests and the §1c command (with both env vars) for the browser tests.
- `lint-imports` enforces `[tool.importlinter]` in `pyproject.toml`. Its two
  contracts constrain only `contracts`/`player` transport neutrality — there is **no
  contract on `central`'s internal imports**, so T0 backend beads (routes in
  `central/app.py`/`central/registry.py`) cannot violate it. Still run it (it is a CI
  step), but it is NOT a load-bearing guard for T0 backend work; do not treat its
  green as evidence the backend change is sound.
- Dependencies are installed with `uv sync --frozen` (uv==0.7.8, Python 3.12.11).

### 1b. uv.lock hazard (memory-known)

Any `uv run …` (even `uv run pytest`/`uv run ruff`) can silently rewrite
`uv.lock`. **Every bead invokes the interpreter directly** (`.venv/bin/python
-m pytest`, `.venv/bin/python -m ruff`), never `uv run`, and **must `git checkout
-- uv.lock` before landing** if the file shows an incidental change. A bead diff
that touches `uv.lock` without a deliberate dependency change is a review finding.

### 1c. The frontend test reality — a harness ALREADY EXISTS (decision)

There **is** a browser harness: `tests/browser/` uses **pytest-playwright**
(`pytest-playwright==0.9.0`, `playwright==1.62.0`, both in `[dependency-groups]
dev`). `tests/browser/test_operator_browser.py` and `test_operator_content_browser.py`
drive **real Chromium against the production `create_app(...)`** on an ephemeral
loopback listener, backed by the same disposable-schema Postgres `registry`
fixture, and assert on real DOM + real HTTP responses. `page_errors` fails any
test on an uncaught JS exception. CI runs them in the pinned
`mcr.microsoft.com/playwright/python:v1.62.0-noble` image with
`PHOTO_WALL_BROWSER_TESTS=1`.

**Decision: option (a) is already satisfied — reuse this harness; do NOT build a
new one.** Frontend beads add NEW test files to `tests/browser/` (targeting the new
`/console` route, §1d). This is the honest, load-bearing path for green-alone
frontend beads and for mutation probes (edit the React source, rebuild the bundle,
the browser test goes red).

**The two existing browser test files stay GREEN and UNTOUCHED through M1–M6.**
`test_operator_browser.py` and `test_operator_content_browser.py` assert the OLD flat
page served at `/` (its DOM ids and copy). Because `pyproject.toml:57` sets
`testpaths = ["tests"]`, EVERY bead's gate runs the WHOLE `tests/` tree, so those two
files are in every frontend bead's gate. The redesign therefore does NOT replace `/`
in place — it is built at a NEW route (`/console`, §1d) so `/` + `operator.html` +
`operator.js` + both existing test files remain green and unedited until the FINAL
cutover bead (Bead 17, end of M6) retires them in one co-change. No redesign bead
edits either existing test file.

**Command a frontend bead's browser verify runs** (after the fast gate). BOTH env
vars are mandatory — `PHOTO_WALL_BROWSER_TESTS=1` (or every browser test skips, §1e)
and the DB URL (or the `registry`-backed harness skips):

```
PHOTO_WALL_BROWSER_TESTS=1 PHOTO_WALL_TEST_DATABASE_URL=postgresql://photo_wall:…@127.0.0.1:54329/photo_wall \
  .venv/bin/python -m pytest tests/browser/<this-bead-test-file>.py -q --browser chromium --tb=short
```

Run the bead's OWN new test file by node id and assert it PASSED (§1e positive-run
rule), then run the full `tests/browser` directory to confirm no regression. Chromium
must be installed (`playwright install chromium`, ~150 MB download — §7.4). **Tests
are BEHAVIORAL (role/text/visible-state), NOT pixel/SVG-geometry — so any Chromium is
fine for the bead gate and NO bead requires the CI-pinned image for layout fidelity.**
(CI still runs the whole browser suite inside
`mcr.microsoft.com/playwright/python:v1.62.0-noble`, checks.yml:101 — that is the CI
harness, not a per-bead requirement.) **The prior pinned-image requirement for
"geometry-asserting beads" (old Bead 1/Bead 10) is DROPPED:** with no computed-geometry
assertion left, host/CI layout divergence cannot manufacture a false green, so a bead
may verify on host Chromium.

**Cost of this choice (state it):** browser beads are **slow and
heavy** — they need a downloaded Chromium *and* a live Postgres, are gated behind
`PHOTO_WALL_BROWSER_TESTS=1` (skipped in the portable-only run), and take seconds
per test. A bead's mutation probe requires the same environment, **plus a
successful Vite build of the bundle they load** (a broken build = red — see §1f).
The upside: frontend behavior is verified against the *real* served bundle and the
*real* app (role/text/visible outcome), not a mock, so the string-join,
tray-routing, honesty-wording, R4-unreachability, and lying-flag probes are all
genuinely enforced, not asserted on paper. **No Node/jsdom *unit*-test runner is
introduced** — Playwright against the real served bundle stays the one frontend
test dialect; a second jsdom dialect would not exercise CSP or the production HTTP
surface. (The Vite *build* is a separate concern from testing: it produces the
bundle the Playwright tests then load — §1f.)

### 1d. React small-build strategy on a NEW `/console` route (CSP-legal, parallel cutover)

The existing `operator.html` + `operator.js` are vanilla, `script-src 'self'`
(design D-d), served by `index()` at `/` (`central/app.py:344-352`) and
`operator_script()` at `/operator.js` (`central/app.py:354-360`). **Both stay
UNCHANGED through M1–M6.** The redesign is a PARALLEL **React console** — real JSX +
hooks, compiled by Vite into one same-origin bundle (design Q9) — served at a new
route:

- The React source lives in a NEW tree `central/console/` (`package.json`,
  `vite.config.*`, `index.html`, `src/…`). `npm run build` emits the bundle to
  `central/console/dist/` (`index.html` + `assets/index-<hash>.js` + one CSS).
- **`@app.get("/console")`** returns the BUILT `central/console/dist/index.html`
  shell, mirroring `index()` exactly (same `Cache-Control: no-store`, same CSP
  header `default-src 'self'; script-src 'self'; …`). This is the **foundation
  bead's** route (Bead 0, §3).
- The shell loads the built bundle `<script type="module"
  src="/console/assets/index-<hash>.js">` plus one bundled CSS — a **same-origin
  bundle**, `script-src 'self'`-clean, **no CSP change** (app.py:348-350 already
  admits same-origin scripts).
- The bundle assets are served by a **mounted static dir**
  (`app.mount("/console/assets", StaticFiles(directory="central/console/dist/assets"))`)
  **or** a `@app.get` per-asset route mirroring `operator_script()` at
  `central/app.py:354-360` (same `text/javascript`/`text/css`, same `no-store`,
  same `script-src 'self'` origin). **Bead 0 picks one and wires it ONCE;** feature
  beads add React components inside the source tree, NOT new serving routes. **The
  redesign serves from `/console`, NOT `operator.js`** — the old file and its route
  are untouched; no filename collision.

**Cost of the parallel cut (state it plainly):** two consoles coexist during the
build — a little duplicated serving (`/` = old flat page, `/console` = redesign) and
two live route trees. The single risk point is the FINAL cutover bead (Bead 17): it
repoints `index()` to the new console and RETIRES `operator.html` + `operator.js` +
the two old browser test files in ONE co-change. Until that bead lands, `/` keeps
working for the operator with zero regression, and every intermediate bead is
green-alone against an unmodified existing suite. This is the depth-before-breadth-legal
cut (design §14 ordering is preserved; only the mount point moves at the end).

### 1e. False-green defenses (a bead cannot report green having run nothing)

`pytest` SKIP is exit 0. Both env-gated skips (missing DB URL → `conftest.py:31-33`;
missing `PHOTO_WALL_BROWSER_TESTS` → the two `skipif` marks, §1c) read as "green." A
bead's verify step is therefore NOT satisfied by exit 0 — it must assert a POSITIVE
collected/passed count for the tests it targets:

- **Positive-run assertion (every bead).** Run the bead's target test(s) by explicit
  node id and confirm each was PASSED and NOT skipped. A SKIP, a "no tests ran", or
  "collected 0 items" for a targeted test = verification FAILED, not green. Mechanism:
  invoke pytest with `-rN` (report skips) and fail the bead if the summary contains
  `skipped` or `no tests ran` for a targeted node; for DB beads additionally confirm a
  non-zero collected count under the `registry` fixture. Never accept a bare
  directory-level exit 0 as proof the relevant tests ran.
- **Mutation-probe protocol (pass → red → restore).** A probe proves nothing unless its
  target test actually RUNS. For every probe: (1) run the target test UNMUTATED and
  confirm it PASSES (proves it runs in this env); (2) apply the mutation and confirm the
  target goes RED; (3) reverse the edit and confirm it returns to GREEN. "Still green
  after revert" is NOT a valid probe — a probe against a skipped test is theater and
  fails the bead. State this pass→red→restore sequence per probe.
- **No geometry-pinned probes (§1c).** Behavioral probes assert role/text/visible
  outcome, not computed SVG geometry, so no probe needs the CI-pinned image and a
  host/CI layout divergence cannot manufacture a false green or red. (CI still runs
  the full browser suite in the pinned image as its harness.)

### 1f. The JS build gate (every frontend bead)

Every frontend bead's verify now runs a BUILD STEP **before** its Playwright run:
`npm --prefix central/console ci` (first scaffold: `npm install`) then the Vite
build `npm --prefix central/console run build`, which must succeed and emit the
bundle `central/console/dist/…` that `@app.get("/console")` serves. **A broken
build is RED** — a bead whose bundle fails to build has NOT passed its gate,
whatever the rest of the suite reports (a JS syntax/type error fails here, before
any test runs). Only after a green build do the behavioral Playwright tests run:
run-for-real with `PHOTO_WALL_BROWSER_TESTS=1` + the DB URL (§1c), the
positive-run-count assertion (§1e), and the pass→red→restore mutation-probe
protocol (§1e) — all retained. Behavioral assertions use role/text/visible-state;
there are **no** pixel/SVG-geometry assertions. Backend-only beads (5, 6), the
cutover routing/deletions (17), and the docs beads do not build the bundle and skip
this step (the cutover instead confirms the already-built bundle serves at `/`).

---

## 2. Milestones and ordered slices

Ordering follows design §14: tracer first; then the Q4 backend (a dependency of
drag-to-move/delete); then calibration direct-manipulation + the Commissioning
facet (T0); then onboarding/binding; then spatial editing UI; then Showrunner;
then situational honesty. Depth-before-breadth: the **React toolchain + `/console`
shell + the read-snapshot hook (Plane A, `useSnapshot()`) land in the foundation
bead (Bead 0) before the tracer render**; the projection primitive lands in the
tracer before any consumer; the Q4 backend lands before its UI consumer; Plane B
(`useDraft()`) lands in the first calibration slice before spatial editing consumes
it.

| M | Milestone (observable end-to-end behavior) | Beads |
|---|---|---|
| M1 | **Read the wall** — React toolchain + `/console` shell (Bead 0), then a read-only console: per-Surface SVG plan, honest now-showing, read-only Inspector incl. Commissioning facts with hardware gated off. Zero backend beyond serving the built bundle. | 0–4 |
| M2 | **Reposition & remove (API)** — `PATCH` moves a frame; `DELETE` removes one, refused while bound or while a live Run targets it. Observable via HTTP. | 5–6 |
| M3 | **Commission the display** — geometry/gain by direct manipulation, client convex guard, honest lease countdown + conflict states. Introduces Plane B. | 7–8 |
| M4 | **Onboarding & binding** — pending rail, bind/unbind facet, auto-recovery banner, post-bind "Commission the display" CTA. | 9 |
| M5 | **Spatial editing UI** — drag-to-create (`POST`), drag-to-move (`PATCH`), delete (`DELETE`), Unplaced tray. Consumes Plane B + M2. | 10–11 |
| M6 | **Run the show (Showrunner)** — mode toggle, Sources+Refresh, Scenes, Programs, Runs, "why" panel, `calibration_valid` badge, **no** Commissioning surface (R4). Ends with the **cutover** (Bead 17): flip `/` to `/console`, retire the old page + its two tests. | 12–17 |
| M7 | **Situational honesty** — guidance banner, snapshot clock, refresh cadence model. | 18 |

Each milestone ends with a **coherence review** (design-reviewer, design mode) at
the milestone boundary and a **docs bead** (§6). Implementation never runs while a
frame revision is in flight (a downstream gap that changes a frozen page is an
errata entry + a re-cut of that slice, per the workflow).

---

## 3. Beads

Each bead: id + name; the ONE observable behavior; packages/files; the one-page
frozen interface (only what this slice needs); acceptance + mutation probe; risk
tier. Frontend signatures are given as React (JSX) **component + hook** boundaries
with JSDoc types — **contracts, not bodies**. Pure helpers (projection math, the
now-showing join, the convex guard, the capability derivation) stay plain functions
imported by components.

### M1 — Read the wall

#### Bead 0 — `F-shell`: React toolchain + `/console` shell (foundation)
- **Observable:** a built React bundle is served same-origin at `/console`; loading
  `/console` renders the app shell (an empty console frame with the mode-toggle
  mount point); `/`, `operator.html`, `operator.js` are untouched and stay green.
- **Files (NEW route + NEW source tree, NOT a mutation of the old page):**
  **new** `central/console/` React source (`package.json`, `vite.config.*`,
  `index.html`, `src/main.jsx`, `src/App.jsx`, `src/useSnapshot.js`,
  `src/useMutate.js`), the build output `central/console/dist/…` (committed or
  git-ignored per repo convention — decide in the bead), **new**
  `@app.get("/console")` shell route + the bundle-asset serving (mounted static dir
  or per-asset route) in `central/app.py` (mirroring `index()`/`operator_script()`
  at app.py:344-360, same CSP + no-store — the CSP `script-src 'self'` at
  app.py:348-350 already covers the same-origin bundle, **NO change**); **new**
  `tests/browser/test_console_shell_browser.py` (behavioral smoke); **edit**
  `.github/workflows/checks.yml` to add the Node build stage (below). **`/`,
  `operator.html`, `operator.js` are NOT touched** and stay green.
- **CI slot (`.github/workflows/checks.yml`, job `portable-and-postgres`):** add
  `actions/setup-node` + `npm --prefix central/console ci` + `npm --prefix
  central/console run build` **after** `uv sync --frozen` (checks.yml:56) and
  **before** the "Walk through the operator interface in the pinned browser image"
  step (checks.yml:89-106) — the browser container mounts the workspace read-only and
  boots `create_app`, which serves the bundle from `central/console/dist/`, so the
  bundle MUST be built on the host runner first. (If `dist/` is committed rather than
  built in CI, the stage instead verifies the build is reproducible; decide in the
  bead.)
- **Exact dev/build commands (state them):**
  - dev (local only, NOT the gate/CI): `npm --prefix central/console run dev` (Vite
    dev server).
  - build (the gate step, §1f): `npm --prefix central/console ci && npm --prefix
    central/console run build` → emits `central/console/dist/index.html` +
    `dist/assets/index-<hash>.js` + one CSS.
  - bundle output path served by app.py: `central/console/dist/` (shell at
    `/console`, assets under `/console/assets/…`).
- **Frozen interface (introduces two shared primitives as hooks):**
  - **read-snapshot hook — Plane A** (shared primitive #1):
    ```jsx
    /** @typedef {{inventory: object, runtime: object, media: object|null, at: number}} Snapshot */
    /** Plane A: one timestamped, ATOMIC fetch of inventory+runtime(+media) as ONE snapshot
     *  (never independently). Refreshed on focus/after-mutation. Returns the snapshot plus a
     *  refresh fn that replaces Plane A wholesale and NEVER merges into Plane B (a draft hook). */
    export function useSnapshot(); // -> {snapshot: Snapshot|null, refresh: () => Promise<Snapshot>}
    ```
  - **refresh-after-mutate hook** (shared primitive #7):
    ```jsx
    /** SHARED refresh-after-mutate helper: run an operator write, then refresh Plane A exactly
     *  once so the surface reflects new server state. EVERY frontend mutation bead
     *  (8,9,10,11,13,14,15,16) uses this instead of hand-rolling a post-write refresh; Bead 18
     *  then only adds focus/visibility + clock — it does NOT retrofit prior beads.
     *  @returns {(op: () => Promise<T>) => Promise<T>} */
    export function useMutate();
    ```
- **Acceptance:** `npm ci` + `vite build` succeed and emit the bundle; `GET
  /console` returns the shell with the CSP + no-store headers intact; the smoke test
  asserts the shell renders (by role/text). `GET /` still serves the old page
  unchanged.
- **Mutation probe:** break the shell mount (render nothing) → the "shell renders"
  behavioral smoke test goes **red** (pass→red→restore, §1e). Also: introduce a JS
  syntax error → the bead gate goes **red at the build step** before any test runs
  (§1f).
- **Risk:** foundation; introduces the Node toolchain + Vite build into a Python
  repo (the one-time cost stated in Q9). Straight to implement; the build + behavioral
  smoke gate is its enforcement.

#### Bead 1 — `T-plan`: per-Surface SVG plan + Unplaced tray (read-only)
- **Observable:** on load, the selected Surface renders its frames as SVG rects from
  `x_mm/y_mm/width_mm/height_mm`; a Surface filter switches plans; frames with no
  distinct geometry appear in the **Unplaced tray** (by identity/label), not at the
  origin.
- **Files (React source in the existing `/console` tree from Bead 0):** **new**
  `central/console/src/projection.js` (pure projection helper), **new**
  `central/console/src/Plan.jsx`, `central/console/src/UnplacedTray.jsx` (React
  components), wired into `App.jsx`; consumes `useSnapshot()` (Bead 0). **new**
  `tests/browser/test_operator_wall_browser.py` targeting `/console`. **No new
  serving route** (the bundle is already served from Bead 0). **`/`, `operator.html`,
  `operator.js` are NOT touched** and stay green.
- **Frozen interface (introduces one shared primitive; consumes Plane A from Bead 0):**
  - `projection.js` — **mm→px SVG projection + selection helper** (shared primitive #3),
    a pure function imported by `Plan.jsx`:
    ```js
    /** @typedef {{x:number,y:number,w:number,h:number}} Rect */
    /** Frames of one Surface with distinct geometry, projected mm→px for the viewport. */
    export function project(frames, surfaceId, viewport); // -> {placed: Array<{id,rect}>, unplaced: string[]}
    /** True when a frame has no distinct position (legacy origin-stacked) -> Unplaced tray. */
    export function isUnplaced(frame); // -> boolean
    ```
  - `Plan.jsx` / `UnplacedTray.jsx` — React components (props, not render calls):
    ```jsx
    /** Read-only per-Surface plan; renders projected frames as SVG rects. selection is frameId|null. */
    export function Plan({ snapshot, surfaceId, selection, onSelect });
    /** Read-only Unplaced tray list of origin-stacked/geometry-less frames, keyed by frame id. */
    export function UnplacedTray({ snapshot, onSelect });
    ```
- **Acceptance:** a frame at (100,100,300,500) renders on the plan and a frame at
  (0,0) with no distinct geometry is **present in the Unplaced tray** (asserted by
  frame identity/label via `get_by_role`/`get_by_text`, not by coordinates);
  switching Surface re-renders.
- **Mutation probe (tracer probe b, behavioral):** remove the tray routing (send
  every frame to `project`'s placed set) → the behavioral test asserting the
  zero-geometry frame is **present in the Unplaced tray** (by identity) goes **red**.
  Any Chromium (no geometry assertion, §1c).
- **Risk:** straight to implement. React removes most of the heaviness the vanilla
  tracer carried (the toolchain + Plane A now live in Bead 0; SVG is hand-coded JSX,
  no `createElementNS`), so this is lighter than the prior 120-min estimate — see §5.

#### Bead 2 — `T-join`: now-showing join + tile chips
- **Observable:** each frame tile shows **"Scheduled: `<scene_id>`"** + phase, a
  connectivity dot from `observation.connected`, and a `calibration_valid` badge —
  never "LIVE".
- **Files:** `central/console/src/Plan.jsx` (tile chip), **new**
  `central/console/src/join.js` (pure helper); `tests/browser/test_operator_wall_browser.py`
  (extend).
- **Frozen interface — now-showing string join** (shared primitive #4; a pure
  function imported by `Plan.jsx` and the Now-showing facet):
  ```js
  /** Intended now-showing for a frame: string compare entry.target === "frame:"+frameId. */
  export function nowShowing(runtime, frameId); // -> {scene_id, phase}|null
  /** Connectivity fact for a frame from its bound output observation.
   *  JOIN KEY IS COMPOUND: match the frame's binding to its OutputReport on BOTH
   *  player_id AND output_id — outputs PK is (player_id, output_id) (001_registry.sql:20)
   *  and output_id values (e.g. "hdmi0") repeat across players. Joining on output_id
   *  alone resolves the wrong player's port. */
  export function connectivity(snapshot, frameId); // -> "connected"|"disconnected"|"unbound"
  ```
  The join key is the verified **string** `"frame:<id>"` (design §1b), NOT an
  object `{kind,id}`. Chip copy asserts intent, never confirmed playback.
- **Acceptance:** a known Run targeting `frame:abc` renders "Scheduled: …" on tile
  `abc`; a disconnected output shows the disconnected dot.
- **Mutation probes (behavioral):** (tracer probe a) revert `nowShowing` to
  `entry.target.kind === "frame" && entry.target.id === frameId` → a test asserting
  the known run **no longer appears as now-showing on its tile** (by frame identity +
  `scene_id` text) goes **red** (object path matches nothing). (tracer probe c)
  relabel the chip "LIVE" → the honesty test asserting the **label text** makes no
  confirmed-playback claim goes **red**.
- **Risk:** straight to implement.

#### Bead 3 — `T-inspector`: Frame Inspector shell + Binding & Now-showing facets (read-only)
- **Observable:** selecting a frame opens a read-only Inspector with facet tabs
  **Commissioning | Binding | Now-showing**; Binding shows Player/Output; Now-showing
  shows the intended scene + precedence "why".
- **Files:** **new** `central/console/src/Inspector.jsx` (+ `BindingFacet.jsx`,
  `NowShowingFacet.jsx` child components), wired into `App.jsx`;
  `tests/browser/test_operator_wall_browser.py` (extend).
- **Frozen interface — Inspector component + facet contract** (shared primitive #6):
  ```jsx
  /** @typedef {"commissioning"|"binding"|"nowshowing"} Facet */
  /** @typedef {(props: {snapshot: object, frameId: string}) => JSX.Element} FacetComponent */
  /** Frame Inspector: tabbed facets; `facet` defaults to "commissioning". Each facet is a
   *  React component receiving ({snapshot, frameId}); the Commissioning facet component lands
   *  in Bead 4. Facets are composed as children, not registered imperatively. */
  export function Inspector({ snapshot, frameId, facet, onFacet });
  ```
- **Acceptance:** select frame → inspector opens; Binding/Now-showing populate from
  the snapshot; the Commissioning tab exists (populated in Bead 4).
- **Mutation probe:** break the string-join reuse in the Now-showing facet → the
  facet's "why" test goes **red** (shares primitive #4 with Bead 2).
- **Risk:** straight to implement.

#### Bead 4 — `Commission-read`: read-only Commissioning facet + default-closed capability gate
- **Observable:** the Commissioning facet shows committed geometry + `gain` values
  (read), Display facts (`OutputReport.connected` as the **live** readback;
  `FrameProfile` labelled **Frame facts**), the bound Player/Output, and the
  color + power areas rendered **"not yet available"** — driven by the
  default-closed gate with zero backend.
- **Files:** `central/console/src/Inspector.jsx` (compose the Commissioning facet),
  **new** `central/console/src/Commissioning.jsx`, **new**
  `central/console/src/capability.js` (pure `derive`) + `central/console/src/GatedArea.jsx`;
  `tests/browser/test_operator_commissioning_browser.py` (new, targets `/console`).
- **Frozen interface — capability gate** (shared primitive #5; two named T0
  consumers: the color area and the power area):
  ```jsx
  /** @typedef {"absent"|"derived-true"} CapabilityState */
  /** Derive a capability from a REAL WIRED PATH (pure fn). T0 stub: always "absent" (no signal
   *  derivable). T1/T2 SEAM: the single point a real signal is wired later; NEVER an operator
   *  toggle and NEVER a bare stored boolean. Build only the default-closed branch now. */
  export function derive(name, snapshot); // "photometric_calibration"|"display_command" -> CapabilityState
  /** Hardware-control area component: renders the live control (children) ONLY when
   *  state === "derived-true", else an explicit "requires the display-control capability —
   *  not yet available". */
  export function GatedArea({ state, children });
  ```
  `Commissioning.jsx`:
  ```jsx
  /** Read-only Commissioning facet: geometry+gain (committed), Display facts, gated color+power.
   *  Display facts join the frame's binding to its OutputReport on the COMPOUND key
   *  (player_id, output_id) — same rule as connectivity() in Bead 2 (outputs PK is
   *  compound, 001_registry.sql:20); never on output_id alone. */
  export function Commissioning({ snapshot, frameId });
  ```
- **Acceptance:** facet shows committed gain and the two gated areas as
  "not yet available"; FrameProfile fields are labelled Frame facts;
  `OutputReport.connected` is labelled the live Display readback.
- **Mutation probes (behavioral):** (commission-read probe a — lying-flag) force
  `derive` to return `"derived-true"` with no wired path → `GatedArea` must NOT
  render an enabled control; the honesty test (asserting only the "not yet available"
  text is present) goes **red**. (probe c) mislabel a FrameProfile field as live
  Display readback → the provenance test goes **red**. (probe b, R4) is
  first fully enforceable once Showrunner exists (Bead 12); a placeholder assertion
  that Commissioning is Wall-mode-only lands here and is strengthened in Bead 12.
- **Risk:** straight to implement; the lying-flag probe is its enforcement. A light
  correctness note in the M1 coherence review confirms the gate defaults closed.

### M2 — Reposition & remove (Q4 backend)

#### Bead 5 — `B-PATCH`: reposition route (LWW, no token)
- **Observable:** `PATCH /v1/operator/frames/{id}` with a partial placement moves a
  frame; omitted fields keep their stored value; last-write-wins.
- **Files:** `central/registry.py`, `central/app.py`, `tests/test_registry.py`
  (extend), `tests/test_operator_authored.py` or a new `tests/test_operator_frames.py`
  (HTTP-level).
- **Frozen interface:**
  ```python
  class FramePlacement(Model):
      surface_id: Identifier | None = None
      x_mm: float | None = None
      y_mm: float | None = None
      width_mm: float | None = Field(default=None, gt=0)
      height_mm: float | None = Field(default=None, gt=0)
  ```
  ```python
  # central/app.py
  @app.patch("/v1/operator/frames/{frame_id}", dependencies=[Depends(admin)])
  def reposition(frame_id: Identifier, placement: FramePlacement) -> dict: ...
  ```
  ```python
  # central/registry.py — SHARED orientation helper (extract in THIS slice; do NOT
  # duplicate the (h!=w) and ((h>w)!=(hpx>wpx)) formula). FrameCreate.oriented_profile
  # (registry.py:44-49) is a model_validator bound to FrameCreate — NOT reusable as-is;
  # migrate it to call this helper in the same slice so create_frame and place_frame
  # share ONE copy of the invariant (rule-of-two).
  def _orientation_coherent(width_mm: float, height_mm: float,
                            width_px: int, height_px: int) -> bool:
      """True when physical and pixel dimensions agree on orientation:
      height_mm == width_mm OR (height_mm > width_mm) == (height_px > width_px)."""
  ```
  ```python
  # central/registry.py — Registry method
  def place_frame(self, frame_id: str, placement: FramePlacement) -> dict:
      """SELECT * FROM frames WHERE id=%s FOR UPDATE (404 RegistryError('unknown_frame')
      if absent) — the sibling delete_frame locks the row the same way; the lock makes
      the read-merge-write atomic so two concurrent PATCHes cannot interleave a torn
      field combination (still LWW at the row level, design §9a). Merge omitted fields
      from the locked row; re-run the SHARED _orientation_coherent(merged width_mm,
      merged height_mm, stored profile.width_px, stored profile.height_px) — on False
      raise RegistryError('oriented_profile', 422). UPDATE frames SET
      surface_id,x_mm,y_mm,width_mm,height_mm WHERE id. Bumps NO generation, NO
      configuration_revision; never touches the calibration column. Returns the merged
      placement echo. Concurrency: last-write-wins, no token — the one deliberate
      exception to R3 (design §9a)."""
  ```
- **Acceptance:** partial PATCH merges; a merged non-convex orientation 422s;
  unknown id 404s; generation/configuration_revision/calibration unchanged;
  `FrameCreate` still rejects an incoherent create through the shared helper (no
  behavior change from the migration).
- **Mutation probe:** drop the orientation guard → the "merged dims violate
  orientation → 422" test goes **red** (unmutated it PASSES, per §1e). Also: make
  `place_frame` bump `generation` → a test asserting generation is unchanged goes
  **red** (guards the token-free invariant that calibration is not invalidated by a
  move). (This is a backend bead; the frontend consumers Bead 10/11 wrap the PATCH
  call in the `useMutate()` hook, primitive #7, so the plan reflects the move.)
- **Risk:** touches public API but LWW/no logic branch on authz. Straight to
  implement; the invariant tests above are its enforcement.

#### Bead 6 — `B-DELETE`: guarded delete route  **[HIGH-RISK — adversarial review lens]**
- **Observable:** `DELETE /v1/operator/frames/{id}` removes a clear frame; refuses
  with 409 while a live Run targets it (`frame_in_use`) or while it is bound
  (`frame_bound`).
- **Files:** `central/registry.py`, `central/app.py`, `tests/test_registry.py`
  (extend), `tests/test_operator_frames.py` (HTTP guards + runtime guard).
- **Frozen interface:**
  ```python
  # central/app.py — runtime guard in the ROUTE (in-memory, cheap), before the store call
  @app.delete("/v1/operator/frames/{frame_id}", dependencies=[Depends(admin)])
  def remove_frame(frame_id: Identifier) -> dict:
      """Guard 1 (runtime, in route): view = coordinator.runtime.read().project(clock.utc());
      if any run in view.runs with run.phase in ('body','outro') has
      f'frame:{frame_id}' in run.participants -> HTTP 409 'frame_in_use'.
      Predicate is run.phase, a plain str on RunView (runtime.py:178) — do NOT use
      `RunView.active`; `.active` exists only on the internal _Run (runtime.py:231-233),
      NOT on the projected RunView. RunView.participants (runtime.py:179) carries
      'frame:<id>' strings and is built from ALL runs unfiltered (runtime.py:671,
      712-718), so filtering by phase in ('body','outro') is BOTH correct and required
      (completed/cancelled runs must not block a delete). Then delegate to
      registry.delete_frame."""
  ```
  ```python
  # central/registry.py — Registry method; binding guard ATOMIC inside the txn
  def delete_frame(self, frame_id: str) -> dict:
      """SELECT * FROM frames WHERE id=%s FOR UPDATE (404 'unknown_frame').
      SELECT 1 FROM bindings WHERE frame_id=%s -> RegistryError('frame_bound')
      (the bindings FK on frame_id, 001_registry.sql:37, would otherwise raise a
      raw 500; the explicit check returns a clean 409). Else DELETE FROM frames
      WHERE id, audit 'frame_deleted', return {'status':'deleted'}.
      DELETE CLEARS NOTHING ELSE: bindings.frame_id is the ONLY FK into frames(id) in
      the whole schema (verified across all migrations); runs reference frames by
      string, not FK. The binding guard is therefore necessary AND sufficient — do NOT
      cascade or pre-clear outputs/preview/scene/program rows (there are none)."""
  ```
- **Acceptance:** delete a clear frame → 200; bound frame → 409 `frame_bound`;
  frame with a live body/outro Run → 409 `frame_in_use`; unknown id → 404. TOCTOU
  window (design §9a) is benign — assert a delete of an unbound-but-targeted frame
  does not crash a projecting Run (dangling string reference is harmless).
- **Mutation probe:** remove the binding guard → the "delete a bound frame is
  refused" test goes **red** (and would otherwise surface a raw 500 from the FK).
  Remove the runtime guard → the "delete a live-Run frame is refused" test goes
  **red**.
- **Risk tier: HIGH** (deletion + guards + public API). One adversarial review
  lens (correctness/authz): does either guard admit a delete it should refuse, or
  refuse one it should admit; is the binding guard truly atomic in the txn; is the
  runtime phase set (`body`/`outro`) the right "live" definition.

### M3 — Commission the display (Plane B)

#### Bead 7 — `C-draft`: calibration direct-manipulation + client convex guard
- **Observable:** in the Commissioning facet, the operator drags corner/crop
  handles on the SVG; a folded/thin quad (`min(cross) <= 1e-6`) or empty crop snaps
  back with an inline message and **no request**; `gain` edits live as "trying".
- **Files:** `central/console/src/Commissioning.jsx`, `central/console/src/projection.js`
  (handle geometry), **new** `central/console/src/useDraft.js` (the Plane B hook) +
  `central/console/src/convex.js` (pure guard);
  `tests/browser/test_operator_commissioning_browser.py` (extend).
- **Frozen interface — Plane B edit-draft hook** (shared primitive #2; consumers:
  this bead + the lease bead + spatial-editing Bead 10):
  ```jsx
  /** @typedef {{corners:number[][], crop:number[], rotation:number, gain:number}} Trying */
  /** Plane B: component-local draft state for a frame, seeded from its committed calibration.
   *  A snapshot refresh NEVER overwrites this hook's state (framework-enforced two-plane rule).
   *  Returns the live draft and mutators. updateHandles returns {valid, reason?} using the SAME
   *  1e-6 epsilon AND winding as the server (see convex.js). */
  export function useDraft(frameId, committed);
  // -> {trying: Trying|null,
  //     updateHandles: (patch) => {valid: boolean, reason?: string},
  //     clearDraft: () => void}
  ```
  Client guard MUST replicate the server's EXACT convexity test, not just the epsilon:
  the server computes, for each corner index `i`, `cross[i] = (b.x-a.x)*(c.y-b.y) -
  (b.y-a.y)*(c.x-b.x)` over `a=corners[i], b=corners[(i+1)%4], c=corners[(i+2)%4]` and
  rejects when `min(cross) <= 1e-6` (contracts/models.py:57-63, clockwise/screen-space
  winding). The client must use the SAME index rotation and sign convention AND the
  same `1e-6` epsilon (`min(cross) > 1e-6` to accept), or a quad the client accepts
  still 400s server-side. The winding is load-bearing, not only the epsilon.
- **Acceptance:** dragging to a convex quad updates the draft; a folded quad snaps
  back, no request sent; a refresh during editing leaves the draft intact
  (two-plane discipline).
- **Mutation probe:** change the client epsilon to `0` → a "thin quad is rejected
  client-side" test goes **red** (a `1e-6`-thin quad slips to the server). Also:
  make `refresh()` clear Plane B → the "draft survives refresh" test goes **red**.
- **Risk:** straight to implement (the concurrency-carrying commit is Bead 8).

#### Bead 8 — `C-lease`: preview/commit/revert + honest lease countdown + conflict states  **[HIGH-RISK — concurrency review lens]**
- **Observable:** Preview pushes trying values under the 30s lease with a **visible
  countdown**; on expiry the panel reverts and the UI says so (trying values
  retained for Re-preview); Commit saves a new revision or surfaces the exact 409;
  polling `/inventory` while the facet is open surfaces "overtaken / committed
  elsewhere".
- **Files:** `central/console/src/Commissioning.jsx`, `central/console/src/useDraft.js`,
  **new** `central/console/src/useCalibration.js` (preview/commit/revert + countdown +
  overtake poll; commit path wraps in the `useMutate()` hook, primitive #7);
  `tests/browser/test_operator_commissioning_browser.py` (extend, controlled clock).
- **Frozen interface (uses EXISTING `POST /v1/operator/frames/{id}/calibration`):**
  ```jsx
  /** Calibration control hook for the open facet. Carries BOTH concurrency tokens
   *  (expected_revision + expected_generation) on every op. The countdown is driven by the
   *  SERVER's expires_at (no auto-renew); on expiry: explicit "panel reverted — Re-preview",
   *  trying values kept in Plane B (useDraft). Polls /inventory (~5s) while mounted to detect a
   *  revision/configuration_revision advance -> "overtaken". */
  export function useCalibration(frameId); // ->
  // { calibrate: (op: "preview"|"commit"|"revert")
  //     => Promise<{ok:true}|{ok:false, conflict:"revision"|"generation"|"unbound"}>,
  //   countdown: number|null,   // seconds remaining, from the server's expires_at
  //   status: "committed"|"previewing"|"expired"|"overtaken"|"conflict" }
  ```
  Conflict mapping is fixed by design §4b: stale `expected_revision` →
  `calibration_revision_conflict` ("reload and re-review"); stale
  `expected_generation` → `binding_generation_conflict` ("binding changed — no
  longer under your control"); expiry → "panel back on committed".
- **Acceptance:** preview shows countdown 30→0; expiry reverts + retains trying;
  a second tab's commit makes this tab's commit 409 on revision; a bind mid-session
  makes commit 409 on generation; overtake is detected by the poll.
- **Mutation probe:** drop `expected_revision` from the commit payload → the
  "stale commit conflicts" browser test (two tabs) goes **red** (the write silently
  succeeds against stale state). Add a silent auto-renew → the "panel reverts on
  expiry" test goes **red**.
- **Risk tier: HIGH** (concurrency + last-writer-wins lease + optimistic tokens).
  One adversarial review lens (concurrency/correctness): can any write succeed
  against stale state; is the countdown the server's `expires_at` (not a per-tab
  claim); does a refresh ever clobber Plane B.

### M4 — Onboarding & binding

#### Bead 9 — `O-bind`: pending rail + bind/unbind facet + auto-recovery banner + post-bind CTA
- **Observable:** new/replacement Players appear in the **Pending** rail; the
  Binding facet binds a pending Output (`PUT …/binding`) and unbinds (`DELETE
  …/binding`), each carrying `expected_generation`; a returning known Pi shows a
  "Recovered — already bound (serial match, not identity)" banner; after a bind, a
  "Commission the display" CTA routes to the Commissioning facet.
- **Files:** `central/console/src/BindingFacet.jsx` (bind/unbind write), **new**
  `central/console/src/EquipmentRail.jsx` (pending/retired rail) + **new**
  `central/console/src/recovery.js` (pure recovery diff);
  **new** `tests/browser/test_operator_binding_browser.py` (targets `/console`).
  **This bead adds its OWN `/console` binding test — it does NOT extend or inherit the
  old `tests/browser/test_operator_browser.py` (that file asserts the OLD flat page at
  `/`, stays green and UNTOUCHED until the Bead 17 cutover). The redesign's bind
  coverage is built fresh against `/console`, not migrated from the old suite.** The
  bind/unbind calls wrap in the `useMutate()` hook (primitive #7) so Pending updates.
- **Frozen interface (EXISTING routes `PUT`/`DELETE /v1/operator/frames/{id}/binding`):**
  ```jsx
  /** Pending (is_bound=false, retired_at=null) and Retired rails as a component. */
  export function EquipmentRail({ snapshot, onSelect });
  /** Recovery is INFERRED (pure fn): retain the prior snapshot's authority_epoch and diff;
   *  suppressed on true first run (no prior snapshot). Never surfaces the per-boot epoch bump. */
  export function detectRecovery(prevSnapshot, snapshot); // -> string[] recovered playerIds
  /** Bind/unbind carry expected_generation; 409 binding_generation_conflict -> "reload and review". */
  export async function bind(frameId, playerId, outputId, expectedGeneration);
  export async function unbind(frameId, expectedGeneration);
  ```
- **Acceptance:** pending player appears; bind moves it out of Pending and shows
  "Review required"; a stale-generation bind 409s with the reload message; recovery
  banner appears only with a prior snapshot; CTA opens Commissioning.
- **Mutation probe:** suppress `expected_generation` on bind → the stale-bind
  conflict test goes **red**. Show the recovery banner on true first run → the
  "no banner on first run" test goes **red**.
- **Risk:** straight to implement (binding routes are pre-existing and already
  browser-covered; this bead is UI over them).

### M5 — Spatial editing UI (consumes M2 + Plane B)

#### Bead 10 — `S-place`: drag-to-create (`POST`) + drag-to-move (`PATCH`)
- **Observable:** dragging a rectangle on empty canvas creates a Frame at that
  position (`POST /v1/operator/frames` with placement); dragging an existing rect
  repositions it (`PATCH …`, LWW); the plan corrects on the next snapshot.
- **Files:** `central/console/src/Plan.jsx`, `central/console/src/projection.js`
  (px→mm inverse), `central/console/src/useDraft.js` (in-progress drag rect);
  `tests/browser/test_operator_wall_browser.py` (extend).
- **Frozen interface (EXISTING `POST /frames`; NEW `PATCH` from Bead 5):**
  ```jsx
  /** Convert a pointer-drag rect (px) to mm placement using the inverse projection (pure fn). */
  export function dragToPlacement(pxRect, viewport, surfaceId); // -> {surface_id,x_mm,y_mm,width_mm,height_mm}
  export async function createFrame(placement, profile); // POST /v1/operator/frames (wrap in useMutate())
  export async function moveFrame(frameId, placement);   // PATCH /v1/operator/frames/{id} (wrap in useMutate())
  ```
- **Acceptance:** drag-create posts and the new frame appears on the plan (by
  identity); drag-move patches and the frame follows; concurrent moves LWW and
  correct on refresh.
- **Mutation probe (behavioral):** invert the px→mm mapping (drop the viewport
  scale) → a test asserting the **created frame's stored placement matches the
  dragged region** — assert on the resulting `/inventory` mm values / the POST body
  (a behavioral outcome), NOT rendered SVG pixel coordinates — goes **red**. Any
  Chromium (no geometry assertion, §1c).
- **Risk:** straight to implement.

#### Bead 11 — `S-remove`: delete + Unplaced tray drag-out
- **Observable:** deleting a frame calls `DELETE …` and shows the guard message on
  409; a tray frame can be dragged onto the plan (`PATCH`) or deleted; legacy
  origin-stacked frames leave the tray permanently.
- **Files:** `central/console/src/Plan.jsx`, `central/console/src/UnplacedTray.jsx`
  (tray interactions); `tests/browser/test_operator_wall_browser.py` (extend).
- **Frozen interface (EXISTING `DELETE` from Bead 6; `PATCH` from Bead 5; delete/drop
  wrap in the `useMutate()` hook):**
  ```jsx
  /** Delete a frame; on 409 map frame_bound/frame_in_use to the design §9a operator messages. */
  export async function deleteFrame(frameId); // -> {ok:true}|{ok:false, message:string}
  /** Drop a tray frame onto the plan at a mm position via PATCH (reuses moveFrame). */
  export function dropFromTray(frameId, pxRect, viewport, surfaceId);
  ```
- **Acceptance:** delete clear frame removes the rect; delete bound frame shows
  "unbind first"; delete live-Run frame shows "finish/cancel the Run"; tray drop
  places the frame on the plan.
- **Mutation probe:** map a 409 to a generic error → the "delete-bound shows the
  unbind guidance" copy test goes **red**.
- **Risk:** straight to implement (guards enforced server-side in Bead 6).

### M6 — Run the show (Showrunner)

#### Bead 12 — `SR-mode`: mode toggle + Showrunner shell + R4 enforcement + badge
- **Observable:** a top-level Wall/Showrunner toggle; Showrunner shows the
  `calibration_valid` Frame-health badge (status) and **cannot reach** the
  Commissioning facet (R4).
- **Files:** `central/console/src/App.jsx` (mode toggle), **new**
  `central/console/src/useMode.js` (Plane B), **new**
  `central/console/src/Showrunner.jsx`;
  **new** `tests/browser/test_operator_showrunner_browser.py` (targets `/console`).
  **Beads 12–16 build their showrunner coverage in this NEW `/console` file — they do
  NOT extend the old `tests/browser/test_operator_content_browser.py` (it asserts the
  OLD flat page, stays green and UNTOUCHED until the Bead 17 cutover).**
- **Frozen interface:**
  ```jsx
  /** @typedef {"wall"|"showrunner"} Mode */
  /** Current mode + setter, held in Plane B (component-local). */
  export function useMode(); // -> {mode: Mode, setMode: (m: Mode) => void}
  /** Showrunner shell component: Sources/Scenes/Programs/Runs regions; the Commissioning facet
   *  is NOT mounted here (R4 — enforced structurally by composition, not a runtime check). */
  export function Showrunner({ snapshot });
  ```
- **Acceptance:** toggling to Showrunner hides Wall/Commissioning; the badge
  renders from `calibration_valid`.
- **Mutation probe (commission-read probe b, now fully enforceable):** mount the
  Commissioning facet in Showrunner → the R4 "Commissioning unreachable in the show
  layer" test goes **red**.
- **Risk:** straight to implement; the R4 probe is its enforcement (regression lens
  folded into the M6 coherence review).

#### Bead 13 — `SR-sources`: Sources list + Refresh
- **Observable:** Sources render as `name:rev` with a Refresh button
  (`POST …/sources/{ref}/refresh`); no Immich/album language.
- **Files:** `central/console/src/Showrunner.jsx` (Sources region);
  `tests/browser/test_operator_showrunner_browser.py` (extend, `/console`).
- **Frozen interface (EXISTING `GET /v1/operator/media`, `POST …/sources/{ref}/refresh`;
  Refresh wraps in the `useMutate()` hook, primitive #7).**
- **Acceptance:** a Source shows `name:rev`; Refresh posts and updates.
- **Mutation probe:** add "open in Immich" affordance → an Immich-boundary copy
  test (design D-e) goes **red**.
- **Risk:** straight to implement.

#### Bead 14 — `SR-scenes`: Scene authoring (live + per-frame authored)  **[near-ceiling — pre-split seam noted]**
- **Observable:** author a Scene with per-Frame asset choices, hard-filtered by
  profile (`GET …/sources/{ref}/candidates?frame_id=`), saved in one request
  (`PUT …/scenes/{id}/authored`).
- **Files:** `central/console/src/Showrunner.jsx`, **new**
  `central/console/src/SceneAuthoring.jsx`;
  `tests/browser/test_operator_showrunner_browser.py` (extend, `/console`).
- **Frozen interface (EXISTING candidate/authored routes; re-expresses the existing
  `authored`/`renderAuthoredChoosers` logic — a real function at `operator.js:176-262`
  in today's flat page — as a React `SceneAuthoring` component; save wraps in the
  `useMutate()` hook).**
- **Pre-split seam (if > 90 min):** `SR-scenes-live` (live-source scene, explicit
  frames) and `SR-scenes-authored` (per-frame candidate choosers + single-request
  save) split on the authored-choosers compile boundary.
- **Acceptance:** authored per-frame choices save in one request; incompatible
  choices are blocked by profile filtering.
- **Mutation probe:** relax the profile hard-filter → the "incompatible choice
  rejected" test goes **red**.
- **Risk:** straight to implement.

#### Bead 15 — `SR-programs`: Programs (single window + priority) + optional N-window helper
- **Observable:** schedule a Program to a single time window with a priority
  (`PUT …/programs/{id}`); optionally POST N discrete windows via a client helper
  (Q2) — each a real stored Program, no implied recurrence rule.
- **Files:** `central/console/src/Showrunner.jsx` (Programs region);
  `tests/browser/test_operator_showrunner_browser.py` (extend, `/console`).
- **Frozen interface (EXISTING `PUT`/`DELETE /v1/operator/programs/{id}`; writes wrap in
  the `useMutate()` hook).**
- **Acceptance:** a Program saves and lists; removal deletes it; no control implies
  a stored recurrence rule.
- **Mutation probe:** label the N-window helper "recurring rule" → the honesty copy
  test (R2/Q2) goes **red**.
- **Risk:** straight to implement.

#### Bead 16 — `SR-runs`: Run control + activation outcome + "why" panel
- **Observable:** activate now (`POST …/activations`) and show the **synchronous**
  `{status, reason}` truthfully; finish/cancel a live Run; a "why" panel ranks a
  Frame's contributions by precedence.
- **Files:** `central/console/src/Showrunner.jsx`, `central/console/src/join.js`
  (precedence read reuses primitive #4);
  `tests/browser/test_operator_showrunner_browser.py` (extend, `/console`).
- **Frozen interface (EXISTING `POST …/activations`, `POST …/runs/{id}/{operation}`,
  `GET …/runtime`; activate/finish/cancel wrap in the `useMutate()` hook).**
- **Acceptance:** activation renders the sync outcome; finish/cancel work; "why"
  ranks deterministically; the calendar does NOT render `missed_window` history
  (not on any GET — design §5/§6).
- **Mutation probe:** render an invented `expired: missed_window` history row → a
  test asserting only synchronous activation outcomes appear goes **red**.
- **Risk:** straight to implement.

#### Bead 17 — `X-cutover`: flip `/` to the new console, retire the old page + old tests  **[HIGH-RISK — coherence/adversarial review lens]**
- **Observable:** `/` now serves the redesigned console (content parity reached at end
  of M6); the old flat page and its two browser test files are gone; the whole suite is
  green with the redesign as the operator's `/`.
- **Precondition:** content parity — every feature the old page/tests covered
  (bind/retire, calibration preview/commit/revert + gain, sources, scene authoring,
  programs, activations, run control) is now hosted on `/console` and covered by a
  `/console` browser test (Beads 1–16). This bead does NOT add features; it moves the
  mount point and removes the now-redundant old surface in ONE co-change.
- **Files:** `central/app.py` (repoint `index()` to serve the BUILT console shell
  `central/console/dist/index.html` at `/`, keeping its `no-store` + CSP headers; drop
  the `/operator.js` route; the bundle-asset serving from Bead 0 stays), **remove**
  `central/operator.html`, **remove** `central/operator.js`, **remove**
  `tests/browser/test_operator_browser.py`, **remove**
  `tests/browser/test_operator_content_browser.py`. Point the redesign's `/console`
  browser tests at `/` (or keep `/console` as an alias — decide in the bead; simplest
  is to serve the console shell from `index()` at `/` and keep `/console` as an alias
  so no test path churns).
- **Frozen interface:** no new signatures — a routing + deletion change only. `index()`
  keeps its `no-store` + CSP headers (app.py:344-352) and now returns the built console
  shell; the bundle-asset serving (Bead 0) stays. The build gate (§1f) still runs — the
  bundle served at `/` must build green.
- **Acceptance:** `GET /` returns the console shell (module `type="module"` entry, CSP
  intact); the two old test files no longer exist; the full `tests/browser` suite is
  green with zero references to old-page DOM ids; no `/console`-covered feature lost
  coverage (each moved to a redesign test in Beads 1–16).
- **Mutation probe:** leave one old test file in place → it asserts deleted flat-page
  DOM and goes **red** (proves the retirement is a real co-change, not a silent
  deletion of passing tests). Restore the deletion → green.
- **Risk tier: HIGH** — this is the single cutover risk point the parallel build was
  designed around (§1d). One coherence/adversarial review lens: is content parity truly
  complete (every retired old-test assertion has a live `/console` equivalent), does `/`
  keep its CSP/no-store headers, is any operator-reachable path left serving a dead
  route. Folded into the M6 coherence review as its own lens, at implement time.

### M7 — Situational honesty

#### Bead 18 — `H-refresh`: guidance banner + snapshot clock + refresh cadence
- **Observable:** a global bar shows "updated N s ago · Refresh"; a non-blocking
  guidance banner carries first-run; refresh fires on focus/visibility-change,
  after every mutation, and on explicit Refresh; the `/healthz` pill polls ~10s.
- **Files:** `central/console/src/useSnapshot.js` (add focus/visibility trigger +
  clock/age), **new** `central/console/src/Guidance.jsx`;
  `tests/browser/test_operator_wall_browser.py` (extend). At this point (post-cutover)
  the console is served at `/`.
- **Scope note (refresh-after-mutate already discharged):** the "after every mutation"
  cadence is ALREADY provided by the shared `useMutate()` hook (primitive #7,
  introduced Bead 0 and used by every frontend mutation bead 8/9/10/11/13/14/15/16). This
  bead does NOT retrofit prior beads — it adds ONLY the focus/visibility-change trigger,
  the snapshot-age clock, the `/healthz` pill, and the guidance banner.
- **Frozen interface:**
  ```jsx
  /** Seconds since the current snapshot; drives "updated N s ago" (read from useSnapshot). */
  export function snapshotAge(); // -> number
  /** Non-blocking, dismissible first-run guidance component; dismissed flag lives in Plane B. */
  export function Guidance({ snapshot });
  ```
- **Acceptance:** the clock advances and Refresh replaces Plane A only; guidance is
  dismissible and its dismissal survives a refresh (Plane B).
- **Mutation probe:** make Refresh merge into Plane B → the "dismissed guidance
  survives refresh" test goes **red**.
- **Risk:** straight to implement.

---

## 4. Shared primitives (freeze at first-needing slice)

| # | Primitive (React hook / component / pure helper) | Introduced (bead) | Named consumers (≥2) | T1/T2 seam |
|---|---|---|---|---|
| 1 | **Plane A read-snapshot hook** `useSnapshot()` | 0 | `Plan.jsx` (1), `join.js` (2), `Inspector.jsx` (3), `Commissioning.jsx` (4), facet-open poll (8), `Showrunner.jsx` (12) | — |
| 2 | **Plane B edit-draft hook** `useDraft()` | 7 | calibration edit (7,8), spatial drag (10), guidance-dismiss flag (18) | — |
| 3 | **mm→px projection + selection** `projection.js` (pure) | 1 | `Plan.jsx` render (1), corner-handle overlay (7), drag-create/move (10) | — |
| 4 | **now-showing string join** `join.js` (pure) | 2 | tile chip (2), Now-showing facet (3), "why" panel (16) | — |
| 5 | **capability gate** `derive()` (pure) + `<GatedArea>` | 4 | color area (4), power area (4) | `derive()` is the single wire-in point for T1 `photometric_calibration` / T2 `display_command`; T0 builds only the default-closed branch |
| 6 | **inspector facet contract** `Inspector.jsx` | 3 | Binding facet (3), Now-showing facet (3), Commissioning facet (4) | Commissioning facet grows T1/T2 controls in place (no reshape) |
| 7 | **refresh-after-mutate hook** `useMutate()` | 0 | PATCH/DELETE consumers (10,11), calibrate commit (8), bind/unbind (9), sources refresh (13), scenes save (14), programs (15), runs (16) | — |

A primitive is frozen (its one page written) when the first slice that needs it is
cut — `useSnapshot()` + `useMutate()` at the foundation (Bead 0); projection/join/
inspector at M1 (Beads 1–3); `useDraft()` at M3; the capability gate at M1 (Bead 4).
Differences arrive as props/parameters, never as `if consumer === …`. Primitives #1
and #7 land in the foundation bead (Bead 0) so no later mutation bead hand-rolls its
own snapshot or post-write refresh and Bead 18 does not retrofit prior beads.

---

## 5. Sizing

**Implementation beads: 19** (the new **Bead 0** React foundation + 17 redesign +
the Bead 17 cutover). Docs beads: **7** (one per milestone, §6). Total **26 beads**.
**All numbers below are ESTIMATES.**

Estimates are padded for the round-trip cost the earlier draft omitted: every
browser-bearing bead pays a uvicorn-thread boot + Chromium context + disposable-schema
migration per edit-run-probe cycle (seconds each, many cycles), **plus the Vite build
of the bundle it loads** (§1f), on top of coding time. The `(browser)` beads carry that
pad; backend beads (5, 6) and the routing-only cutover (17) carry a smaller one. **Net
sizing effect of the React reversal:** the toolchain adds a one-time foundation bead
(Bead 0) and a small per-bead build step, but React component/hook DX **speeds** most
feature beads (JSX SVG replaces hand-rolled `createElementNS`; hooks replace the
hand-rolled two-plane store) — so the two roughly offset and the tracer (Bead 1)
actually gets **cheaper**, not dearer.

| Bead | Est (min, ESTIMATE) | Notes |
|---|---|---|
| 0 F-shell | 90 | **NEW** — Node toolchain + Vite scaffold + `/console` shell serving + bundle-asset route + CI build stage + behavioral smoke; introduces `useSnapshot()` + `useMutate()` (one-time toolchain cost) |
| 1 T-plan | 80 | **LOWERED from 120** — JSX SVG (no `createElementNS`, no toolchain here — that moved to Bead 0); introduces projection; behavioral (browser) |
| 2 T-join | 70 | (browser) |
| 3 T-inspector | 70 | (browser) |
| 4 Commission-read | 90 | introduces capability gate (browser) |
| 5 B-PATCH | 55 | backend + shared `_orientation_coherent` extraction/migration |
| 6 B-DELETE | 65 | backend, HIGH-RISK |
| 7 C-draft | 95 | introduces Plane B `useDraft()` (browser) |
| 8 C-lease | 105 | HIGH-RISK; keep as its own bead (browser, controlled clock) |
| 9 O-bind | 90 | new `/console` binding test file (browser) |
| 10 S-place | 85 | behavioral (any Chromium — no geometry assertion) (browser) |
| 11 S-remove | 70 | (browser) |
| 12 SR-mode | 80 | R4 enforcement; new `/console` showrunner test file (browser) |
| 13 SR-sources | 60 | (browser) |
| 14 SR-scenes | 110 | **OVER ceiling with pad — pre-split expected** (live / authored) (browser) |
| 15 SR-programs | 80 | (browser) |
| 16 SR-runs | 95 | (browser) |
| 17 X-cutover | 45 | repoint `index()` to the built shell, delete old page + 2 old tests, run full suite; HIGH-RISK (coherence lens) |
| 18 H-refresh | 70 | (browser); refresh-after-mutate already discharged by primitive #7 |
| Docs ×7 | 20–30 each | ~3 h total |

**Rough total (ESTIMATE):** ~25 h implementation + ~3 h docs ≈ **28 hours** (the added
Bead 0 foundation is roughly offset by the cheaper React tracer and faster feature
beads; the prior estimate was 18 impl + 7 docs ≈ 25 beads / ~28 h). **Relative token
weight** (heaviest first): M6 Showrunner (5 beads + cutover, most surface) > M3
Commission (Plane B + concurrency) > M1 Read-the-wall (foundation + three primitives) >
M5 Spatial > M2 backend > M4 binding > M7 honesty.

**Ceiling flags (90 min / 8 agents default):** Bead 14 (`SR-scenes`, 110) sits OVER
the ceiling — **pre-split expected** on the authored-choosers component boundary into
`SR-scenes-live` and `SR-scenes-authored`. Beads 7, 8 are near ceiling; keep each to
its single component/hook set and do not fold in a neighbor's surface. No bead exceeds
2 compile units — frontend beads touch one/two `central/console/src/*.{jsx,js}` files;
backend beads touch `central/` + `tests/`; the foundation touches the new
`central/console/` tree + `central/app.py` + `checks.yml`; the cutover touches
`central/app.py` + deletions.

---

## 6. Docs beads (one per milestone)

The gate doc is already the design of record — docs beads do NOT restate it; they
update operator-facing docs and keep `scripts/check_docs.py` green (it globs all
`docs/**/*.md`, so relative links must resolve).

| Docs bead | After | Updates |
|---|---|---|
| D1 | M1 | README operator section: the wall plan, read-only Inspector, honest now-showing wording |
| D2 | M2 | Document `PATCH`/`DELETE /v1/operator/frames/{id}` (request/response, guards) in the operator API doc |
| D3 | M3 | Commissioning facet + lease/countdown + conflict states operator guidance |
| D4 | M4 | Onboarding/binding + auto-recovery banner guidance |
| D5 | M5 | Spatial editing + Unplaced tray operator guidance |
| D6 | M6 | Showrunner (Sources/Scenes/Programs/Runs) + R4 (no Commissioning at show time) |
| D7 | M7 | Refresh model + snapshot-age + guidance banner; note placeholder cadences are tuned, not gate decisions |

---

## 7. Pre-launch preflight (implementation-workflow §6) — enumerate, do NOT run

Before any unattended build launch on this branch, confirm:

0. **`.venv` — IT DOES NOT EXIST TODAY.** `ls .venv` returns "No such file or
   directory" at branch tip, yet the whole gate is `.venv/bin/...`. Build it FIRST:
   `uv sync --frozen` (uv==0.7.8, Python 3.12.11), then confirm `.venv/bin/python -V`
   prints `3.12.11`. `uv sync --frozen` installs from the committed lock and does NOT
   rewrite `uv.lock` (the rewrite hazard is `uv run`) — confirm `git status` shows
   `uv.lock` unchanged after. Without this step the first gate command hits "no such
   file." Parallel prominence to the `.env` flag below.
1. **Pinned runtime present.** Python `3.12.11` (CI `setup-python`), `uv==0.7.8`;
   `.venv` built via step 0. Verify `.venv/bin/python -V` and that `uv.lock` is
   unmodified at branch tip.
2. **`.env` exists — IT DOES NOT TODAY (and its absence CRASHES, it does NOT skip).**
   `scripts/test_local.py:15` does `(root/".env").read_text()` → `FileNotFoundError`,
   and `compose.yaml:7` uses `${PHOTO_WALL_DB_PASSWORD:?…}` so `docker compose up`
   aborts loudly. Run `python scripts/configure.py` to generate `.env`
   (`PHOTO_WALL_DB_PASSWORD`, `PHOTO_WALL_DB_PORT=54329`) **before** the first bead.
   The SILENT-skip danger is NOT the missing file — it is the missing ENV VARS at run
   time: `PHOTO_WALL_TEST_DATABASE_URL` unset → the `registry` fixture skips every DB
   test (`conftest.py:31-33`); `PHOTO_WALL_BROWSER_TESTS` unset → every browser test
   skips (the two `skipif` marks). Both read as green (see §1e). Route DB tests through
   `test_local.py` (it exports the URL from `.env`) and set `PHOTO_WALL_BROWSER_TESTS=1`
   for browser beads; do NOT rely on AGENTS.md's bare `pytest` (§1a — it sets neither).
3. **Postgres up.** `docker compose up -d --wait` (image `postgres:16.9`, published
   `127.0.0.1:54329`). The `registry` fixture creates/drops a random schema per
   test; deployment data is untouched.
4. **Browser gate reachable — NETWORK DOWNLOAD required first.** `PHOTO_WALL_BROWSER_TESTS=1`
   AND Chromium present. Chromium is a ~150 MB `playwright install chromium` download
   (and the pinned image / `postgres:16.9` are image pulls) — these need network BEFORE
   any network-isolated unattended run; complete them in preflight, not mid-build.
   Tests are **behavioral** (role/text/visible-state), so **any Chromium is fine and
   no bead needs the CI-pinned image** for layout fidelity (CI still runs the whole
   browser suite in `mcr.microsoft.com/playwright/python:v1.62.0-noble`,
   checks.yml:101, as its harness). Without a reachable browser gate, browser beads'
   green-alone and mutation probes cannot run — flag the run as blocked, do not
   declare frontend beads verified.
4a. **Node runtime + a GREEN Vite build — NONE EXIST TODAY (new toolchain).** The
   React redesign needs Node present (**pin the version**, e.g. Node 20 LTS via
   `actions/setup-node` in CI and the same locally) and a first `npm --prefix
   central/console ci` (~a network download of the JS deps — do it in preflight, not
   mid-build) followed by a green `npm --prefix central/console run build` **BEFORE
   bead 1** (indeed Bead 0 establishes it). A broken or absent build = the frontend
   gate is RED (§1f); confirm `central/console/dist/` is emitted and served at
   `/console`. This step sits alongside the venv (step 0), Postgres (step 3), and
   browser-env/Chromium (step 4) preflight; it is required only for frontend beads,
   not the backend beads (5, 6).
5. **Positive-run assertion, UNPIPED on branch tip** before cutting bead 1 (§1e):
   `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` ·
   `python scripts/check_docs.py` · `.venv/bin/python scripts/test_local.py -q`
   · (frontend) the `tests/browser` command from §1c. No `| tee`, no swallowed exit.
   A bead is green only if its TARGET tests report a positive passed count — a SKIP,
   "no tests ran", or "collected 0" for a target = FAIL, not green. Mutation probes
   must show pass→red→restore (§1e); a probe against a skipped test is theater.
6. **Hooks.** No custom git hooks and no `.pre-commit-config.yaml` exist
   (`.git/hooks` are samples only) — nothing runs the gate automatically on
   commit/push, so the bead's verify is the *only* gate; run it explicitly. The
   main loop owns the commit after verify+review pass.
7. **uv.lock guard.** Interpreter invoked directly (never `uv run`); if `uv.lock`
   shows an incidental change, `git checkout -- uv.lock` before landing.

---

## 8. Milestone reviews

One **coherence review** (design-reviewer, design mode, grounded in the real diff)
at each milestone boundary M1–M7, plus the milestone's docs bead. The coherence
pass enforces: errata are applied (not context-only); the frozen pages match the
as-built module surfaces; the two-plane discipline holds across the milestone; and
no `uv.lock`/scope drift landed. The three HIGH-RISK beads (6 B-DELETE, 8 C-lease,
17 X-cutover) additionally get their per-bead adversarial review lens at implement
time (not deferred to the milestone boundary). Bead 17's cutover lens verifies content
parity — every retired old-test assertion has a live `/console` equivalent — before the
old page + its two browser test files are deleted. Bead 12's R4 probe and Bead 4's
lying-flag probe are treated as regression/honesty lenses folded into the M1 and M6
coherence passes.

---

## 9. Confirmations for the owner

- **No slice secretly needs a backend DATA endpoint beyond Q4.** Confirmed. Every
  T0 slice reads existing GETs (`/inventory`, `/runtime`, `/media`, `/healthz`,
  `sources/{ref}/candidates`) and writes existing routes (`PUT`/`DELETE
  …/binding`, `POST …/calibration`, `POST …/sources/{ref}/refresh`,
  `PUT …/scenes`, `PUT`/`DELETE …/programs`, `POST …/activations`,
  `POST …/runs/{id}/{op}`) plus the two Q4 routes. The capability gate at T0 is
  default-closed with **zero backend**. The one non-data addition is
  **static serving** for the React bundle (Bead 0: `@app.get("/console")` + the
  bundle assets, same pattern as the existing `index()`/`operator_script()`) — no
  new API surface, no schema.
- **T0 seams for T1/T2 that need an explicit stub:** exactly one —
  `capability.js:derive()` returns `"absent"` unconditionally (the single point a
  T1 `photometric_calibration` / T2 `display_command` signal is later wired), and
  the Commissioning color + power areas render "not yet available" via the
  `<GatedArea>` component. Build only the default-closed branch; do not stub a fake
  signal.
- **No migration required.** Confirmed against `central/migrations/001_registry.sql`:
  the `frames` table already has `surface_id, x_mm, y_mm, width_mm, height_mm`
  (lines 22–35). Q4 is routes-only; T0 adds no schema. The `bindings` FK on
  `frame_id` (line 37) is why `DELETE` needs the explicit `frame_bound` guard (a
  raw delete would surface a 500).
