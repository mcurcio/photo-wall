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
    assert '--env PHOTO_WALL_TEST_REQUIRE_DATABASE --env CI' in selections['browser']


def test_the_console_catalog_walk_is_its_own_leg_in_the_browser_image_the_script_pins():
    """The walker leaves the operator browser leg for its own, needing no database, in the same
    pinned image the baseline script reads, so baselines are made where they are checked."""
    workflow = (WORKFLOWS / 'checks.yml').read_text()
    walk, browser = _job(workflow, 'console-catalog'), _job(workflow, 'browser')
    walker = 'tests/browser/test_console_catalog_browser.py'
    assert f'--ignore {walker}' in browser and walk.count(walker) == 1
    assert f'{walker} -n 4 \\\n                --browser chromium' in walk
    assert 'run build-storybook' in walk and 'needs:' not in walk
    assert 'compose.test-database.yml' not in walk and 'PHOTO_WALL_CATALOG_UPDATE' not in walk
    pinned = re.findall(r'mcr.microsoft.com/playwright/python:\S+', walk + browser)
    assert len(set(pinned)) == 1 and 'catalog_baselines' in workflow
    assert re.search(r'timeout-minutes: [1-8]\n', walk)


def test_the_wall_scenario_keeps_every_immich_adapter_check_in_a_parallel_job():
    """The scenario jobs start a set-up fixture only; the full adapter run is its own job."""
    workflow = (WORKFLOWS / 'software-e2e.yml').read_text()
    scenario = _job(workflow, 'two-players-three-outputs')
    adapter = _job(workflow, 'immich-adapter')
    assert '--keep --setup-only' in scenario
    assert 'scripts.immich_fixture run' in adapter
    assert '--setup-only' not in adapter and '--keep' not in adapter
    setup = (ACTIONS / 'software-e2e-setup/action.yml').read_text()
    # One signed-in, retrying pull of the fixture images per job, overlapping the image builds.
    prefetch = setup.index('Prefetch the Immich fixture images')
    assert setup.index('uv sync --frozen') < setup.index('docker login ghcr.io') < prefetch
    assert prefetch < setup.index('Build or restore the central image')
    assert '.venv/bin/python -m scripts.immich_fixture prefetch' in setup
    assert not re.search(r'docker (compose .*)?pull', setup)


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
    on native arm64, every leg booting the one component set and fixture node-components.yml
    built for the run -- the set base-image.yml bakes."""
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
    components = (WORKFLOWS / 'node-components.yml').read_text()
    builds, fixture = _job(components, 'build'), _job(components, 'pid1-fixture')
    assert 'runs-on: ubuntu-24.04-arm\n' in builds and 'runs-on: ubuntu-24.04-arm\n' in fixture
    for builder, job in (('debian-packaging/build-root.sh', builds),
                         ('.venv/bin/python -m scripts.node_release_writer write', builds),
                         ('tests/node_pid1_fixture/build.sh', fixture)):
        assert builder in job, builder
        assert builder not in scenario, builder
    components = 'name: photo-wall-node-components-${{ env.REVISION }}'
    assert components in scenario and components in (WORKFLOWS / 'base-image.yml').read_text()
    assert 'name: photo-wall-node-pid1-fixture-${{ env.REVISION }}' in scenario
    assert 'docker load -i "$RUNNER_TEMP/node-pid1-fixture/image.tar"' in scenario
    assert f'{FIXTURE_VARIABLE}: ' in scenario
    assert '-m node_pid1 -k "$SCENARIO"' in scenario and 'SCENARIO: ${{ matrix.scenario }}' in scenario
    pipeline = (WORKFLOWS / 'pipeline.yml').read_text()
    job = _job(pipeline, 'node-pid1')
    assert "if: contains(fromJSON(needs.plan.outputs.jobs), 'node-pid1')" in job
    assert 'uses: ./.github/workflows/node-pid1.yml' in job
    assert 'revision: ${{ needs.plan.outputs.revision }}' in job
    build = _job(pipeline, 'node-components')
    assert 'uses: ./.github/workflows/node-components.yml' in build
    assert 'revision: ${{ needs.plan.outputs.revision }}' in build
    assert "pid1-fixture: ${{ contains(fromJSON(needs.plan.outputs.jobs), 'node-pid1') }}" in build


def test_a_pull_request_uploads_no_release_artifact_but_keeps_failure_diagnostics():
    workflow = (WORKFLOWS / 'base-image.yml').read_text()
    for step in re.split(r'^      - ', workflow, flags=re.MULTILINE):
        if 'uses: actions/upload-artifact@' not in step:
            continue
        condition = re.search(r'^        if: (.+)$', step, flags=re.MULTILINE)
        assert condition, step.splitlines()[0]
        assert condition[1] in ("github.event_name != 'pull_request'", 'failure()',
                                "failure() || github.event_name != 'pull_request'"), step
    assert 'name: photo-wall-node-components-' not in workflow.split('upload-artifact@')[-1]


def test_linux_media_runs_in_parallel_and_writes_its_cache_only_from_main():
    linux_media = _job((WORKFLOWS / 'checks.yml').read_text(), 'linux-media')
    assert "--env 'PYTEST_ADDOPTS=-n auto' photo-wall-media-test:ci" in linux_media
    assert ("cache-write: ${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
            in linux_media)
    action = (ACTIONS / 'service-image/action.yml').read_text()
    assert 'uses: ./.github/actions/buildkit-cache' in action
    assert 'write: ${{ inputs.cache-write }}' in action


# --- Docker Hub pulls go through the mirror (docs/module-appliance-ci.md, "Docker Hub pulls") ---

MIRROR_ACTION = 'docker-hub-mirror'
BUILDKIT_MIRROR = ('buildkitd-config-inline: '
                   '${{ steps.docker-hub-mirror.outputs.buildkitd-config }}\n')
BUILDKIT_IMAGE = 'driver-opts: image=${{ steps.docker-hub-mirror.outputs.buildkit-image }}\n'
# A step that reaches Docker: the CLI, Compose, a builder, or an action that builds an image.
_DOCKER_STEP = re.compile(r'\bdocker\b|\bcompose\b|buildx|build-push-action'
                          r'|uses: \./\.github/actions/(service-image|software-e2e-setup)\n')
# Jobs that pull nothing from Docker Hub, so they need no mirror; every other job with steps
# runs it before its first Docker step, so a new job is red until it does or is listed here.
WITHOUT_DOCKER_HUB = {
    'checks.yml': {'static', 'unit', 'node-bus',
                   'console-catalog'},             # the Playwright image, from mcr.microsoft.com
    'pipeline.yml': {'plan', 'tested', 'gate',
                     'seal'},                      # reads and tags ghcr.io images only
    'base-image.yml': {'build-base-image'},        # runs only the squashfs it built and imported
}


def _steps(body, indent):
    """The steps of a job or composite action, comment lines dropped (a comment heads the next
    step, so it would be counted with the one before)."""
    text = '\n'.join(line for line in body.splitlines() if not line.lstrip().startswith('#'))
    return re.split(rf'^{indent}- ', text + '\n', flags=re.MULTILINE)[1:]


def _workflow_jobs():
    for workflow in sorted(WORKFLOWS.glob('*.yml')):
        parts = re.split(r'^  ([\w-]+):\n', workflow.read_text().split('\njobs:\n', 1)[1],
                         flags=re.MULTILINE)
        for job, body in zip(parts[1::2], parts[2::2]):
            if '\n    steps:\n' in body:  # not a reusable-workflow call
                yield workflow.name, job, body


def _first_docker_contact(steps, routed):
    """'mirror' when a step routing through the mirror comes before any Docker step, 'docker'
    when a Docker step comes first, None when there is neither."""
    for step in steps:
        if any(f'uses: ./.github/actions/{action}\n' in step for action in routed):
            return 'mirror'
        if _DOCKER_STEP.search(step):
            return 'docker'
    return None


def _routed_actions():
    """The mirror action, and each composite action that runs it before any Docker step."""
    routed = {MIRROR_ACTION}
    for action in ACTIONS.glob('*/action.yml'):
        if _first_docker_contact(_steps(action.read_text(), '    '), {MIRROR_ACTION}) == 'mirror':
            routed.add(action.parent.name)
    return routed


def test_every_job_that_reaches_docker_hub_runs_the_mirror_before_docker():
    routed = _routed_actions()
    assert 'software-e2e-setup' in routed
    listed = {(name, job) for name, jobs in WITHOUT_DOCKER_HUB.items() for job in jobs}
    seen = set()
    for name, job, body in _workflow_jobs():
        seen.add((name, job))
        contact = _first_docker_contact(_steps(body, '      '), routed)
        if (name, job) in listed:
            assert contact != 'mirror', f'{name}: {job} runs the mirror; drop it from the list'
        else:
            assert contact == 'mirror', (
                f'{name}: {job} must run ./.github/actions/{MIRROR_ACTION} before its first '
                'Docker step, or be listed as pulling nothing from Docker Hub')
    assert listed <= seen, listed - seen


def test_every_buildx_builder_carries_the_mirror():
    """A docker-container builder ignores the daemon's mirrors: each gets the action's BuildKit
    configuration and BuildKit's own image through the mirror (setup-buildx-action otherwise
    pulls moby/buildkit from Docker Hub), from a mirror step that ran before it."""
    routed = _routed_actions()
    sources = [(path, '      ') for path in WORKFLOWS.glob('*.yml')]
    sources += [(path, '    ') for path in ACTIONS.glob('*/action.yml')]
    builders = 0
    for path, indent in sources:
        for step in _steps(path.read_text(), indent):
            if 'uses: docker/setup-buildx-action@' in step:
                builders += 1
                assert BUILDKIT_MIRROR in step, path
                assert BUILDKIT_IMAGE in step, path
    assert builders >= 6  # not vacuous: the six builders today
    for action in ACTIONS.glob('*/action.yml'):
        if 'docker/setup-buildx-action@' in action.read_text():
            assert action.parent.name in routed, action


def test_no_job_level_image_is_pulled_from_docker_hub():
    """A job's `container:` or `services:` image is pulled before any step runs, so before the
    mirror: it must name another registry (mirror.gcr.io/library/<name>@<digest> for a Docker Hub
    image)."""
    for workflow in WORKFLOWS.glob('*.yml'):
        for reference in re.findall(r'^\s+(?:image|container): *([^\s{][^\s]*)$',
                                    workflow.read_text(), flags=re.MULTILINE):
            host = reference.split('/', 1)[0]
            assert '/' in reference and '.' in host and host != 'docker.io', (workflow, reference)
