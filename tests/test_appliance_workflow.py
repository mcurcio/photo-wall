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
    # Each fixture-driving job records its Docker diagnostics privately and uploads them only
    # when it fails: the wall scenario (fixture, scenario, cleanup) and the adapter checks.
    scenario, adapter = workflow.split("\n  immich-adapter:\n")
    for job, steps, log in [(scenario, 3, "photo-wall-software-e2e"),
                            (adapter, 1, "photo-wall-immich-adapter")]:
        assert job.count("PHOTO_WALL_DOCKER_DEBUG: '1'") == steps
        assert job.count(
            f"PHOTO_WALL_DOCKER_DEBUG_LOG: ${{{{ runner.temp }}}}/{log}/docker-debug.log") == steps
        upload = job.split("name: Upload Docker failure diagnostics", 1)[1]
        assert upload.startswith("\n        if: failure()")
        assert f"{log}/docker-debug.log" in upload
