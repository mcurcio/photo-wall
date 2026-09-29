# Operator UX loop: Scene cleanup, Frame profile editing and plain copy — 2026-09-29

**Evidence class:** local software and browser integration. This follow-up is
part of draft [PR #38](https://github.com/mcurcio/photo-wall/pull/38), after the
[first operator UX loop](2026-09-28-operator-ux-loop.md). It has not been rolled
out to the Kubernetes installation or qualified on physical display hardware.

The Scenes library now offers guarded **Delete** for every stored Scene,
including Scenes whose features the console cannot edit losslessly. Central
compares the card's saved revision, then refuses deletion while a Program,
active Run, queued activation, or another stored Scene refers to it. The 409
answer lists the blocking IDs; the console gives the operator the next action.
Deleting an unused definition leaves completed Run snapshots intact. The
confirmation explicitly handles an open unsaved draft of that same Scene.

Commissioning now offers **Edit profile** for the intended panel resolution,
diagonal and video capability. The transaction preserves the persistent Frame
identity, measured placement and authored Scene targets. It requires an
unbound Frame, refuses a live Run or stale equipment generation, and validates
the profile against the aperture orientation. A changed profile clears its
preview, invalidates calibration, and advances calibration, generation and
desired configuration revisions. The UI retains a refused draft, explains how
to clear each refusal, and returns keyboard focus after Save or Cancel.

A further copy pass keeps the optimistic Scene revision in storage and request
bodies while removing numeric revision labels from Scene and Run cards. Scene
editing now describes saved changes and the Reload action in operator terms.
Browser assertions still inspect request revisions and stale conflict handling,
so the simplified copy does not weaken the guard.

| Check | Result and limit |
|---|---|
| Final portable suite (`.venv/bin/python -m pytest -q --disable-warnings -rN`) | 2,547 passed, 907 skipped, 3 warnings. PostgreSQL, Chromium and platform opt-in checks skipped here; the first two ran separately below. |
| Full local PostgreSQL suite during Scene deletion integration | 3,164 passed, 279 skipped, 1 expected failure, 4 warnings. The later Frame profile tests were checked separately because this suite had already collected its tests when that code landed. |
| Focused PostgreSQL routes after Frame profile integration (`tests/test_operator_frames.py`, `tests/test_operator_runtime.py`) | 18 passed, 1 warning. |
| Clean full Chromium browser suite after Scene status fix | 271 passed, 3 warnings. A subsequent keyboard-focus change to the Frame profile editor passed all 6 focused profile browser tests against rebuilt assets. |
| Complete Scene flow and showrunner Chromium modules after the copy pass | 85 passed, 3 warnings. These include stale-edit and request-body revision cases. |
| Ruff, documentation link check, production console build and `git diff --check` | Passed after the changes; the documentation checker covered 94 Markdown documents. |

The previous PR head (`7e53dab`) passed its hosted portable/PostgreSQL, Linux
media, base-image, netboot tracer and two-Player demo jobs. Hosted checks for
this follow-up revision are separate. No real Immich refresh, Pi PXE boot,
HDMI output or visible timing was exercised by these local tests.
