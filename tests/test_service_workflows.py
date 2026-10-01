"""All hosted native media consumers must use the shared retained OS producer."""
import re
from pathlib import Path

from scripts.demo_wall import FAULT_SEGMENTS, SCENARIOS

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / '.github/workflows'
ACTIONS = ROOT / '.github/actions'
E2E_SETUP = 'uses: ./.github/actions/software-e2e-setup\n'


def test_every_hosted_media_build_supplies_the_retained_base():
    consumers = []
    for workflow in WORKFLOWS.glob('*.yml'):
        text = workflow.read_text()
        for step in re.split(r'^      - ', text, flags=re.MULTILINE):
            if re.search(r'target: media-(worker|test)\n', step):
                consumers.append(workflow.name)
                assert 'build-args: MEDIA_BASE_IMAGE=${{ needs.service-base.outputs.image }}' in step
                assert 'uses: ./.github/workflows/service-base.yml' in text
            if E2E_SETUP in step:  # builds the media worker from the base handed to it
                consumers.append(workflow.name)
                assert 'media-base-image: ${{ needs.service-base.outputs.image }}' in step
                assert 'uses: ./.github/workflows/service-base.yml' in text
    for action in ACTIONS.glob('*/action.yml'):
        for step in re.split(r'^    - ', action.read_text(), flags=re.MULTILINE):
            if re.search(r'target: media-(worker|test)\n', step):
                assert action.parent.name == 'software-e2e-setup'
                assert 'build-args: MEDIA_BASE_IMAGE=${{ inputs.media-base-image }}' in step
    assert sorted(consumers) == ['checks.yml', 'checks.yml', 'pipeline.yml', 'software-e2e.yml',
                                 'software-e2e.yml']


def test_shared_producer_queues_all_callers_and_requires_published_output():
    producer = (WORKFLOWS / 'service-base.yml').read_text()
    assert 'group: media-system-${{ inputs.architecture }}' in producer
    assert 'queue: max' in producer
    assert 'cancel-in-progress: false' in producer
    assert 'args=(--require-published)' in producer
    assert '--compare "$COMPARE_REVISION"' in producer
    assert 'github.event.pull_request.head.repo.full_name == github.repository' in producer
    assert 'if [[ "$CAN_PUBLISH" == "true" ]]; then args+=(--publish); fi' in producer
    for name, architecture in [('checks.yml', 'amd64'), ('software-e2e.yml', 'arm64')]:
        text = (WORKFLOWS / name).read_text()
        # The jobs have a shared owner, independent of the workflow or PR ref.
        assert f'architecture: {architecture}' in text


def test_existing_required_jobs_fail_if_shared_preparation_fails():
    for name, jobs in [('checks.yml', ['image-smoke', 'linux-media']),
                       ('software-e2e.yml', ['two-players-three-outputs', 'immich-adapter'])]:
        workflow = (WORKFLOWS / name).read_text()
        for job in jobs:
            body = re.split(r'^  [\w-]+:\n', workflow.split(f'  {job}:\n')[1],
                            maxsplit=1, flags=re.MULTILINE)[0]
            assert '    needs: service-base\n    if: always()' in body
            assert body.index('Require media OS preparation') < body.index('uses: actions/checkout')
            assert 'test "$PREPARATION" = success' in body
            assert 'test -n "$MEDIA_BASE_IMAGE"' in body


def test_no_workflow_references_the_retired_ci_os_image_pipeline():
    """The old custom-image / signed-disk CI pipeline (scripts.os_base,
    scripts.build_ci_image, scripts.ci_images and the appliance.yml that drove
    them) is fully retired in p4-retire s5. No surviving workflow may reference
    any of its modules."""
    for workflow in WORKFLOWS.glob('*.yml'):
        text = workflow.read_text()
        assert 'scripts.os_base' not in text
        assert 'scripts.build_ci_image' not in text
        assert 'scripts.ci_images' not in text
    assert not (WORKFLOWS / 'appliance.yml').exists()


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


def _job(workflow, job):
    return re.split(r'^  [\w-]+:\n', workflow.split(f'\n  {job}:\n')[1], maxsplit=1,
                    flags=re.MULTILINE)[0]


def test_the_tier_jobs_partition_the_suite_and_fail_closed():
    """unit, db and browser run every test exactly once (tests/conftest.py derives the tiers),
    each needing no other job, and every job refuses a missing database."""
    workflow = (WORKFLOWS / 'checks.yml').read_text()
    assert "\nenv:\n  COMPOSE_PROJECT_NAME: photo-wall-ci\n  PHOTO_WALL_TEST_REQUIRE_DATABASE: '1'\n" \
        in workflow
    selections = {job: _job(workflow, job) for job in ('unit', 'db', 'browser')}
    assert '-m "not db and not browser" -n 4 --dist worksteal' in selections['unit']
    assert '-m db -n 4 --dist loadgroup' in selections['db']
    assert ' tests/browser -n 4 --browser chromium' in selections['browser']
    assert '["status"] != "passed")' in selections['browser']
    for job, body in selections.items():
        assert 'needs:' not in body, job
        assert '--durations=25' in body, job
        assert re.search(r'timeout-minutes: \d+\n', body), job
    for job in ('db', 'browser'):
        assert 'compose.test-database.yml up -d --wait' in selections[job]
    assert 'PHOTO_WALL_RELEASE_TOKEN' in selections['db']
    for job in ('unit', 'db'):  # both tiers hold tests of the published Player wire
        assert 'published_player_wire.py prepare' in selections[job]
        assert 'PHOTO_WALL_PUBLISHED_PLAYER_WIRE_DIR' in selections[job]
    assert '--env PHOTO_WALL_TEST_REQUIRE_DATABASE --env CI' in selections['browser']


def test_the_wall_scenario_keeps_every_immich_adapter_check_in_a_parallel_job():
    """The scenario jobs start a set-up fixture only; the full adapter run is its own job."""
    workflow = (WORKFLOWS / 'software-e2e.yml').read_text()
    scenario = _job(workflow, 'two-players-three-outputs')
    adapter = _job(workflow, 'immich-adapter')
    assert '--keep --setup-only' in scenario
    assert 'scripts.immich_fixture run' in adapter
    assert '--setup-only' not in adapter and '--keep' not in adapter
    setup = (ACTIONS / 'software-e2e-setup/action.yml').read_text()
    assert setup.index('Prefetch the Immich fixture images') < setup.index(
        'Build or restore the central image')


def test_the_wall_scenario_jobs_run_every_fault_segment_exactly_once():
    """The full scenario is its fault segments; the parallel scenario jobs partition them, and
    every job shares one setup definition."""
    workflow = (WORKFLOWS / 'software-e2e.yml').read_text()
    scenario = _job(workflow, 'two-players-three-outputs')
    parts = re.search(r'\n        part: \[(.+)\]\n', scenario)[1].split(', ')
    assert 'PART: ${{ matrix.part }}' in scenario and '--scenario "$PART"' in scenario
    covered = [segment for part in parts for segment in SCENARIOS[part]]
    assert sorted(covered) == sorted(set(covered)) == sorted(SCENARIOS['full'])
    assert SCENARIOS['full'] == tuple(FAULT_SEGMENTS)
    for job in ('two-players-three-outputs', 'immich-adapter'):
        body = _job(workflow, job)
        assert body.count(E2E_SETUP) == 1, job
        for repeated in ('uv sync', 'docker login', 'setup-buildx-action', 'service-image'):
            assert repeated not in body, (job, repeated)


def test_the_node_pid1_job_requires_every_real_systemd_scenario_in_parallel():
    """One matrix leg per scenario of tests/test_node_pid1.py, each required, never skipped,
    on native arm64 from the components builder base-image.yml runs."""
    from test_node_pid1 import FIXTURE_VARIABLE, REQUIRE_VARIABLE, SCENARIOS

    workflow = (WORKFLOWS / 'node-pid1.yml').read_text()
    scenario = _job(workflow, 'scenario')
    legs = re.search(r'\n        scenario: \[(.+)\]\n', scenario)[1].split(', ')
    assert sorted(legs) == sorted(SCENARIOS) and len(legs) == len(set(legs))
    assert 'fail-fast: false' in scenario
    assert 'runs-on: ubuntu-24.04-arm\n' in scenario
    assert re.search(r'timeout-minutes: \d+\n', scenario)
    assert f"\n  {REQUIRE_VARIABLE}: '1'\n" in workflow
    assert "\n  PHOTO_WALL_TEST_REQUIRE_DATABASE: '1'\n" in workflow
    assert 'compose.test-database.yml up -d --wait' in scenario
    for builder in ('build_node_components', 'build_node_pid1_fixture'):
        assert f'.venv/bin/python -m scripts.{builder}' in scenario, builder
    assert '.venv/bin/python -m scripts.build_node_components' in (
        WORKFLOWS / 'base-image.yml').read_text()
    assert f'{FIXTURE_VARIABLE}: ' in scenario
    assert '-m node_pid1 -k "$SCENARIO"' in scenario and 'SCENARIO: ${{ matrix.scenario }}' in scenario
    pipeline = (WORKFLOWS / 'pipeline.yml').read_text()
    job = _job(pipeline, 'node-pid1')
    assert "if: contains(fromJSON(needs.plan.outputs.jobs), 'node-pid1')" in job
    assert 'uses: ./.github/workflows/node-pid1.yml' in job
    assert 'revision: ${{ needs.plan.outputs.revision }}' in job
