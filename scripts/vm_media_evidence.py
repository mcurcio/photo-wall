"""Read-only central evidence for the exact-image native photo scenario.

This joins production worker, authority, commitment and observation records.
It never creates readiness, commits or renderer observations. The caller must
separately establish that the reporting Player is the exact native VM artifact.
"""

from __future__ import annotations

from contracts.models import Layer, Observation, Plan, PlayerConfiguration, Readiness, Variant


def presentation(record: dict, *, player_id: str, authority_epoch: int,
                 frame_id: str, output_id: str, expected_sha256: str | None = None,
                 expected_original_sha256: str | None = None,
                 prior_grants: tuple[dict, ...] = ()) -> dict | None:
    """Return a bounded proof only when all records refer to the same drawing.

Feedback is the latest report, not historical readiness. Its sequence must be
at least the commit's recorded sequence; the production coordinator owns the
historical readiness decision. Both sequence numbers remain explicit in proof.
"""
    try:
        layer = Layer.model_validate(record["layer"])
        plan = Plan.model_validate(record["plan"])
        config = PlayerConfiguration.model_validate(record["configuration"])
        feedback = Readiness.model_validate(record["readiness"])
        observation_payload = dict(record["observation"])
        # Coordination records the central Runtime/Planner handling outcome beside
        # the transport observation. Validate the observation itself strictly.
        observation_payload.pop("handling", None)
        observation = Observation.model_validate(observation_payload)
        variant = Variant.model_validate(record["variant"])
        commit = record["commit"]
        # The coordinator renews a valid row on later ticks. Preserve a real
        # earlier SQL snapshot instead of requiring its latest renewal to
        # predate an already delivered drawing, or inventing historical time.
        if commit["committed_at"] > observation.observed_at:
            keys = ("authority_epoch", "revision", "assignment_id", "group_id")
            commit = next((grant for grant in prior_grants
                           if grant.get("player_id") == player_id and grant.get("plan_id") == plan.plan_id
                           and all(grant.get(key) == commit[key] for key in keys)
                           and grant.get("valid") is True
                           and grant["committed_at"] <= observation.observed_at), commit)
        group = record["group"]
        assignment = layer.assignment_id
        if not (
            (plan.player_id, config.player_id, record["player_id"])
            == (player_id, player_id, player_id)
            and plan.authority_epoch == config.authority_epoch == feedback.authority_epoch
            == observation.authority_epoch == commit["authority_epoch"] == authority_epoch
            and plan.plan_id == feedback.plan_id == observation.plan_id
            and plan.revision == feedback.revision == observation.revision == commit["revision"]
            and assignment == observation.assignment_id == commit["assignment_id"]
            and layer in plan.layers and config.authorizes(layer)
            and layer.frame_id == frame_id and layer.output_id == output_id
            and layer.variant == variant and variant.media_type == "image/jpeg"
            and (expected_sha256 is None or variant.sha256 == expected_sha256)
            and (expected_original_sha256 is None
                 or record.get("original_sha256") == expected_original_sha256)
            and record["blob_state"] == record["job_state"] == "ready"
            and record["blob_digest"] == record["job_digest"] == variant.sha256
            and record["blob_size"] == variant.size
            and assignment in feedback.secured and assignment in feedback.prepared
            and feedback.capacity_ok and feedback.clock_uncertainty <= .1
            and type(commit["readiness_sequence"]) is int
            and 0 < commit["readiness_sequence"] <= feedback.sequence
            and record["commit"]["valid"] is True and commit["valid"] is True
            and group["status"] == "committed"
            and group["id"] == commit["group_id"]
            and group["members"].get(assignment)
            == [player_id, authority_epoch, output_id, layer.binding_generation]
            and observation.status == "presented" and observation.detail == "none"
            and plan.valid_from <= observation.observed_at < plan.valid_until
            and layer.start <= observation.observed_at < layer.end
            and group["starts_at"] <= observation.observed_at < group["valid_until"]
            and commit["committed_at"] <= observation.observed_at <= record["received_at"]
        ):
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return dict(schema=1, player_id=player_id, authority_epoch=authority_epoch,
                frame_id=frame_id, output_id=output_id, assignment_id=assignment,
                run_id=layer.run_id, plan_id=plan.plan_id, revision=plan.revision,
                sha256=variant.sha256, size=variant.size, media_type=variant.media_type,
                original_sha256=expected_original_sha256,
                readiness_sequence=feedback.sequence,
                commit_readiness_sequence=commit["readiness_sequence"],
                committed_at=commit["committed_at"], observed_at=observation.observed_at,
                received_at=record["received_at"], position=observation.position,
                group_id=group["id"])


def read_presentations(connection, *, player_id: str, authority_epoch: int,
                       frame_id: str, output_id: str, source_ref: str,
                       expected_sha256: str | None = None,
                       expected_original_sha256: str | None = None,
                       prior_grants: tuple[dict, ...] = ()) -> list[dict]:
    """Read up to 32 recent joined candidates; no private source metadata exits.

The caller owns the database transaction. SQL compares JSON identities as text
so malformed diagnostic records cannot cause a cast before protocol validation.
"""
    rows = connection.execute("""
        SELECT e.player_id, e.occurred_at AS received_at, e.detail AS observation,
               l.layer, p.manifest AS plan, f.readiness, cfg.configuration,
               b.variant, b.digest AS blob_digest, b.size AS blob_size,
               b.state AS blob_state, j.state AS job_state, j.variant_sha AS job_digest,
               j.result->>'original_sha256' AS original_sha256,
               jsonb_build_object('authority_epoch',c.authority_epoch,'revision',c.revision,
                 'assignment_id',c.assignment_id,'group_id',c.group_id,'valid',c.valid,
                 'committed_at',c.committed_at,'readiness_sequence',c.readiness_sequence) AS commit,
               jsonb_build_object('id',g.id,'status',g.status,'members',g.members,
                 'starts_at',g.starts_at,'valid_until',g.valid_until) AS "group"
        FROM execution_events e
        JOIN execution_commits c ON c.player_id=e.player_id AND c.assignment_id=e.assignment_id
          AND c.authority_epoch::text=e.detail->>'authority_epoch'
          AND c.revision::text=e.detail->>'revision'
        JOIN plan_offers p ON p.player_id=c.player_id AND p.authority_epoch=c.authority_epoch
          AND p.revision=c.revision
        JOIN player_configurations cfg ON cfg.player_id=c.player_id
          AND cfg.authority_epoch=c.authority_epoch
        JOIN player_feedback f ON f.player_id=c.player_id AND f.authority_epoch=c.authority_epoch
        JOIN assignment_locks l ON l.player_id=c.player_id AND l.authority_epoch=c.authority_epoch
          AND l.assignment_id=c.assignment_id
        JOIN coordination_groups g ON g.id=c.group_id
        JOIN media_blobs b ON b.digest=l.layer->'variant'->>'sha256'
        JOIN media_jobs j ON j.variant_sha=b.digest
        JOIN source_members m ON m.asset_id=j.asset_id AND m.source_ref=%s
        WHERE e.kind='observation' AND e.player_id=%s AND c.authority_epoch=%s
        ORDER BY e.sequence DESC LIMIT 32
        """, (source_ref, player_id, authority_epoch)).fetchall()
    proofs = [presentation(row, player_id=player_id, authority_epoch=authority_epoch,
                           frame_id=frame_id, output_id=output_id, expected_sha256=expected_sha256,
                           expected_original_sha256=expected_original_sha256, prior_grants=prior_grants)
              for row in rows]
    return [proof for proof in proofs if proof is not None]


def read_grants(connection, *, player_id: str, authority_epoch: int) -> list[dict]:
    """Capture current real grants before subsequent coordinator renewal."""
    return connection.execute("""
        SELECT c.player_id,p.plan_id,c.authority_epoch,c.revision,c.assignment_id,c.group_id,
               c.committed_at,c.readiness_sequence,c.valid
        FROM execution_commits c JOIN plan_offers p USING(player_id,authority_epoch,revision)
        WHERE c.player_id=%s AND c.authority_epoch=%s AND c.valid
        ORDER BY c.committed_at DESC,c.assignment_id LIMIT 32
        """, (player_id, authority_epoch)).fetchall()


def stale_session(connection, *, player_id: str, current_epoch: int, prior_epoch: int) -> dict:
    row = connection.execute(
        "SELECT authority_epoch FROM players WHERE id=%s AND retired_at IS NULL", (player_id,)
    ).fetchone()
    grants = connection.execute(
        "SELECT count(*) AS n FROM execution_commits c JOIN players p ON p.id=c.player_id "
        "AND p.authority_epoch=c.authority_epoch WHERE c.player_id=%s "
        "AND c.authority_epoch=%s AND c.valid", (player_id, prior_epoch)
    ).fetchone()
    return {"old_session_current": bool(row and row["authority_epoch"] == prior_epoch),
            "valid_grants": grants["n"] if row and row["authority_epoch"] == current_epoch else -1}
