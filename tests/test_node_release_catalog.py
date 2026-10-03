"""Real origin HTTP codec/hash verification and durable explicit V2 publication."""
import asyncio
import json
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from test_fleet_attempts import BASE_ABI, BOOT_ID, SERIAL, _seed
from test_node_boot import environment

from central.fleet.node_release_catalog import NodeReleaseCatalog
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.infra.catalog_records import PgReleaseRecords
from central.infra.transactions import PgTransaction
from central.kernel.handling import OriginUnavailable
from central.origins.github import GitHubReleaseOrigin
from contracts.node_boot import NodeBaseRefV2, NodeBootRequestV2
from contracts.node_release import (
    NODE_RELEASE_MANIFEST,
    NodeReleaseAssetV2,
    NodeReleaseV2,
    encode_node_release,
    parse_node_release,
)


def publication():
    bodies = {role: (role+'-exact-bytes').encode() for role in ('base','boot','node-base-deb',
        'node-display-deb','manager-primary-deb','manager-primary','build-provenance','app','app-deb')}
    artifacts = tuple(NodeReleaseAssetV2(role, role+'.bin', sha256(data).hexdigest(), len(data))
                      for role, data in bodies.items())
    refs = {a.role: a for a in artifacts}
    app = replace(environment(), environment_sha256=refs['app'].sha256,size_bytes=refs['app'].size_bytes,
                  deb_sha256=refs['app-deb'].sha256)
    manager = replace(environment('2','photo-wall-node-manager'), environment_sha256=refs['manager-primary'].sha256,
                      size_bytes=refs['manager-primary'].size_bytes,deb_sha256=refs['manager-primary-deb'].sha256)
    release = NodeReleaseV2('a'*40,NodeBaseRefV2('v9.0.0',refs['base'].sha256,'7'*64,100,
        BASE_ABI,'graphics-v1','plugin-v1'),app,manager,None,artifacts)
    urls = {a.filename:bodies[a.role] for a in artifacts}
    urls[NODE_RELEASE_MANIFEST] = encode_node_release(release)
    entry = {'tag_name':release.base.tag,'draft':False,'prerelease':False,'assets':[
        {'name':name,'browser_download_url':'https://assets.test/'+name,'id':index+1,
         'updated_at':'2026-09-30T00:00:00Z'} for index,name in enumerate(urls)]}
    def handle(request):
        if request.url.path == '/repos/test/repo/releases':
            return httpx.Response(200,json=[entry])
        data = urls.get(request.url.path.lstrip('/'))
        return httpx.Response(404 if data is None else 200,content=data or b'')
    origin = GitHubReleaseOrigin('test/repo',transport=httpx.MockTransport(handle))
    return origin, release, urls


def discover(registry, origin):
    result = asyncio.run(origin.list_releases(etag=None))
    with registry.db.transaction() as conn:
        PgReleaseRecords().claim(PgTransaction(conn), result.releases[0], now=registry.clock.utc())
    return result.releases[0]


def test_discovery_is_observation_then_verified_explicit_publication_and_selection(registry):
    from central.fleet.node_boot import NodeBootService
    _seed(registry)
    origin, release, _ = publication()
    observed = discover(registry,origin)
    discover(registry,origin)
    assert observed.os_image is None  # V2 bytes did not become legacy image metadata.
    sessions = NodeSessions(registry.db,registry.clock,NodeControlConfig('node-test'))
    catalog = NodeReleaseCatalog(sessions,origin)
    identity = sha256(encode_node_release(release)).hexdigest()
    assert catalog.list()['releases'][0]['verified_at'] is None
    deployment_id = uuid4()
    assert asyncio.run(catalog.publish(identity,deployment_id,True,'operator:release-test'))['published']
    assert asyncio.run(catalog.publish(identity,deployment_id,True,'operator:release-test'))['duplicate']
    boots = NodeBootService(sessions)
    boots.select(deployment_id,0)
    offer = boots.offer(NodeBootRequestV2(SERIAL,BOOT_ID,'b'*64))
    assert offer.base == release.base and offer.manager_primary == release.manager_primary
    assert offer.app_environment == release.app_environment
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM node_release_catalog').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) n FROM node_release_verifications').fetchone()['n'] == 1


def test_corrupt_declared_artifact_cannot_publish(registry):
    _seed(registry)
    origin, release, urls = publication()
    discover(registry,origin)
    urls['node-display-deb.bin'] = b'wrong-bytes'
    service = NodeReleaseCatalog(NodeSessions(registry.db,registry.clock,NodeControlConfig('node-test')),origin)
    with pytest.raises(OriginUnavailable, match="download_corrupt"):
        asyncio.run(service.publish(sha256(encode_node_release(release)).hexdigest(),uuid4(),True,'operator:test'))
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM node_release_verifications').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) n FROM node_deployments').fetchone()['n'] == 0


def test_conflicting_same_release_revision_and_absent_asset_refuse(registry):
    origin, release, urls = publication()
    discover(registry,origin)
    changed = replace(release,base=replace(release.base,squashfs_sha256='6'*64))
    urls[NODE_RELEASE_MANIFEST] = encode_node_release(changed)
    with pytest.raises(ValueError,match='identity_conflict'):
        discover(registry,origin)
    invalid = json.loads(encode_node_release(release))
    invalid['artifacts'] = [a for a in invalid['artifacts'] if a['role'] != 'node-display-deb']
    with pytest.raises(ValueError,match='artifacts_invalid'):
        parse_node_release(json.dumps(invalid).encode())
    with pytest.raises(ValueError,match='environment_asset_mismatch'):
        replace(release,manager_primary=replace(release.manager_primary,architecture='amd64'))


def test_actual_producer_manifest_roundtrips_through_origin_catalog(registry,tmp_path):
    from test_node_release_artifacts import inputs
    _seed(registry)
    _, _, output = inputs(tmp_path)
    raw = (output/NODE_RELEASE_MANIFEST).read_bytes()
    manifest = parse_node_release(raw)
    entry = {'tag_name':manifest.base.tag,'draft':False,'prerelease':False,'assets':[
        {'name':path.name,'browser_download_url':'https://assets.test/'+path.name,'id':index+1,
         'updated_at':'2026-09-30T00:00:00Z'} for index,path in enumerate(output.iterdir())]}
    def handle(request):
        if request.url.path=='/repos/test/repo/releases':
            return httpx.Response(200,json=[entry])
        path=output/request.url.path.lstrip('/')
        return httpx.Response(200,content=path.read_bytes()) if path.is_file() else httpx.Response(404)
    origin=GitHubReleaseOrigin('test/repo',transport=httpx.MockTransport(handle))
    observed=discover(registry,origin)
    assert observed.node_publication.manifest==raw
    assert observed.os_image.sha256!=manifest.base.content_key
    catalog=NodeReleaseCatalog(NodeSessions(registry.db,registry.clock,NodeControlConfig('node-test')),origin)
    assert asyncio.run(catalog.publish(sha256(raw).hexdigest(),uuid4(),False,'operator:producer-output'))['published']


# --- The release read (console DDD Part E G1, R18).


class _Recording:
    """The registry's Database, recording every statement each transaction runs, in order."""

    def __init__(self, db):
        self.db, self.statements = db, []

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def recorded():
            with self.db.transaction() as conn:
                outer = self

                class Conn:
                    def execute(self, sql, *args):
                        outer.statements.append(sql)
                        return conn.execute(sql, *args)
                yield Conn()
        return recorded()


def _read(registry, db=None):
    sessions = NodeSessions(db or registry.db, registry.clock, NodeControlConfig('node-test'))
    return NodeReleaseCatalog(sessions, origin=object()).list()


def test_release_read_is_one_repeatable_read_read_only_snapshot(registry):
    from test_node_boot import cold_setup
    cold_setup(registry)
    recording = _Recording(registry.db)
    read = _read(registry, recording)
    # One transaction: the snapshot is fixed before its first data query, and it writes nothing.
    assert recording.statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in recording.statements[1:])
    assert len(recording.statements) == 4
    assert read['read_at'] == registry.clock.utc()


def test_with_no_policy_row_the_selection_is_revision_zero_and_nothing_is_listed(registry):
    _seed(registry)
    read = _read(registry)
    assert read['selection'] == {'revision': 0, 'deployment_id': None, 'changed_at': None}
    assert read['deployments'] == [] and read['releases'] == []


def test_the_selected_deployment_is_listed_even_when_older_than_the_newest_fifty(registry):
    from test_node_boot import cold_setup

    from central.fleet.node_boot import encode_node_deployment
    _, _, selected = cold_setup(registry)
    changed_at = registry.clock.utc()
    older = replace(selected, deployment_id=uuid4())
    newer = [replace(selected, deployment_id=uuid4()) for _ in range(50)]
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO node_deployments VALUES(%s,%s,%s)",
                     (older.deployment_id, encode_node_deployment(older), changed_at - 10))
        for offset, deployment in enumerate(newer, start=1):
            conn.execute("INSERT INTO node_deployments VALUES(%s,%s,%s)",
                         (deployment.deployment_id, encode_node_deployment(deployment), changed_at + offset))
    read = _read(registry)
    listed = [row['deployment_id'] for row in read['deployments']]
    # The 50 newest, newest first, then the selected one; the older unselected one is not listed.
    assert listed == [str(d.deployment_id) for d in reversed(newer)] + [str(selected.deployment_id)]
    assert str(older.deployment_id) not in listed
    assert read['selection'] == {'revision': 1, 'deployment_id': str(selected.deployment_id),
                                 'changed_at': changed_at}
    row = read['deployments'][-1]
    assert row == {'deployment_id': str(selected.deployment_id), 'published_at': changed_at,
                   'base_tag': selected.base.tag,
                   'app_environment_sha256': selected.app_environment.environment_sha256}


def test_releases_and_deployments_serve_their_base_tag_and_app(registry):
    from test_node_boot import cold_setup
    _, _, deployment = cold_setup(registry, app=False)
    read = _read(registry)
    [release] = read['releases']
    assert release['base_tag'] == deployment.base.tag
    assert release['app_environment_sha256'] is None
    assert release['verified_at'] == 1000
    assert release['download_bytes'] == 256 + 4 * 32 + deployment.manager_primary.size_bytes + 16
    assert read['deployments'][0]['app_environment_sha256'] is None


def test_a_release_with_an_app_serves_its_app_environment(registry):
    _seed(registry)
    origin, release, _ = publication()
    discover(registry, origin)
    [row] = _read(registry)['releases']
    assert row['manifest_sha256'] == sha256(encode_node_release(release)).hexdigest()
    assert (row['tag'], row['base_tag']) == ('v9.0.0', 'v9.0.0')
    assert row['app_environment_sha256'] == release.app_environment.environment_sha256
    assert row['download_bytes'] == sum(asset.size_bytes for asset in release.artifacts)
    assert row['verified_at'] is None
