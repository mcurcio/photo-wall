# Proposed CI revocation owner transaction — NOT applied

This is a concrete review design, not an instruction to bypass automatic approval.
Pending explicit approval for gate/schema/authentication boundary writes.

## Files
- central/fleet/rollout_gate.py: extract close_in(conn, expected_generation=None, reason) retaining exclusive singleton row lock; ordinary close delegates. Add durable revoked-CI-scope check after gate acquisition in open, renew, require_open_in. No change to previously committed commands or immutable issued holds.
- central/migrations/061_node_ci_publication.sql: unshipped schema adds qualification_sha256 to immutable node_ci_publications; adds append-only node_ci_revoked_scopes keyed exact three aggregate report hashes. Deployed checksum migrations must never be rewritten; current branch has not deployed061.
- central/fleet/node_ci_evidence.py: signed revocation ingestion acquires gate exclusive lock FIRST, then publication head. Validate/pin feed identity and monotonic generation; preserve sticky revoked artifacts. Derive affected scopes from immutable original publication/artifact mapping, append scope veto, close new-admission gate, and append signed state=revoked generation to same durable publication outbox in ONE transaction. Live bytes retain exact scope hashes/image set. Duplicate feed must not issue another generation or renew expiry. Publishing fails closed; it does not reopen gate.
- tests/test_node_ci_evidence.py + tests/test_fleet_rollout_gate.py: concurrent effect holds shared gate while revocation waits; admitted prior command remains prior authority. After revocation commits, new admission and premeasured-old-certification reopen are refused. Outbox delivery failure retains durable closure/veto; exact retry remains same bytes/gen/expiry. A genuinely different nonrevoked report scope requires explicit current certification/CAS; no automatic unveto.

A durable scope veto is necessary because a verifier may measure the old live ConfigMap immediately before waiting for the gate lock. Closing alone would permit that cached certificate to reopen after closure. Matching exact report scope ties veto to actually revoked qualification; current ConfigMap transport delay cannot defeat it. Later executable publisher must deliver immutable outbox bytes in generation order using ConfigMap UID/resourceVersion CAS, configured exact target/key, with replay protection. Successful publishing never opens admission.

## Independent reboot receipt proposal
Exact draft: 2026-09-30-node-reboot-carrier-proposal.md. Keep current authenticated carrier producer equality and scope equality. Retain original command SHA, original command_session_id and producer equality against canonical stored command. Only remove requirement that receipt carrier session equal original command session; encoded original response remains unchanged. No command creation, deadline renewal, permit, reboot or invocation occurs. Tests must cover lost reply before/after storage, expired original session/new carrier, exact preserved original received_at on duplicate, forged replacement session and wrong boot/incarnation refusal. This proposal is also UNAPPLIED following separate automatic-review rejection.

## Guard dependency
Guard062 must use the same owner close_in port and gate-first serialization. Implementing equivalent gate mutation indirectly through a new guard module would bypass the rejection, so no such change is applied. Guard service/IaC remain absent. Continue only read-only review/design until authorization resolves.
