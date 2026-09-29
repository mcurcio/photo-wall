# Source refresh feedback and Show now Frame recovery — 2026-09-29

**Evidence class:** local Chromium with disposable PostgreSQL schemas, portable
tests, and documentation checks. This change belongs to draft
[PR #38](https://github.com/mcurcio/photo-wall/pull/38). It has not been
deployed to the live Kubernetes installation, which read-only inspection still
found on Photo Wall `v0.12.0`.

On each Photo Sources card, **Refresh** now shows a per-Source request state.
It cannot be submitted twice while its request is in flight. A 202 is labeled
"Refresh requested"; refusal and unknown network/server outcomes have separate
messages. The saved Source status and issue remain the worker's result, and the
request feedback does not expose Central's internal refresh revision. Feedback
is keyed to the exact Source reference so a later revision cannot inherit an
old card's message.

Show now's Scene and Review steps now link an unhealthy target Frame directly
to the Wall Inspector facet named by the shared Frame health classifier.
Healthy Frame chips stay informational, and other uses of the shared Frame
chips do not gain links. Following a link and returning keeps the Show now
draft. The health label reflects Central's latest accepted Player readiness
report, not confirmed visible output. Browser verification exercises a Frame
moving from **No report yet** (Binding) to **Needs commissioning**
(Commissioning), then checks the retained Show now draft and a healthy Frame.

## Local verification

- Portable suite: **2,549 passed, 934 skipped, three warnings**. Browser and
  PostgreSQL integration cases are opt-in in this invocation.
- Local Compose PostgreSQL suite: **3,178 passed, 304 skipped, one documented
  expected failure, four warnings**. Browser and platform-specific cases are
  opt-in here; the browser suite ran separately.
- Focused Chromium runs: the Show now and Photo Sources modules initially had
  **38 passed, one failed** because the new fixture expected commissioning
  before reporting Player readiness. The corrected case and accepted-refresh
  case then passed **two tests**; the corrected test verifies the health
  transition and both Inspector destinations. The first local Chromium launch
  attempts were blocked by macOS sandbox Mach-port registration, so these
  results use the approved test process outside that sandbox.
- Full Chromium walkthrough against the final rebuilt console: **290 passed,
  three warnings**. This includes the accepted, refused, server-error and
  aborted Source refresh requests, plus the Show now recovery route and draft.
- Ruff, documentation link checks (99 Markdown files), production console
  build with the bundled Node runtime, and `git diff --check` passed.

The real Immich refresh, the user's Source catalog, physical HDMI output,
Player-side diagnostic display, and Kubernetes rollout were not exercised by
these local checks.
