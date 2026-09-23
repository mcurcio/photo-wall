from __future__ import annotations

import pytest

from central.kernel.types import ReleaseVersion, release_version, require_reason, require_sha256


@pytest.mark.parametrize("tag, expected", [
    ("v1.2.3", ReleaseVersion(1, 2, 3, "")),
    ("v1.2.3-rc.1", ReleaseVersion(1, 2, 3, "rc.1")),
])
def test_release_version_accepts_strict_v_semver(tag, expected):
    assert release_version(tag) == expected


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "v1.2.3.4", "v1.2.3+b", "v" + "1" * 124 + ".2.3"])
def test_release_version_rejects(tag):
    with pytest.raises(ValueError, match="invalid_tag"):
        release_version(tag)


def test_release_version_length_limit_is_128():
    ok = "v1.2.3-" + "a" * 121
    assert len(ok) == 128
    assert release_version(ok).prerelease == "a" * 121
    with pytest.raises(ValueError, match="invalid_tag"):
        release_version(ok + "a")


def test_order_key_ranks_full_release_above_its_prerelease():
    full = release_version("v1.2.3").order_key()
    pre = release_version("v1.2.3-rc.1").order_key()
    assert full > pre
    assert release_version("v1.2.4-rc.1").order_key() > full


def test_require_sha256_and_reason():
    assert require_sha256("a" * 64) == "a" * 64
    for bad in ("A" * 64, "a" * 63, "g" * 64):
        with pytest.raises(ValueError, match="invalid_sha256"):
            require_sha256(bad)
    assert require_reason("not_published") == "not_published"
    for bad in ("", "Bad", "1x", "a-b", "a" * 65):
        with pytest.raises(ValueError, match="invalid_reason"):
            require_reason(bad)
