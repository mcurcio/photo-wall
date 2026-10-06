"""One failure text for every stage (design §2.5): the clock record's summary and the trust
provenance beside a certificate failure, nothing beside any other cause."""

import pytest
import tls_fixture as tls

from contracts.clock_record import ClockRecord, ClockState
from uplink.causes import Cause, UplinkError
from uplink.diagnosis import MAX_FAILURE_TEXT, failure_text, trust_provenance
from uplink.trust import Trust

RECORD = ClockRecord(state=ClockState.UNSYNCED, floor=1790380800, raised_to_floor=True,
                     tier=None, source=None, offset=None, stepped=False,
                     tried=("dhcp:none", "pool:198.51.100.1:timeout"), writer="netboot",
                     written_at=1790380810.0)
PROVENANCE = "bundle=sha256:0123456789ab anchors=1 floor=2026-09-20"


@pytest.mark.parametrize("error", [
    UplinkError(Cause.TIME, "not_yet_valid", host="photo-wall.example", detail="verify_code=9"),
    UplinkError(Cause.TIME, "expired", host="photo-wall.example", detail="verify_code=10"),
    UplinkError(Cause.TLS, "untrusted", host="photo-wall.example", detail="verify_code=20"),
])
def test_a_certificate_failure_carries_the_clock_summary_and_provenance(error):
    assert failure_text(error, clock=RECORD, provenance=PROVENANCE) == (
        f"{error.console()} {RECORD.summary()} {PROVENANCE}")
    assert failure_text(error, clock=None) == f"{error.console()} clock=unknown"


@pytest.mark.parametrize("error", [
    UplinkError(Cause.TLS, "hostname", host="photo-wall.example", detail="verify_code=62"),
    UplinkError(Cause.CENTRAL, "error", host="photo-wall.example", detail="app_unconfigured",
                central_error="app_unconfigured"),
    UplinkError(Cause.REDIRECT, "unexpected", host="photo-wall.example", detail="status=302"),
    UplinkError(Cause.CONFIGURATION, "absent", detail="not_discovered"),
])
def test_any_other_failure_is_its_console_text_alone(error):
    assert failure_text(error, clock=RECORD, provenance=PROVENANCE) == error.console()


def test_the_text_is_one_bounded_line():
    error = UplinkError(Cause.TIME, "expired", host="h" * 200, detail="d" * 300)
    text = failure_text(error, clock=RECORD, provenance="p" * 900)
    assert len(text) == MAX_FAILURE_TEXT and "\n" not in text


def test_trust_provenance_names_the_bundle_its_anchors_and_the_floor(tmp_path):
    trust = Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA, tls.OTHER_CA))
    assert trust_provenance(trust, 1790380800) == (
        f"bundle=sha256:{trust.sha256[:12]} anchors=2 floor=2026-09-26")
