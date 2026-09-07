# Central authority and stateless Player independent review

Date: 2026-09-07. Reviewer: independent architecture/correctness review agent.

Scope: the complete central-authority/stateless-Player working-tree refactor based on revision `04257179b5950c6f0d99f213b29b24e0fd649e9e`. This is a pre-commit review checkpoint. It is not final-revision or built-artifact acceptance evidence.

## Findings and disposition

| Severity | Finding | Disposition |
|---|---|---|
| P1 | A consumed candidate could fail during verification or before a functioning candidate userspace watchdog started, leaving rollback dependent on an external reboot. | Fixed. Trusted initramfs now requires and arms a nowayout kernel watchdog before candidate verification/download; systemd takes it over, pre-root failure forces reboot, and the generic VM supplies and preloads QEMU's supported PCI `i6300esb` watchdog. |
| P1 | The generic VM health probe accepted the superseded durable health shape and rejected the current Player report. | Fixed. The probe accepts the exact volatile schema, rejects durable/unknown forms, and emits bounded release and clock status including RTT, offset, delay, drift, uncertainty, and rejection counters. |
| P1 | The operator UI still treated Player storage as a binding/health gate. | Fixed. Storage errors and output filtering were removed; Installation intent and current equipment/output observations drive binding. |
| P2 | The boot fixture bypassed the shared daemon-builder policy. | Fixed. It now uses the same explicit Buildx `default` builder with `--load`, labels, and network policy as the other locally derived images. |
| P2 | Demo text claimed cache/session/clock evidence that its current run did not record. | Fixed. Player containers now have tmpfs only and no writable volume. The demo document distinguishes wired scenario behavior, focused Player tests, and pending final demo/image evidence. |
| P2 | Media architecture documents still described per-Player transfer renewal and the removed custom worker claim/retry API. | Fixed. The documents now match the global bounded gateway lease and exact job-ID delivery/retry ownership in Procrastinate. |

The follow-up review found no material blocker in these paths or in the new Execution authorization repository. Media authorization now checks current Installation session/configuration and Execution-owned offers/locks through named ports under the caller transaction; Media no longer reads coordination tables directly.

## Verification

The complete PostgreSQL-backed suite passed **982 tests**, with **11 explicit platform/tooling skips** and four dependency deprecation warnings. The standalone demo harness passed **57 tests**. Ruff, Import Linter, documentation-link checking across 63 Markdown files, bytecode compilation, lockfile resolution, and diff whitespace checks passed. The review agent independently ran 218 focused portable tests; its PostgreSQL-dependent tests were skipped in that separate environment.

## Remaining acceptance

This review does not qualify a final commit, rebuilt Player/appliance artifacts, the authenticated browser walkthrough, the full two-Player/three-Output run, the generic-VM native/cache/rollback sequence, or physical Pi PXE/replacement/dual-HDMI/continuity/visible coordination. Those gates remain unchecked, and PR #2 must remain draft until evidence is tied to the final revision.
