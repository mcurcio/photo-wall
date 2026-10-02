"""Shared fleet device/offer fixtures for the node tests.

The queued-attempt tests left with the retired V1 T1/T2 command path; other suites
(including the PID1 Central fixture) import these seeds by this module name.
"""

from uuid import UUID

from central.content_catalog.catalog import device_id_for_serial

SERIAL = "abcdef1234567890"
DEVICE_ID = device_id_for_serial(SERIAL)
assert DEVICE_ID is not None
BOOT_ID = UUID(int=901)
OFFER_ID = UUID(int=902)
SESSION_ID = UUID(int=903)
AUDIENCE = "test-installation-1"
BASE_TAG = "v1.0.0"
TARGET_TAG = "v1.0.1"
BASE_SHA = "a" * 64
BASE_TARBALL_SHA = "b" * 64
BASE_ABI = "sha256:" + "c" * 64
TARGET_SHA = "d" * 64
FALLBACK_SHA = "e" * 64


def _seed(registry, *, offer_audience: str = AUDIENCE,
          fallback_qualified: bool = True, target_sha: str = TARGET_SHA,
          target_size: int = 123, fallback_sha: str = FALLBACK_SHA,
          fallback_size: int = 80) -> None:
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at,base_tarball_sha256,base_abi,"
                     "base_abi_squashfs_sha256,base_abi_source_manifest) "
                     "VALUES(%s,1,0,0,FALSE,900,900,%s,%s,%s,'manifest.v2.json')",
                     (BASE_TAG, BASE_TARBALL_SHA, BASE_ABI, BASE_SHA))
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at,mirror_state,payload_url,payload_sha256,"
                     "payload_size,payload_format,payload_base_abi,payload_source_manifest) "
                     "VALUES(%s,1,0,1,FALSE,900,900,'mirrored',%s,%s,%s,"
                     "'pw-player-data-v1',%s,'manifest.v2.json')",
                     (TARGET_TAG, "https://example.invalid/target-old.tar.gz",
                      target_sha, target_size, BASE_ABI))
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,%s,900,900)", (DEVICE_ID, SERIAL))
        conn.execute("INSERT INTO fleet_boot_offers(offer_id,installation_audience,device_id,"
                     "serial,kernel_boot_id,boot_nonce,base_policy_source,base_policy_revision,"
                     "app_policy_source,app_policy_revision,base_tag,base_content_key,"
                     "base_sha256,base_size,app_status,compatibility_basis,offer_schema,"
                     "created_at,expires_at) "
                     "VALUES(%s,%s,%s,%s,%s,%s,'operator_baseline',1,'explicit',1,%s,%s,"
                     "%s,1024,'unconfigured','none',2,900,2000)",
                     (OFFER_ID, offer_audience, DEVICE_ID, SERIAL, BOOT_ID, "1" * 32,
                      BASE_TAG, BASE_TARBALL_SHA, BASE_SHA))
        conn.execute("INSERT INTO fleet_os_command_sessions(command_session_id,device_id,"
                     "device_generation,kernel_boot_id,offer_id,installation_audience,"
                     "trust_mode,agent_key_sha256,verifier_ref,issued_at,expires_at) "
                     "VALUES(%s,%s,1,%s,%s,%s,'t1',%s,'test-gateway',900,1100)",
                     (SESSION_ID, DEVICE_ID, BOOT_ID, OFFER_ID, AUDIENCE, "f" * 64))
        conn.execute("INSERT INTO fleet_app_policy(singleton,revision,target_tag,"
                     "target_sha256,target_size,target_format,changed_at) "
                     "VALUES(TRUE,1,%s,%s,%s,'pw-player-data-v1',900)",
                     (TARGET_TAG, target_sha, target_size))
        if fallback_qualified:
            conn.execute("INSERT INTO fleet_generation_acceptances(device_id,"
                         "device_generation,kind,content_key,sha256,size,base_abi,"
                         "trust_mode,evidence_ref,accepted_at,basis,command_session_id) "
                         "VALUES(%s,1,'app',%s,%s,%s,%s,'t1',"
                         "'qualified-output-proof',900,'cold_boot',%s)",
                         (DEVICE_ID, fallback_sha, fallback_sha, fallback_size,
                          BASE_ABI, SESSION_ID))
            conn.execute("INSERT INTO assets(kind,identity,created_at) "
                         "VALUES('player-payload',%s,900)", (fallback_sha,))
            conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                         "locator_sha256,locator_size,expected_sha256,expected_size,added_at) "
                         "VALUES('player-payload',%s,%s,%s,%s,%s,%s,%s,900)",
                         (fallback_sha, f"fleet-fallback:{DEVICE_ID}:1",
                          "https://example.invalid/fallback.tar.gz", fallback_sha,
                          fallback_size, fallback_sha, fallback_size))
