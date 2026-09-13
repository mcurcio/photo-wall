"""Fail-closed contracts for the surviving hosted workflows.

The old appliance qualification workflow (appliance.yml: signed-disk build + VM
boot + old release-artifacts) was retired in p4-retire s5. What remains here is
the software-e2e diagnostics contract, which is independent of that pipeline.
"""

from pathlib import Path

WORKFLOWS = Path(__file__).parents[1] / ".github/workflows"


def test_software_e2e_uploads_sanitized_docker_failure_diagnostics():
    workflow = (WORKFLOWS / "software-e2e.yml").read_text()

    job_prefix = workflow.split("    steps:", 1)[0]
    assert "runner.temp" not in job_prefix
    assert workflow.count("PHOTO_WALL_DOCKER_DEBUG: '1'") == 3
    assert workflow.count(
        "PHOTO_WALL_DOCKER_DEBUG_LOG: ${{ runner.temp }}/photo-wall-software-e2e/docker-debug.log"
    ) == 3
    assert "name: Upload Docker failure diagnostics" in workflow
    assert "if: failure()" in workflow
    assert "photo-wall-software-e2e/docker-debug.log" in workflow
