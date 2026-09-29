# Scene Source readiness and commissioning resolution check — 2026-09-29

**Evidence class:** local browser and PostgreSQL checks. This follow-up belongs
to draft [PR #38](https://github.com/mcurcio/photo-wall/pull/38); it has not
been deployed to the live Kubernetes installation, which read-only inspection
still found on Photo Wall `v0.12.0`.

The Scene Photos and Review steps now use the same current Source health
classification as the Photo sources cards. They name an awaiting, failing,
overdue, empty or healthy Source and explain the consequence for content. For
a non-healthy current Source, **Refresh Source** requests work without
claiming that a 202 response means the worker finished it. **Manage in Photo
sources** opens the Source list; returning to Scenes and resuming its draft
preserves the selected Source, target Frame and Scene name. A saved Source
reference absent from the current list has no claimed current status or
inline refresh action. A live Scene can still be saved while its Source is
unready; this UI makes that risk visible without changing Central's
admission contract.

Commissioning now compares a connected Output's positive resolution from
the last Player start with the persistent Frame profile. It accounts for a
valid committed 90° or 270° calibration rotation, so a correctly rotated portrait
panel is not flagged; an invalidated calibration does not suppress the warning.
A mismatch names both raw values and asks the operator
to check the display and intended rotation, restart the Player if the
display changed, and unbind/edit/rebind if the profile is wrong. The warning
neither changes the profile nor blocks calibration.

Focused database-backed Chromium verification passed **46 tests** across the
Scene and Commissioning modules. The new Scene cases cover awaiting, failed
and empty Sources on Photos and Review, accepted refresh without draft loss,
and navigation to Photo sources and back. Commissioning cases cover equal,
different, validly rotated and rotation-invalidated resolutions while checking
that the saved profile is unchanged. The portable suite passed **2,549 tests**, with 928 opt-in skips
and three warnings. The local PostgreSQL suite passed **3,178 tests**, with
298 browser and platform opt-in skips, one documented expected failure and
four warnings. Ruff, import contracts, the production console build and
`git diff --check` passed during integration.
The final full Chromium walkthrough passed **285 tests** with three warnings
against the final production console build.

These checks do not establish a successful refresh against the user's real
Immich, current catalog membership, visible panel content, physical display
dimensions, or a Kubernetes rollout.
