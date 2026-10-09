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
BASE_TAG = "v1.0.0"
BASE_SHA = "a" * 64
BASE_TARBALL_SHA = "b" * 64
BASE_ABI = "sha256:" + "c" * 64


def _seed(registry) -> None:
    """The base release and the device every node test boots."""
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at,base_tarball_sha256) "
                     "VALUES(%s,1,0,0,FALSE,900,900,%s)", (BASE_TAG, BASE_TARBALL_SHA))
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,%s,900,900)", (DEVICE_ID, SERIAL))
