from __future__ import annotations

import pytest

from central.kernel.assets import (
    Asset,
    AssetKey,
    AssetKind,
    AssetReady,
    AssetReference,
    OriginLocator,
)
from central.kernel.job_types import FetchOsImage, FetchSealedEnvironment
from central.kernel.ports import (
    Candidates,
    PublishedRelease,
    ReleaseListing,
    UpstreamVersion,
)

SHA = "ab" * 32
LOCATOR = OriginLocator(url="https://example.test/a.deb", sha256=SHA, size=10)
REF = AssetReference(owner="v1.0.0", locator=LOCATOR, expected_size=10, expected_sha256=SHA)
KEY = AssetKey(AssetKind.SEALED_ENVIRONMENT, SHA)


def test_candidates_invariants():
    one = FetchOsImage(tarball_sha256="1" * 64)
    two = FetchOsImage(tarball_sha256="2" * 64)
    assert Candidates((one, two), pinned=False).jobs == (one, two)
    assert Candidates((one,), pinned=True).pinned
    with pytest.raises(ValueError):
        Candidates((), pinned=False)
    with pytest.raises(ValueError):
        Candidates((one, two), pinned=True)
    with pytest.raises(ValueError):
        Candidates((one, FetchSealedEnvironment(sha256=SHA)), pinned=False)
    with pytest.raises(ValueError):
        Candidates((one, FetchOsImage(tarball_sha256="1" * 64)), pinned=False)


def test_asset_requires_references_with_unique_owners():
    assert Asset(KEY, (REF,), produced=None, last_served_at=None).references == (REF,)
    with pytest.raises(ValueError):
        Asset(KEY, references=(), produced=None, last_served_at=None)
    with pytest.raises(ValueError):
        Asset(KEY, (REF, REF), produced=None, last_served_at=None)


@pytest.mark.parametrize("identity", ["", "a/b", "a\\b", "a\0b", ".", "..", "x" * 257])
def test_asset_key_refuses_unsafe_identities(identity):
    with pytest.raises(ValueError):
        AssetKey(AssetKind.OS_IMAGE, identity)


def test_value_invariants():
    with pytest.raises(ValueError):
        AssetReady(size=0, sha256=SHA)
    with pytest.raises(ValueError):
        AssetReady(size=1, sha256="XYZ")
    with pytest.raises(ValueError):
        OriginLocator(url="ftp://x", sha256=None, size=None)
    with pytest.raises(ValueError):
        OriginLocator(url="https://" + "x" * 2041, sha256=None, size=None)
    with pytest.raises(ValueError):
        AssetReference(owner="", locator=LOCATOR, expected_size=None, expected_sha256=None)


def test_published_release_invariants():
    release = PublishedRelease(tag="v1.0.0", is_prerelease=False,
                               node_problem="node_release_invalid",
                               node_version=UpstreamVersion(1.0, 1))
    assert ReleaseListing((release,), etag='"e"', unchanged=False).releases == (release,)
    with pytest.raises(ValueError):
        PublishedRelease(tag="1.0.0", is_prerelease=False, node_problem="node_release_invalid")
    with pytest.raises(ValueError, match="invalid_is_prerelease"):
        PublishedRelease(tag="v1.0.0", is_prerelease=None)
    with pytest.raises(ValueError):
        ReleaseListing((release,), etag=None, unchanged=True)


def test_upstream_version_is_ordered_by_time_then_asset_id():
    assert UpstreamVersion(1.0, 9) < UpstreamVersion(2.0, 1) < UpstreamVersion(2.0, 2)
    assert UpstreamVersion(2.0, 2) == UpstreamVersion(2.0, 2)


@pytest.mark.parametrize("changed_at,asset_id,code", [
    (float("inf"), 1, "invalid_changed_at"),
    (float("nan"), 1, "invalid_changed_at"),
    ("2026-09-01", 1, "invalid_changed_at"),
    (True, 1, "invalid_changed_at"),
    (1.0, 0, "invalid_asset_id"),
    (1.0, -1, "invalid_asset_id"),
    (1.0, 1.0, "invalid_asset_id"),
    (1.0, True, "invalid_asset_id"),
])
def test_upstream_version_refuses_a_non_finite_time_or_a_non_positive_id(changed_at, asset_id,
                                                                         code):
    with pytest.raises(ValueError, match=code):
        UpstreamVersion(changed_at, asset_id)
