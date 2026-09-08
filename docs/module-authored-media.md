# Authored media references

The central service exposes a bounded operator flow for turning current source
membership into durable media references. This is the backend contract used by
the operator UI; source connection details, upstream identifiers, URLs, and
credentials never cross this API.

`GET /v1/operator/sources/{source_ref}/candidates` returns the current neutral
candidate data, source status, and snapshot refresh time for a configured
source. An optional `frame_id` filters the list using the persistent Frame
profile and the shared `central.planner.eligible` predicate. The unfiltered
1,000-candidate limit still applies. Missing or failed variants remain visible
with their preparation status; original-media compatibility does not imply
playback readiness. Binding generations do not change the immutable Frame
profile. An unknown Frame returns 404. `POST
/v1/operator/authored-candidates` accepts `{ "source_ref": "…",
"asset_ids": ["…"] }`, with one to 1,000 unique asset IDs. The central
service requires the source's latest refresh status to be `ok`, verifies current
membership, and derives each candidate from its stored `OriginalAsset` metadata.
A failed or unavailable refresh retains old membership for diagnosis and GET
responses but returns `source_not_fresh` (409) for new authored rows. A successful response contains the canonical neutral
references and the number newly created, for example
`{"asset_refs":["asset-…"],"created":1}`. Repeating the same request is
idempotent.

The operator form saves through `PUT /v1/operator/scenes/{scene_id}/authored`
with `{ "scene": <Scene>, "source_ref": "…", "asset_ids": ["…"] }`.
It requires exactly the unique asset references used by that Scene's body,
outro and nested children, and rejects live source references in this operation.
The Coordinator takes its coordination lock, the Runtime lock, then the media
lock in one PostgreSQL transaction. It rechecks freshness, membership,
immutable metadata, capacity and each Frame's original-media compatibility,
and saves the references and Scene together. Any failure rolls back both.
The response contains `status: "configured"`, `scene_id`, `revision`,
`asset_refs` and `created`. Repeating a successful current-source request
creates no extra authored rows. Live Scene saves use the existing Scene PUT.

Per-Frame candidate reads are advisory and may observe different refresh times.
The atomic save validates all choices against current membership; stale or
incompatible choices cannot gain durable authority through the form. This
reuses the Planner's eligibility rule and preserves its final fail-closed check.
The form updates its missing-choice, replacement, or compatible-choice guidance
after each selection change without rebuilding the chooser controls. The
[browser walkthrough](../tests/browser/test_operator_content_browser.py) checks
real per-Frame choices, prepared-photo labels, guidance changes, and single-request authored saves
using generated public photos and production publication transactions.

Authored rows retain their immutable candidate snapshot after a source no
longer contains the asset. New rows record nullable source provenance so
legacy rows remain readable. The total of source snapshot candidates and
authored rows is bounded by the planner's 10,000 candidate limit. Duplicate
requests do not consume capacity. Unknown assets return 404; known assets that
are no longer members return 409; stale source status, immutable metadata
conflicts and capacity conflicts return 409; malformed requests return 422.
Successfully created authored rows are conservatively retained for the MVP because Scene ownership
and reference-aware deletion are not implemented; they permanently consume the
bounded authored capacity until that lifecycle is added.

Catalog and authored reads share one hydration query. It joins the selected
recipe's ready job and ready blob, and applies the current failure or retry
cooldown. Eviction, corruption, recipe changes, and worker failures therefore
clear stale variants dynamically instead of leaving an old snapshot executable.
Scheduler acquisition, hard planner eligibility, assignment locks, and media
publication remain unchanged. A candidate with no ready variant can be
acquired normally; a candidate in cooldown is excluded from the unsecured
pool while existing locks retain their authority.

The endpoints require the existing operator bearer token. The [runbook](runbook.md) describes the operator controls and private worker
connection provisioning. Worker configuration uses the established wrapper
shape `{ "schema": 1, "connections": [ … ] }` and must be provisioned through
the deployment's existing secret handling; no connection secret belongs in an
authored-candidate request.

Integration coverage lives in `tests/test_authored_media.py`,
`tests/test_authored_scene.py` and `tests/test_authored_compatibility.py`, and uses the
shared isolated PostgreSQL fixture. Run it with:

```sh
PHOTO_WALL_TEST_DATABASE_URL=… .venv/bin/python -m pytest -q tests/test_authored_media.py
```
