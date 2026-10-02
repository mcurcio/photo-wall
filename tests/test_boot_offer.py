"""Pure PXE offer and handoff boundaries; no network, mount or hardware claims."""

import json

import pytest

from appliance.boot_offer import (
    BootOffer,
    BootOfferError,
    boot_nonce,
    read_current_handoff,
    read_handoff,
    write_handoff,
)
from contracts.player_payload import MAX_ARCHIVE_BYTES

BOOT_ID = "11111111-2222-3333-4444-555555555555"
BASE = "a" * 64
APP = "b" * 64
ABI = "sha256:" + "c" * 64


def offer(**changes):
    value = {"schema": 1, "offer_id": "12345678-1234-1234-1234-123456789abc",
             "base": {"sha256": BASE, "size": 123, "tag": "v0.13.0"},
             "initial_app": {"sha256": APP, "size": 456, "tag": "v0.13.0"},
             "initial_app_status": "selected", "compatibility_basis": "abi_match",
             "base_policy_source": "pin", "base_policy_revision": 0,
             "app_policy_source": "override", "app_policy_revision": 7,
             "expires_at": 1800000000.0}
    value.update(changes)
    return json.dumps(value).encode()


def test_offer_correlates_exact_assets_and_revisions():
    parsed = BootOffer.parse(offer())
    assert (parsed.base.sha256, parsed.initial_app.sha256) == (BASE, APP)
    assert (parsed.base_policy_revision, parsed.app_policy_revision) == (0, 7)
    assert parsed.app_policy_source == "override"
    assert BootOffer.parse(offer(initial_app=None, initial_app_status="unconfigured",
                                 compatibility_basis="none")).initial_app is None


def test_schema_two_data_payload_format_and_abi_survive_handoff(tmp_path):
    asset = {"sha256": APP, "size": 456, "tag": "v0.13.0",
             "format": "pw-player-data-v1", "base_abi": ABI}
    parsed = BootOffer.parse(offer(schema=2, initial_app=asset))
    assert parsed.initial_app.format == "pw-player-data-v1"
    path = write_handoff(tmp_path, kernel_boot_id=BOOT_ID, nonce="c" * 32,
                         base_digest=BASE, offer=parsed)
    assert read_handoff(path)["schema"] == 2
    assert read_handoff(path)["base_tag"] == "v0.13.0"
    assert read_handoff(path)["initial_app"]["base_abi"] == ABI
    altered = json.loads(path.read_text())
    altered.pop("base_tag")
    path.write_text(json.dumps(altered))
    assert read_handoff(path) is None
    with pytest.raises(BootOfferError, match="boot_offer_abi"):
        BootOffer.parse(offer(schema=2, initial_app={**asset, "base_abi": "wrong"}))
    with pytest.raises(BootOfferError, match="boot_offer_size"):
        BootOffer.parse(offer(schema=2, initial_app={**asset,
                                                    "size": MAX_ARCHIVE_BYTES + 1}))


@pytest.mark.parametrize("payload", [
    offer(schema=2), offer(base={"sha256": "not-hex", "size": 123}),
    offer(app_policy_revision=-1), offer(base_policy_revision=True),
    offer(initial_app=None), offer(offer_id="not-a-uuid"),
    offer(initial_app_status="unconfigured"), offer(compatibility_basis=[]),
    offer(expires_at=True),
    b'{"schema":1,"schema":1}', b"x" * 4097,
])
def test_malformed_offer_fails_closed(payload):
    with pytest.raises(BootOfferError):
        BootOffer.parse(payload)


def test_nonce_is_stable_within_kernel_boot_and_rotates_on_reboot(tmp_path):
    first = boot_nonce(tmp_path, BOOT_ID)
    assert first == boot_nonce(tmp_path, BOOT_ID)
    assert boot_nonce(tmp_path, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") != first
    assert (tmp_path / "boot-nonce.json").stat().st_mode & 0o777 == 0o600


def test_present_corrupt_nonce_never_reselects_in_the_same_kernel_boot(tmp_path):
    path = tmp_path / "boot-nonce.json"
    first = boot_nonce(tmp_path, BOOT_ID)
    path.write_text('{"kernel_boot_id":"' + BOOT_ID + '","boot_nonce":"broken"}')
    with pytest.raises(BootOfferError, match="boot_nonce_invalid"):
        boot_nonce(tmp_path, BOOT_ID)
    assert "broken" in path.read_text()  # no replacement or second offer identity
    path.write_text('{"kernel_boot_id":"' + BOOT_ID + '","boot_nonce":"' + first + '",'
                    '"boot_nonce":"' + first + '"}')
    with pytest.raises(BootOfferError, match="boot_nonce_invalid"):
        boot_nonce(tmp_path, BOOT_ID)


def test_handoff_is_explicitly_written_to_new_root_and_validated(tmp_path):
    nonce = boot_nonce(tmp_path / "initrd-run", BOOT_ID)
    path = write_handoff(tmp_path / "new-root", kernel_boot_id=BOOT_ID, nonce=nonce,
                         base_digest=BASE, offer=BootOffer.parse(offer()))
    assert path.parent == tmp_path / "new-root/etc/photo-wall"
    assert path.stat().st_mode & 0o777 == 0o600
    assert read_handoff(path)["initial_app"]["sha256"] == APP
    assert read_handoff(path)["initial_app_status"] == "selected"
    assert read_handoff(path)["app_policy_source"] == "override"
    path.write_text('{"schema":1,"boot_nonce":"forged"}')
    assert read_handoff(path) is None


def test_current_boot_handoff_only_treats_absence_as_legacy(tmp_path):
    path = tmp_path / "handoff.json"
    assert read_current_handoff(path, BOOT_ID) is None
    write_handoff(tmp_path, kernel_boot_id=BOOT_ID, nonce="c" * 32,
                  base_digest=BASE, offer=BootOffer.parse(offer()))
    actual = tmp_path / "etc/photo-wall/boot-handoff.json"
    assert read_current_handoff(actual, BOOT_ID)["base_digest"] == BASE
    with pytest.raises(BootOfferError, match="boot_handoff_stale"):
        read_current_handoff(actual, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    actual.write_text("broken")
    with pytest.raises(BootOfferError, match="boot_handoff_invalid"):
        read_current_handoff(actual, BOOT_ID)


def test_handoff_cannot_claim_a_different_base_than_the_offer(tmp_path):
    with pytest.raises(BootOfferError, match="boot_handoff_base_mismatch"):
        write_handoff(tmp_path, kernel_boot_id=BOOT_ID, nonce="c" * 32,
                      base_digest="f" * 64, offer=BootOffer.parse(offer()))


def test_legacy_handoff_does_not_claim_an_offer(tmp_path):
    path = write_handoff(tmp_path, kernel_boot_id=BOOT_ID, nonce="c" * 32,
                         base_digest=BASE, offer=None)
    assert read_handoff(path)["mode"] == "legacy_uncorrelated"
    assert read_handoff(path)["offer_id"] is None
    altered = json.loads(path.read_text())
    altered["schema"] = 2
    path.write_text(json.dumps(altered))
    assert read_handoff(path) is None
