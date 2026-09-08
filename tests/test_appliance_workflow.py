"""Fail-closed contracts for the hosted appliance qualification workflow."""

from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/appliance.yml"


def test_routine_appliance_runs_are_smoke_and_manual_full_binds_native_media():
    workflow = WORKFLOW.read_text()

    assert "default: smoke" in workflow
    assert "APPLIANCE_SCOPE: ${{ github.event_name == 'workflow_dispatch' && inputs.scope || 'smoke' }}" in workflow
    assert "if: env.APPLIANCE_SCOPE == 'full'" in workflow
    assert "target: media-worker" in workflow
    assert workflow.count("WORKER_IMAGE: ${{ steps.worker.outputs.imageid }}") == 2
    assert workflow.count('worker_args=(--worker-image "$WORKER_IMAGE")') == 2
    assert '--scope "$APPLIANCE_SCOPE"' in workflow
    assert "--scope smoke" not in workflow


def test_software_e2e_uploads_sanitized_docker_failure_diagnostics():
    workflow = (WORKFLOW.parent / "software-e2e.yml").read_text()

    job_prefix = workflow.split("    steps:", 1)[0]
    assert "runner.temp" not in job_prefix
    assert workflow.count("PHOTO_WALL_DOCKER_DEBUG: '1'") == 3
    assert workflow.count(
        "PHOTO_WALL_DOCKER_DEBUG_LOG: ${{ runner.temp }}/photo-wall-software-e2e/docker-debug.log"
    ) == 3
    assert "name: Upload Docker failure diagnostics" in workflow
    assert "if: failure()" in workflow
    assert "photo-wall-software-e2e/docker-debug.log" in workflow


def test_upload_compression_uses_available_cpus_after_exact_artifact_acceptance():
    workflow = WORKFLOW.read_text()
    acceptance = workflow.index("- name: Boot the exact artifact and launch the production Player")
    compression = workflow.index("- name: Compress the disk for artifact upload")
    assert acceptance < compression
    assert 'xz -T0 -6 "$disk"' in workflow[compression:]
    assert 'sha256sum "$(basename "$disk").xz" ci-image.json artifact.json > UPLOAD-SHA256SUMS' in workflow
    assert "compression-level: 0" in workflow
