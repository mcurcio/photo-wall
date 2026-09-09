import json

import pytest

from contracts.release import MAX_ROOTFS_BYTES, Release, configuration_digest


def release(**changes):
    values = dict(revision="a" * 40, boot_abi="b" * 64, configuration_sha256="c" * 64,
                  rootfs_sha256="d" * 64, rootfs_size=4096)
    return Release(**{**values, **changes})


def test_release_is_canonical_bounded_and_names_content_not_arbitrary_paths():
    value = release()
    assert Release.decode(value.encode()) == value
    assert value.rootfs_name == "rootfs-" + "d" * 64 + ".squashfs"
    assert value.release_id != release(rootfs_size=4097).release_id
    value.require_compatible("b" * 64, "c" * 64)
    with pytest.raises(ValueError, match="release_incompatible"):
        value.require_compatible("e" * 64, "c" * 64)
    with pytest.raises(ValueError, match="release_incompatible"):
        value.require_compatible("b" * 64, "e" * 64)


@pytest.mark.parametrize("changes", [
    {"schema": True}, {"schema": 2}, {"revision": "main"}, {"revision": "a" * 64},
    {"boot_abi": "../../kernel"}, {"configuration_sha256": None},
    {"rootfs_sha256": "D" * 64}, {"rootfs_size": 0}, {"rootfs_size": -1},
    {"rootfs_size": 1.5}, {"rootfs_size": True}, {"rootfs_size": MAX_ROOTFS_BYTES + 1},
])
def test_invalid_or_unbounded_release_fields_are_rejected(changes):
    with pytest.raises(ValueError, match="invalid_release"):
        release(**changes)


@pytest.mark.parametrize("payload", [b"", b"[]", b"x" * 8193, b"\xff", b"{}", b"null"])
def test_release_parser_rejects_malformed_payload(payload):
    with pytest.raises(ValueError, match="invalid_release"):
        Release.decode(payload)


def test_release_parser_rejects_duplicate_unknown_and_noncanonical_fields():
    encoded = release().encode()
    for payload in (b'{"schema":1,' + encoded[1:], encoded[:-1], b" " + encoded,
                    encoded.replace(b'"schema":1', b'"schema":1,"url":"https://other.test"'),
                    json.dumps(json.loads(encoded), indent=2).encode()):
        with pytest.raises(ValueError, match="invalid_release"):
            Release.decode(payload)


def test_configuration_binds_all_four_exact_public_inputs_independently_of_mapping_order():
    files = {name: name.encode() for name in ("public.json", "bootstrap.json", "ca.pem", "release.pub.pem")}
    digest = configuration_digest(files)
    assert configuration_digest(dict(reversed(list(files.items())))) == digest
    for name in files:
        assert configuration_digest({**files, name: files[name] + b"x"}) != digest
    with pytest.raises(ValueError):
        configuration_digest({**files, "extra": b"data"})
    with pytest.raises(ValueError):
        configuration_digest({**files, "ca.pem": b""})
