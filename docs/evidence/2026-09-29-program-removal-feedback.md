# Program removal feedback — 2026-09-29

**Evidence class:** local browser and PostgreSQL checks. This follow-up belongs
to draft [PR #38](https://github.com/mcurcio/photo-wall/pull/38); it has not
been deployed to the live Kubernetes installation, which read-only inspection
still found on Photo Wall `v0.12.0`.

An upcoming or past Program's **Remove** action previously ignored the DELETE
response. A refusal, server failure, or missing response left the card in place
without an explanation. The card now reports accepted, refused, or unknown
outcomes. It prevents a second request while one is in flight. If Central
accepts removal but the list refresh fails, the card says it is waiting for
the list to update. After a refusal or unknown outcome, the operator reviews
the current Program state before retrying; if it became running or due, the
existing confirmation flow applies. This changes only operator feedback, not
Program or Run execution semantics.

Once a snapshot shows the removed Program absent, the card forgets its request
feedback. Scheduling a later Program with the same plain id therefore starts
with an enabled Remove control and no stale receipt.

The per-card request guard and feedback state are shared with Photo Sources
Refresh through `useCardMutation`. Endpoint-specific response text remains in
each section, and `useMutate` still refreshes the one Central snapshot after
every request, including a transport failure with an unknown outcome.

## Local verification

- Portable suite: **2,549 passed, 935 skipped, three warnings**. Browser and
  PostgreSQL integration cases are opt-in in this invocation.
- Local Compose PostgreSQL suite: **3,178 passed, 305 skipped, one documented
  expected failure, four warnings**. Browser and platform-specific cases are
  opt-in here; the browser suite ran separately.
- Focused Chromium regression: **25 passed, three warnings** across the
  Program removal cases and Photo Sources flow. After the id-reuse cleanup,
  the changed Program and Source cases passed again (**two passed**).
- Full Chromium walkthrough against the final rebuilt console: **291 passed,
  three warnings**. The Program case walks a refusal, a server-error unknown
  outcome, a successful retry, and reuse of the same Program id. Browser
  checks ran with approved macOS sandbox escalation because Chromium cannot
  register its Mach port inside the sandbox.
- The production console build with the bundled Node runtime, Ruff,
  documentation link checks (100 Markdown files) and `git diff --check` passed
  during integration.

The real user's Programs, physical Player output, and Kubernetes rollout were
not exercised by these local checks.
