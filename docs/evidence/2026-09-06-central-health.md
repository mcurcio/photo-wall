# Central coordination health — 2026-09-06 Pacific

Independent review found that `/healthz` and the operator's “Central healthy”
indicator could stay green when PostgreSQL was reachable but the scheduler's
coordination or acquisition-request cycle kept failing. The endpoint previously
reported database connectivity alone.

The corrected endpoint requires database health and, when enabled, a running
scheduler with a successful complete tick within ten monotonic seconds.
Startup, sanitized coordination errors, stalled work and a stopped scheduler
return HTTP 503. A later complete successful tick restores health. UTC tick
timestamps remain diagnostic; UTC clock steps do not affect freshness.
Explicit scheduler-disabled test mode retains database-only semantics.

**Six focused behavioral tests passed** for database failure, coordination
failure/recovery, acquisition failure/recovery, startup, staleness across UTC
steps and shutdown. The combined PostgreSQL-backed suite passed **840 tests /
15 explicit host/integration skips / 4 dependency warnings in 97.61 seconds**.
Five skips are the separately executed live systemd update scenarios; the
other ten require GNU tar, Linux filesystem/TFTP tools or root UID behavior.

Spark independently reviewed the final health change and found no blocking
startup or correctness issue. The [runbook](../runbook.md) documents the new
health contract. This endpoint does not qualify visible presentation, and the
authenticated operator browser walkthrough remains pending.
