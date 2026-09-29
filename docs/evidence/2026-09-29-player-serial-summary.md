# Player serials on collapsed Equipment cards — 2026-09-29

**Evidence class:** local console browser verification. This follow-up belongs to
draft [PR #38](https://github.com/mcurcio/photo-wall/pull/38) and has not been
deployed to the live Kubernetes installation.

Collapsed Player cards previously showed the enrollment ID and standing but
hid the reported hardware serial. With several Pending Players, an operator
had to open each card to match the reported serial. The card's standing line
now shows the final six characters of the reported serial when boot records
provide one. The disclosure button keeps the Player ID as its accessible
name, and expanded details still show the full serial and boot outcome. When
boot facts are unavailable, no serial suffix is invented from the Player ID.

Two focused Chromium tests passed: separate pending Players remain
distinguishable while collapsed, and missing boot facts leave the card and
retire action usable. The production console build, Ruff, documentation link
check across 97 Markdown documents, and `git diff --check` passed. The full
portable suite passed **2,549 tests** with 921 opt-in skips and three warnings.
The local PostgreSQL suite passed **3,178 tests**, with 291 browser and
platform opt-in skips, one documented expected failure and four warnings.
The full Chromium suite passed **277 tests** with three warnings in the
default host timezone, including both new serial tests and the Program
repeated-hour test with its explicit Los Angeles browser context.
The serial remains a reported claim, not physical identity proof; this change
does not qualify actual Pi identification or a live deployment.
