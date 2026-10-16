# Authored media references

The central service exposes a bounded operator flow for turning current source
membership into durable media references. This is the backend contract used by
the operator UI; source connection details, upstream identifiers, URLs, and
credentials never cross this API.

`GET /v1/operator/sources/{source_ref}/candidates` returns the Source revision's
whole pool as neutral candidates, newest first, with `ready` (whether the pool has a
complete check) and `count`. Since roadmap [4](roadmap.md#4-real-photo-sources) there
is no candidate cap: a huge pool answers one large list (paging this picker is not
in 4). An optional `frame_id` filters the list using the persistent Frame
profile and the shared `central.planner.eligible` predicate. Missing or failed variants remain visible
with their preparation status; original-media compatibility does not imply
playback readiness. With `frame_id`, each candidate also carries `standing`,
the planner's own verdict for that Frame (`central.planner.candidate_standing`:
`usable`, `preparing`, `failed_to_prepare` or `no_compatible_variant`; a failed
job awaiting its retry reads `failed_to_prepare`). Without `frame_id` there is
no `standing`. Binding generations do not change the immutable Frame
profile. An unknown Frame returns 404. `POST
/v1/operator/authored-candidates` accepts `{ "source_ref": "…",
"asset_ids": ["…"] }`, with one to 1,000 unique asset IDs per request. The central
service verifies that each photo is in the Source's pool now and derives each
candidate from its pool row. A pool keeps what its checks confirmed while Immich is
down or a check failed ([C7](module-media.md#connections-and-health)), so a pool
authors from what it holds whether or not its latest check completed; there is no
freshness refusal. A successful response contains the canonical neutral
references and the number newly created, for example
`{"asset_refs":["asset-…"],"created":1}`. Repeating the same request is
idempotent.

The operator form saves through `PUT /v1/operator/scenes/{scene_id}/authored`
with `{ "scene": <Scene>, "source_ref": "…", "asset_ids": ["…"] }`.
It requires exactly the unique asset references used by that Scene's body,
outro and nested children, and rejects live source references in this operation.
The Coordinator takes its coordination lock, the Runtime lock, then the media
lock in one PostgreSQL transaction. It rechecks pool membership, immutable
metadata and each Frame's original-media compatibility,
and saves the references and Scene together. Any failure rolls back both.
The response is the Scene save's answer, `SceneOnWall` (the stored `scene` and
the `earlier_version` that still plays: [Runtime](module-runtime.md)), with
`asset_refs` and `created`; `…/authored/apply-now` saves and applies it at once
in the same transaction. Repeating a successful current-source request
creates no extra authored rows. Live Scene saves use the existing Scene PUT.

Per-Frame candidate reads are advisory and may observe different checks.
The atomic save validates all choices against current membership; stale or
incompatible choices cannot gain durable authority through the form. This
reuses the Planner's eligibility rule and preserves its final fail-closed check.
The form updates its missing-choice, replacement, or compatible-choice guidance
after each selection change without rebuilding the chooser controls. The
[browser walkthrough](../tests/browser/test_operator_showrunner_browser.py) checks
real per-Frame choices, prepared-photo labels, guidance changes, and single-request authored saves
using generated public photos and production publication transactions.

Authored rows retain their immutable candidate after the Source's pool no
longer contains the photo. New rows record nullable source provenance so
legacy rows remain readable. Neither pools nor authored rows have a count cap
(the Planner's candidate caps went with delivery 4). Unknown assets return 404
(`authored_asset_not_found`); known assets that are not in the pool return 409
(`authored_asset_not_member`); a changed immutable candidate returns 409
(`authored_candidate_immutable`); malformed requests return 422.
Successfully created authored rows are conservatively retained for the MVP because Scene ownership
and reference-aware deletion are not implemented.

**Hand-placed photos from before delivery 4.** Migration 081 rewrote each pre-4
photo record that an authored row names (or whose prepared copy is pinned) to the
new photo record with the same identity, so it can still be prepared from Immich.
If the photo was edited in Immich since, its preparation fails and the operator
picks it again.

Pool and authored reads share one hydration query. It joins the selected
recipe's ready job and ready blob, and applies the current failure or retry
cooldown. Eviction, corruption, recipe changes, and worker failures therefore
clear stale variants dynamically instead of leaving an old variant executable.
Scheduler acquisition, hard planner eligibility, assignment locks, and media
publication remain unchanged. A candidate with no ready variant can be
acquired normally; a candidate in cooldown is excluded from the unsecured
pool while existing locks retain their authority.

The endpoints require the existing operator bearer token. The [runbook](runbook.md#connecting-immich) describes the operator controls and how an Immich
connection is added in Settings › Photo library; no connection secret belongs in an
authored-candidate request.

Integration coverage lives in `tests/test_authored_media.py`, `tests/test_authored_scene.py`,
`tests/test_authored_compatibility.py` and `tests/test_migration_081.py`, and uses the
shared isolated PostgreSQL fixture. Run it with:

```sh
PHOTO_WALL_TEST_DATABASE_URL=… .venv/bin/python -m pytest -q tests/test_authored_media.py
```
