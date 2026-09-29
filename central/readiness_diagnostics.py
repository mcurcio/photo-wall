"""Read-only projection of current Player readiness failures for operator views.

The projection joins the last accepted feedback to the latest offered plan and the
caller's Frame snapshot. It deliberately stores no second copy of readiness state.
"""

from __future__ import annotations

from central.installation_models import FrameInventory
from contracts.liveness import SILENT_AFTER_SECONDS
from contracts.models import (
    FrameProfile,
    Identifier,
    Instant,
    Model,
    OutputBinding,
    Plan,
    PlayerConfiguration,
    Readiness,
)

MAX_READINESS_DIAGNOSTICS = 8192


class ReadinessDiagnostic(Model):
    """One current assignment failure, resolved through its exact offered manifest."""

    player_id: Identifier
    authority_epoch: int
    sequence: int
    plan_id: Identifier
    revision: int
    assignment_id: Identifier
    frame_id: Identifier
    output_id: Identifier
    binding_generation: int
    failure_code: str
    received_at: Instant
    observed_at: Instant
    layer_start: Instant
    layer_end: Instant


def _snapshot_bindings(frames: tuple[FrameInventory, ...], player_id: str, read_at: float):
    """Build current binding facts from the caller's same-snapshot Frame inventory."""
    owned = sorted(
        (frame for frame in frames if frame.player_id == player_id and frame.output_id is not None),
        key=lambda frame: frame.output_id,
    )
    bindings = tuple(
        OutputBinding(
            output_id=frame.output_id,
            frame_id=frame.id,
            generation=frame.generation,
            configuration_revision=frame.configuration_revision,
            profile=FrameProfile.model_validate(frame.profile),
            calibration=frame.calibration,
            preview=(frame.preview if frame.preview_expires is not None and frame.preview_expires > read_at else None),
            preview_expires=(frame.preview_expires if frame.preview is not None and frame.preview_expires is not None
                             and frame.preview_expires > read_at else None),
        )
        for frame in owned
    )
    enabled = {frame.output_id for frame in owned if frame.calibration_valid}
    return bindings, enabled, {frame.id: frame for frame in owned}


def project_readiness_diagnostics(
    conn,
    *,
    read_at: float,
    frames: tuple[FrameInventory, ...],
) -> tuple[ReadinessDiagnostic, ...]:
    """Project only failures still authoritative at ``read_at``.

    ``conn`` belongs to the caller and must already be in the desired database
    snapshot. The highest revision is selected before checking expiry: an expired
    latest offer must not revive an older offer. ``frames`` must come from the same
    snapshot, allowing binding/profile/calibration changes to stale old feedback.
    """
    rows = conn.execute(
        "SELECT p.id AS player_id,p.authority_epoch,f.sequence,f.received_at,f.readiness,"
        "c.revision AS config_revision,c.configuration,"
        "o.revision AS offer_revision,o.plan_id AS offer_plan_id,o.valid_until,o.manifest "
        "FROM players p JOIN player_feedback f ON f.player_id=p.id "
        "AND f.authority_epoch=p.authority_epoch "
        "JOIN player_configurations c ON c.player_id=p.id "
        "AND c.authority_epoch=p.authority_epoch "
        "JOIN LATERAL (SELECT revision,plan_id,valid_until,manifest FROM plan_offers "
        "WHERE player_id=p.id AND authority_epoch=p.authority_epoch "
        "ORDER BY revision DESC LIMIT 1) o ON TRUE "
        "WHERE p.retired_at IS NULL"
    ).fetchall()

    diagnostics: list[ReadinessDiagnostic] = []
    binding_cache: dict[str, tuple[tuple[OutputBinding, ...], set[str], dict[str, FrameInventory]]] = {}
    for row in rows:
        received_at = row["received_at"]
        # Future-dated feedback and silent Players are not current operator facts.
        if received_at > read_at or read_at - received_at > SILENT_AFTER_SECONDS:
            continue
        if row["valid_until"] <= read_at:
            continue
        report = Readiness.model_validate(row["readiness"])
        plan = Plan.model_validate(row["manifest"])
        if (
            report.authority_epoch != row["authority_epoch"]
            or report.sequence != row["sequence"]
            or report.plan_id != row["offer_plan_id"]
            or report.revision != row["offer_revision"]
            or plan.player_id != row["player_id"]
            or plan.authority_epoch != row["authority_epoch"]
            or plan.plan_id != row["offer_plan_id"]
            or plan.revision != row["offer_revision"]
            or plan.valid_until != row["valid_until"]
            or report.plan_id != plan.plan_id
            or report.revision != plan.revision
        ):
            continue

        if row["player_id"] not in binding_cache:
            binding_cache[row["player_id"]] = _snapshot_bindings(frames, row["player_id"], read_at)
        current_bindings, enabled_outputs, current_frames = binding_cache[row["player_id"]]
        configuration = PlayerConfiguration.model_validate(row["configuration"])
        if (
            configuration.player_id != row["player_id"]
            or configuration.authority_epoch != row["authority_epoch"]
            or configuration.configuration_revision != row["config_revision"]
            or configuration.bindings != current_bindings
            or set(configuration.enabled_outputs) != enabled_outputs
            or plan.bindings != configuration.bindings
        ):
            continue
        layers = {layer.assignment_id: layer for layer in plan.layers}
        failures = {failure.assignment_id: failure for failure in report.failures}
        for assignment_id, failure in failures.items():
            layer = layers.get(assignment_id)
            frame = current_frames.get(layer.frame_id) if layer else None
            if (
                layer is None
                or frame is None
                or layer.end <= read_at
                or not configuration.authorizes(layer)
                or frame.output_id != layer.output_id
                or frame.generation != layer.binding_generation
            ):
                continue
            diagnostics.append(ReadinessDiagnostic(
                player_id=row["player_id"],
                authority_epoch=row["authority_epoch"],
                sequence=report.sequence,
                plan_id=plan.plan_id,
                revision=plan.revision,
                assignment_id=assignment_id,
                frame_id=layer.frame_id,
                output_id=layer.output_id,
                binding_generation=layer.binding_generation,
                failure_code=failure.code,
                received_at=received_at,
                observed_at=report.observed_at,
                layer_start=layer.start,
                layer_end=layer.end,
            ))
    ordered = sorted(diagnostics, key=lambda item: (item.player_id, item.output_id, item.assignment_id))
    return tuple(ordered[:MAX_READINESS_DIAGNOSTICS])
