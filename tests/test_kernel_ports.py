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
from central.kernel.job_types import FetchOsImage, FetchPackage
from central.kernel.ports import (
    Candidates,
    PackageRequest,
    PublishedRelease,
    ReleaseListing,
    Unknown,
)

SHA = "ab" * 32
LOCATOR = OriginLocator(url="https://example.test/a.deb", sha256=SHA, size=10)
REF = AssetReference(owner="v1.0.0", locator=LOCATOR, expected_size=10, expected_sha256=SHA)
KEY = AssetKey(AssetKind.PLAYER_DEB, SHA)


def test_candidates_invariants():
    one = FetchOsImage(tag="v1.0.0")
    two = FetchOsImage(tag="v1.1.0")
    assert Candidates((one, two), pinned=False).jobs == (one, two)
    assert Candidates((one,), pinned=True).pinned
    with pytest.raises(ValueError):
        Candidates((), pinned=False)
    with pytest.raises(ValueError):
        Candidates((one, two), pinned=True)
    with pytest.raises(ValueError):
        Candidates((one, FetchPackage(sha256=SHA)), pinned=False)
    with pytest.raises(ValueError):
        Candidates((one, FetchOsImage(tag="v1.0.0")), pinned=False)


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
    with pytest.raises(ValueError):
        PackageRequest(sha256="XYZ")
    with pytest.raises(ValueError):
        Unknown(reason="Not A Reason")


def test_published_release_invariants():
    release = PublishedRelease(tag="v1.0.0", is_prerelease=False, package=LOCATOR,
                               package_problem=None, os_image=LOCATOR)
    assert ReleaseListing((release,), etag='"e"', unchanged=False).releases == (release,)
    with pytest.raises(ValueError):
        PublishedRelease(tag="1.0.0", is_prerelease=False, package=LOCATOR, package_problem=None,
                         os_image=None)
    with pytest.raises(ValueError):
        PublishedRelease(tag="v1.0.0", is_prerelease=False, package=None, package_problem=None,
                         os_image=None)
    with pytest.raises(ValueError):
        PublishedRelease(tag="v1.0.0", is_prerelease=False, package=LOCATOR,
                         package_problem="no_deb", os_image=None)
    with pytest.raises(ValueError):
        PublishedRelease(tag="v1.0.0", is_prerelease=False,
                         package=OriginLocator(url="https://x.test/a", sha256=None, size=1),
                         package_problem=None, os_image=None)
    with pytest.raises(ValueError):
        ReleaseListing((release,), etag=None, unchanged=True)
