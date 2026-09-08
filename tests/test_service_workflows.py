"""All hosted native media consumers must use the shared retained OS producer."""
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / '.github/workflows'


def test_every_hosted_media_build_supplies_the_retained_base():
    consumers = []
    for workflow in WORKFLOWS.glob('*.yml'):
        text = workflow.read_text()
        for step in re.split(r'^      - ', text, flags=re.MULTILINE):
            if re.search(r'target: media-(worker|test)\n', step):
                consumers.append(workflow.name)
                assert 'build-args: MEDIA_BASE_IMAGE=${{ needs.service-base.outputs.image }}' in step
                assert 'uses: ./.github/workflows/service-base.yml' in text
    assert sorted(consumers) == ['appliance.yml', 'checks.yml', 'checks.yml', 'software-e2e.yml']


def test_shared_producer_queues_all_callers_and_requires_published_output():
    producer = (WORKFLOWS / 'service-base.yml').read_text()
    assert 'group: media-system-${{ inputs.architecture }}' in producer
    assert 'queue: max' in producer
    assert 'cancel-in-progress: false' in producer
    assert 'args=(--require-published)' in producer
    assert '--compare "$COMPARE_REVISION"' in producer
    assert 'github.event.pull_request.head.repo.full_name == github.repository' in producer
    assert 'if [[ "$CAN_PUBLISH" == "true" ]]; then args+=(--publish); fi' in producer
    for name, architecture in [('checks.yml', 'amd64'), ('software-e2e.yml', 'arm64'),
                               ('appliance.yml', 'arm64')]:
        text = (WORKFLOWS / name).read_text()
        # The jobs have a shared owner, independent of the workflow or PR ref.
        assert f'architecture: {architecture}' in text


def test_existing_required_jobs_fail_if_shared_preparation_fails():
    for name, jobs in [('checks.yml', ['portable-and-postgres', 'linux-media']),
                       ('software-e2e.yml', ['two-players-three-outputs'])]:
        workflow = (WORKFLOWS / name).read_text()
        for job in jobs:
            body = re.split(r'^  [\w-]+:\n', workflow.split(f'  {job}:\n')[1],
                            maxsplit=1, flags=re.MULTILINE)[0]
            assert '    needs: service-base\n    if: always()' in body
            assert body.index('Require media OS preparation') < body.index('uses: actions/checkout')
            assert 'test "$PREPARATION" = success' in body
            assert 'test -n "$MEDIA_BASE_IMAGE"' in body


def test_only_appliance_workflow_assembles_pi_os_and_smoke_skips_media():
    for workflow in WORKFLOWS.glob('*.yml'):
        if workflow.name != 'appliance.yml':
            text = workflow.read_text()
            assert 'scripts.os_base' not in text
            assert 'scripts.build_ci_image' not in text
            assert 'scripts.ci_images builder' not in text
    appliance = (WORKFLOWS / 'appliance.yml').read_text()
    assert "inputs.scope == 'full'" in appliance
    assert 'needs: [select-definition, service-base]' in appliance
    assert 'if [[ "$APPLIANCE_SCOPE" == full ]]; then test "$MEDIA_SELECTION" = success; fi' in appliance


def test_browser_dependencies_are_published_and_match_locked_playwright():
    workflow = (WORKFLOWS / 'checks.yml').read_text()
    version = re.search(r'mcr.microsoft.com/playwright/python:v([\d.]+)-noble@sha256:[a-f0-9]{64}',
                        workflow)[1]
    assert f'name = "playwright"\nversion = "{version}"' in (ROOT / 'uv.lock').read_text()
    assert 'playwright install' not in workflow
    assert '--with-deps' not in workflow
    assert 'UV_PROJECT_ENVIRONMENT=/tmp/photo-wall-browser-venv' in workflow
    assert 'uv sync --frozen --no-install-project' in workflow
    assert '--network host' in workflow  # Existing disposable database stays reachable.
    assert 'scripts/test_local.py -q' in workflow
