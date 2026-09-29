# Exact-time Program edit through a repeated hour — 2026-09-29

**Evidence class:** local browser and deterministic console model tests. This
follow-up belongs to draft [PR #38](https://github.com/mcurcio/photo-wall/pull/38)
and has not been deployed to the live Kubernetes installation.

The Program editor previously disabled Edit when a saved upcoming time fell
in the later occurrence of a repeated local daylight-saving hour. A
`datetime-local` control displays the same clock text for both occurrences;
parsing that text alone would silently change the saved UTC instant. The
editor now retains the exact saved start and end instants while their local
text remains unchanged, including on a Scene or priority edit. Changed fields
use the browser's local-time parsing. The same resolved instants feed
validation and the replacement request, while Central's expected-Program
comparison and future-window guard remain in force. When and Review identify
a retained repeated-hour occurrence with its time-zone abbreviation and UTC
offset.

Focused Node model verification under `TZ=America/Los_Angeles` passed for a
future window whose start and end are both in the later occurrence, plus a
changed-time validation case. Focused Chromium verification under the same
zone passed for replacing only the priority; it asserted that the HTTP
replacement body preserved both original epoch values. The console production
build and `git diff --check` passed during focused verification.

The full portable suite passed **2,549 tests**, skipped 919 database,
browser and platform opt-in cases, and reported three warnings. The local
PostgreSQL suite passed **3,178 tests**, skipped 289 browser and platform
opt-in cases, expected one known failure, and reported four warnings. Two
media-worker tests also passed with FFmpeg hidden from `PATH`, verifying that
their synthetic preparer does not depend on the CI host's media tools. Ruff,
documentation links across 96 Markdown files, production console build and
`git diff --check` passed. The full Chromium suite under
`TZ=America/Los_Angeles` passed **275 tests** with three warnings. The tests establish
stored-time preservation in the configured browser zone; they do not qualify
real wall timing or a deployed browser with a different time-zone setup.
