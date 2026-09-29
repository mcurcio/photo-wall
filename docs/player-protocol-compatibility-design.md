# Player control-protocol compatibility after a Central upgrade

**Status:** design proposal for review, not implemented or qualified. **Scope:** F0 in the [v0.13 production-readiness intake](production-readiness-v0.13.md). **Evidence:** the [2026-09-29 Kubernetes diagnosis](evidence/2026-09-29-kubernetes-central-diagnosis.md) and exact v0.12.0/v0.13.0 release sources. This design concerns the Player **application** control-state schema; the base-OS management protocol is proposed separately in the [fleet-control design](player-fleet-control-design.md).

## Failure to prevent

Central v0.13 sends `identify_output` on every state response, even when it is null ([coordinator](../central/coordination.py), [REST/WebSocket routes](../central/app.py)). The published v0.12 Player's `State` accepts only `configuration`, `plan`, `commits`, and `revocations`, with unknown fields forbidden ([service](../player/service.py), [models](../contracts/models.py) at tag v0.12.0). It validates state before starting its readiness loop. The deployed boot downloaded v0.12, enrolled, then repeatedly received HTTP 200 state responses without posting readiness. Player-local logs and its actual running package remain to be collected, but the schema mismatch deterministically predicts this trace. A successful TCP connection, HTTP response, or enrollment is not an accepted application session.

The defect is in response compatibility, so a Central-side repair can restore a still-running v0.12 app on its next retry. A PXE reboot or global package promotion is not a prerequisite for that repair. Base reachability and app acceptance must remain separate operational facts.

## Proposed contract

| Boundary | Proposed rule |
|---|---|
| Legacy application state | A session that has not negotiated a newer control-state schema receives exactly the four v0.12 top-level fields. Omit `identify_output` **entirely**, including when its value is null. Apply the same projection to HTTP `GET /v1/player/state` and WebSocket state messages. |
| New application state | After unchanged enrollment and before opening either state transport, a new Player explicitly offers supported control-state schemas and capabilities through an authenticated hello; Central atomically pins one selection for that authority epoch. Only a session selected for the newer schema may receive `identify_output`. The v0.13 Player already treats an omitted `identify_output` as optional, so it can consume the legacy projection while negotiation is introduced. |
| Identification | If a session has not negotiated `identify_output`, the operator Identify action returns a named unsupported-capability result. Check capability in the **same player-row locked transaction** that records the request, so re-enrollment cannot race a successful response. Clear or suppress a pending Identify request when its target epoch loses capability. It must not claim that the display will flash while silently omitting the command, or send the new field and break an old Player. |
| Protocol naming | Version the **control-state envelope** separately from the existing nested `Plan.protocol = 1`. A new envelope field must not silently redefine that plan protocol. Application package tag/version is diagnostic information, not evidence of supported capabilities. |
| Authority | Schema selection is attached to the fresh enrollment authority epoch. Re-enrollment resets it to **negotiation open**; hello compares the current bearer and epoch **under the player-row lock** before selecting. The first HTTP or WebSocket state read seals an unnegotiated epoch as legacy under that same lock, before coordination delivery. A late or conflicting hello is rejected; identical retries are idempotent. A Central restart, WebSocket reconnect, or concurrent old HTTP request cannot silently change the pinned schema. Unknown preexisting epochs migrate as sealed legacy. Keep lock acquisition ordered so the registry row lock is released before coordination delivery. State projection never grants Frame authority or weakens existing epoch and binding checks. |

Use one wire serializer/projection for both state transports at the Central boundary. The coordinator may retain a rich internal delivery object; avoid duplicating compatibility rules in REST and WebSocket handlers. Do not modify v0.12's immutable published package or rely on a lenient future Player parser to repair that binary. Omitting the field only when null is insufficient: an Identify request would then break the old session.

### Recovery slice before full negotiation

The smallest safe Central recovery slice emits the four-field legacy state to **all unnegotiated sessions** on both transports and explicitly disables/refuses Identify for them. This temporarily makes Identify unavailable to current v0.13 Players until an opt-in package and negotiation arrive. It preserves ordinary execution and readiness. A feature flag that lets an operator send `identify_output` to an unknown client would defeat the compatibility guarantee.

For durable negotiation, add an authenticated `POST /v1/player/hello` **after unchanged enrollment and before the first state read**. A new Player offers supported envelope versions and named capabilities under its existing bearer and authority epoch; Central selects and persists one set for that epoch. Old Players never call hello and retain the exact strict enrollment request/response and four-field state. A new Player talking to an older Central may treat a `404` from **that hello request only** as legacy if it supports that schema; unrelated 404s remain errors. The client must wait for hello acceptance before its first REST or WebSocket state read. Repeating an identical hello is idempotent; a conflicting offer is rejected. An absent offer is legacy. Treat any advertised package release as an observed string, not an authorization or version-selection shortcut. A signed enrollment extension remains an alternative, but would needlessly change an existing strict contract. The exact hello schema, atomic selection and storage migration remain reviewed decisions before implementation.

## Upgrade and failure policy

| Pair | Required outcome |
|---|---|
| v0.12 Central + published v0.12 Player | Existing control and readiness behavior remains the baseline. |
| v0.12 Central + published v0.13 Player | Verify both a warm session and cold re-enrollment with exact packages; the optional Player field suggests core compatibility but does not prove it. |
| Current v0.13 Central + v0.12 Player | Broken as deployed; this is the regression fixture. |
| Repaired Central + v0.12 Player, warm session or cold re-enrollment | Four-field state; accepted readiness; no Identify command. |
| Repaired Central + current v0.13 Player | Core control and readiness work with the legacy projection; Identify awaits explicit negotiation. |
| Negotiating Central + capable Player | The selected newer envelope enables Identify on both HTTP and WebSocket state. |
| Unsupported app after a future compatibility retirement | Central records a named `upgrade_required` outcome if it has verified version/protocol evidence. A new base agent remains reachable; an old base has `OS telemetry unavailable`, so silence alone cannot identify the cause. |

Before deploying a Central release, test it against the immediately prior **published Player package** and the current package, not only source-tree models. Exercise a warm Central upgrade while an old Player retains its token, cold re-enrollment, bound and unbound readiness, empty and active plans, REST and WebSocket delivery, reconnect after Central restart, and rollback to the previous Central. The gate also checks that operator Identify refuses legacy sessions at the server even when a request was queued before a Central restart or re-enrollment. Fleet telemetry must show which enrolled devices are legacy or unknown before retiring an envelope version. Offline/unknown devices need an explicit supported-version floor or operator retirement; absence from current telemetry is not proof they are upgraded. The proposed minimum is every known non-retired device plus the prior published release.

Malformed negotiation, stale authority, or unsupported capability must fail with a bounded named code. A transport error cannot update the accepted-readiness timestamp. The new envelope must preserve plan/commitment identity and current execution invariants in the [execution contract](execution-contract.md).

## Decisions for review

1. **Support window:** retain the legacy envelope for every known non-retired or offline device of unknown app version, with at least one prior published release as a floor; decide the explicit retirement action and longer support window.
2. **Hello shape and storage:** choose the exact authenticated request/response and per-epoch capability record. Hello and the first state read use the current player-row lock to validate bearer/epoch and pin one selection; identical retries are idempotent, late/conflicting ones fail, and registry lock is released before coordination delivery. Unknown preexisting sessions remain sealed legacy through migration and Central restart.
3. **Identify availability during the recovery slice:** accept temporary unavailability to restore v0.12 readiness immediately, or sequence a capable Player release and negotiation before re-enabling it. Do not silently report success.
4. **Release gate:** decide where the exact published `.deb` pair tests run and how a failed compatibility matrix blocks Central deployment. Physical Pi evidence is additional qualification, not a substitute for this gate.

No implementation or deployed remediation is claimed by this document. Close F0 only with the exact-artifact compatibility fixture, Player-local confirmation of the incident, and an observed accepted readiness report after repair.
