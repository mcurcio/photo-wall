# Independent registry and enrollment review

Date: 2026-09-05. Reviewer: independent requirements review agent.

Scope: [database boundary](../../central/db.py), [registry](../../central/registry.py), [HTTP adapter](../../central/app.py), its [operator page](../../central/operator.html), registry migrations and [decision 0002](../decisions/0002-registry-and-enrollment.md). Read the existing [PostgreSQL registry tests](../../tests/test_registry.py). Implementation changed during review; dispositions distinguish observed fixes from pending evidence. No implementation files were edited by this reviewer.

## Findings and disposition

| ID / severity | Concrete issue | Correction and evidence | Review disposition |
|---|---|---|---|
| R01 — High | Calibration originally compared only committed calibration revision. Rebinding preserves that revision while incrementing Frame generation, so a delayed calibration commit from the old equipment could validate the replacement using stale equipment correction. | Require expected Frame generation as well as base calibration revision under the Frame lock. Test preview on generation G, rebind to G+1, then submit the old commit; it must fail and leave replacement calibration unvalidated. | Fixed. Root added `expected_generation` to registry/API/UI; the stale-generation replacement regression passed in the independently executed PostgreSQL suite. |
| R02 — High | Preview originally called `refresh()`, which called `loadCalibration()` using committed calibration. This reset the form immediately after preview, so the subsequent Commit submitted the original values instead of the values the operator had previewed. | Preserve the active preview/draft through refresh and commit those exact values. Exercise gain/corner preview→commit and verify the saved/output configuration equals the preview. | Root changed `loadCalibration()` to `f.preview || f.calibration` during review. Source fix inspected; a browser walkthrough remains necessary. |
| R03 — High | Player configuration substitutes preview values into `OutputBinding.calibration` but does not carry the preview expiry or committed fallback calibration. A Player disconnected after receiving a preview cannot implement the documented 30-second expiry/revert. Server-only cleanup cannot repair the disconnected output. | Send committed calibration plus optional preview with its expiry (or an equivalent locally executable lease). Player expiry must restore committed settings without a network round trip, including after rejoin/restart according to the configured policy. Test preview received→central outage→31 seconds elapsed and check actual applied correction. | Fixed at the contract boundary. `OutputBinding` now carries committed calibration, optional preview and expiry; the downloaded-object offline-expiry regression passed. Actual Renderer adoption still belongs to Player integration. |
| R04 — High | `/v1/player/config` reads `bindings` and `execution_bindings` in separate transactions. A binding/calibration change between reads produces one internally contradictory configuration. A controlled real-PostgreSQL probe returned gain `1.0`, configuration revision `3` in `bindings`, and gain `0.4`, revision `4` in `execution_bindings`. | Read one consistent, authority-validated registry snapshot and derive both views from it. Keep lock order consistent with bind/retire: expiring previews acquires Frame locks, so do not then acquire Player locks in an order opposite to mutations. Test calibration change, rebind and token rotation concurrent with config retrieval. | Fixed. Root added one `configuration_for()` snapshot with Player authority lock; a follow-up probe confirmed concurrent calibration updates preserve identical response views. |
| R05 — Medium | `challenge()` originally deleted old same-key rows then inserted without a lock/uniqueness constraint. Concurrent requests could retain multiple same-key challenges and overrun the global 1,024 challenge bound; concurrent first registrations could reach an unhandled unique violation. | Serialize the count/delete/insert and first enrollment path, or enforce equivalent constraints/retry behavior. Test simultaneous same-key challenges and consume/replay behavior. | Root added a shared advisory transaction lock to challenge/enroll during review. An executed probe with eight simultaneous challenges left exactly one outstanding nonce; this fix was independently verified. |
| R06 — Medium | Re-enrollment upserts reported Outputs but leaves omitted previous Outputs unchanged. An executed probe first reported HDMI-A-1/2, then reported only HDMI-A-1; inventory still returned HDMI-A-2 as `connected=True`. The stale equipment remains eligible for operator binding and its observation never indicates loss. | Define each enrollment/equipment report as a snapshot and mark omitted observations disconnected/unknown while preserving desired bindings; alternatively make the protocol explicitly incremental and report removals. Prevent missing equipment from claiming current execution resource readiness. Test dropped/reappearing Output and an existing binding across the change. | Fixed. Enrollment now marks prior observations disconnected before applying the current report; the removed-Output PostgreSQL regression passed. |
| R07 — Medium | Initial enrollment required at least one Output and positive observed dimensions. A fresh Pi booting without an attached/identified display could not appear centrally, contrary to visibility before binding/local setup. | Permit no observed Outputs and unknown dimensions for disconnected/unidentified equipment. Keep observed capability/readiness separate from registration. | Fixed. Enrollment now allows an empty tuple and unknown dimensions; the no-panels PostgreSQL registration regression passed. |

R03/R04 are contract correctness issues in the existing configuration path, not requests to review an absent renderer or coordinator. Their consequences can be prevented now, before Player code depends on the response shape.

## Additional concurrency point

Binding requests currently carry no expected Frame generation or operation identity. A delayed retry of “bind Frame F to Output A” can undo a newer successful move to Output B. This differs from simply repeating a request without intervening work. Add a compare-and-set generation or durable idempotency policy for operator binding changes, and test reordered requests. The current unique Output/Frame constraints correctly prevent simultaneous duplicate occupancy; they do not prevent this stale-write case.

The same distinction applies to previews from concurrent operator tabs: both can share a committed calibration revision while replacing each other's preview. A preview identity or expected configuration revision makes ownership explicit if concurrent editing is supported. At minimum document last-writer preview policy while retaining commit/generation checks.

## Checks performed

Executed synthetic probes against the real local PostgreSQL server using a unique temporary schema, with credentials read as data from the private local environment and never printed. All created schemas were removed. Existing deployment data was untouched.

Observed results:

```text
eight concurrent challenges: surviving nonces=1
outputs after complete report drops HDMI-A-2:
  HDMI-A-1 connected=True
  HDMI-A-2 connected=True
single response calibration values: 1.0 0.4
single response config revisions: 3 4
```

The configuration race used a controlled calibration commit between the two actual registry reads to make the legal interleaving deterministic. An earlier artificial enrollment barrier stopped being usable when the root's advisory lock landed; it timed out because requests correctly serialized. It is not counted as a failed implementation test or proof of an unfixed race.

The transaction wrapper commits on successful exit and rolls back exceptions through psycopg's connection context. Migration application has an advisory lock, checksum verification, and transactional application. Frame uniqueness constraints and separate operator/Player credentials are appropriate foundations. The reviewed token proof covers the signed nonce and equipment report; tokens are hashed at rest; API validation errors avoid echoing submitted secret-bearing bodies. No SQL interpolation or direct credential disclosure was found in the reviewed request paths.

## Trust and evidence limits

The declared provisioning-network trust envelope is explicit and consistent with automatic unbound enrollment. These HTTP/schema probes do not establish TLS deployment, PXE boot trust, generated key storage permissions, image integrity, private network reachability controls, direct-Immich denial or media authorization. Those require their owning implementation and evidence. A key-derived identity and nonce proof do not attest physical hardware; the decision correctly avoids that claim.

Disposition after re-review: R01–R07 are fixed in the reviewed code/contracts, with the UI fix source-inspected and the registry/lease/snapshot fixes exercised as described above. Resolve the additional stale binding-write/concurrent-preview policy before freezing operator mutation semantics. Browser/Renderer adoption, media authorization and appliance qualification remain downstream integration gates; this review does not establish them.


## Follow-up verification

Re-read the latest registry/API/models/UI after the root implemented the findings. Independently executed:

```sh
.venv/bin/python scripts/test_local.py -q tests/test_registry.py
```

Result: **7 passed**, one upstream Starlette/AnyIO deprecation warning, using actual PostgreSQL. The tests cover no-panel enrollment, removed Output observations, retired identity/generation rejection, concurrent Output claims, downloaded-preview expiry/revert, calibration conflict/restart, and operator/Player credential separation.

A separate controlled API probe committed new calibration after `configuration_for()` returned its single snapshot but before serialization. Both `bindings` and `execution_bindings` remained identical at the previous valid revision, while the later committed value was durably present in inventory. This verifies the response-consistency correction without claiming all concurrency behavior is proven. Its temporary schema was removed.

## Orchestrator follow-up

Binding writes now require expected generation; duplicate current binds are no-ops, and a delayed prior transfer cannot undo a later transfer. `test_delayed_binding_retry_cannot_undo_a_newer_transfer` exercises this on real PostgreSQL. Decision 0002 explicitly records last-writer-wins for uncommitted previews at the same base/generation; commits remain revision checked. Browser walkthrough remains pending approval after automatic review rejected the disposable localhost fixture sign-in. This does not upgrade source inspection to browser evidence.
