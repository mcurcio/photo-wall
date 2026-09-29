# Frame-to-Scene handoff and refreshed candidates — 2026-09-29

**Evidence class:** local Chromium against disposable PostgreSQL schemas,
portable and local PostgreSQL suites. These changes extend draft
[PR #38](https://github.com/mcurcio/photo-wall/pull/38). The last read-only
Kubernetes inspection still showed Central at `v0.12.0`; this branch has not
been deployed to the live installation.

A bound, validly calibrated Frame now offers **Choose content for this Frame**
in Commissioning. The Now-showing facet's **Make a Scene** uses the same route.
Both open Scene authoring with that Frame checked on the Frames step. The
operator can add or remove targets before saving; opening authoring never
starts a Run. A previously open new Scene draft stays intact and explains when
the requested Frame was not added. A dirty edit keeps the flow's existing
Resume or Discard protection. Legacy Frame ids outside the Scene target rule
receive an explanation instead of a broken handoff.

For hand-picked Scenes, candidate reads now include the Source's completed
refresh revision. Once the worker publishes a new revision, Central's
per-Frame filtered candidate list is read once again. A valid current choice
stays selected; an item that leaves or becomes ineligible is pruned by the
existing Scene draft rule. **Reload compatible media** provides a manual read
when the outcome of a Source refresh is unknown. The browser regression
publishes a synthetic Source refresh and checks a retained choice, a new
compatible choice, exclusion of an incompatible item, and no repeat read on
the next unchanged poll.

## Local verification

- Portable suite: **2,549 passed, 939 skipped, three warnings**. Browser and
  PostgreSQL cases are opt-in for this invocation.
- Local Compose PostgreSQL suite: **3,178 passed, 309 skipped, one documented
  expected failure, four warnings**. Browser and host-specific checks are
  opt-in here; the browser suite runs separately.
- Focused Chromium checks: six passed across the refresh, Frame handoff,
  existing-draft, commissioning and Now-showing entry cases. A focused
  calibration drag check passed after its test scrolled the lower editor into
  view; the new content action had moved it below the viewport.
- Full Chromium walkthrough after the test scroll correction: **295 passed,
  three warnings**. The first full pass had 294 passed and one failed drag
  action in the existing calibration test because its editor had moved below
  the viewport. The corrected test and entire walkthrough then passed.
- Production console build with the bundled Node runtime, Ruff,
  documentation links (101 Markdown files) and `git diff --check` passed.

The real Immich Source, live Kubernetes rollout, actual HDMI pixels, and
Player-side diagnostic display were not exercised by these local checks.
