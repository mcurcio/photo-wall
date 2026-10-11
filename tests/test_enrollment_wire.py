"""The Player sign-up wire, frozen: the bytes a Player signs and the body it posts to
`/v1/enrollment/register`. Central and the Player call the same functions, so a change on one
side is invisible to every round-trip test; these vectors make any change to the wire fail here,
where it must be decided as a cross-release break (Central and Players upgrade separately).

PR 45 removed `ticket_id` from both: a Player built before it posts `"ticket_id": null` and signs
it, and this Central refuses that body (422). That is the recorded break (.claude/errata.md,
"PR 45 widened", deploy order), pinned by the last test.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.enrollment import Enrollment, OutputReport, enrollment_message
from player.identity import load_identity

NONCE = "a" * 64
DEVICE = "device-" + "b" * 64
BOOT = "12345678-1234-1234-1234-123456789abc"
OUTPUTS = (OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080),)


def test_the_signed_enrollment_message_is_frozen():
    assert enrollment_message(NONCE, OUTPUTS, DEVICE, BOOT) == (
        b'{"boot_id":"12345678-1234-1234-1234-123456789abc","device_id":"device-'
        + b"b" * 64
        + b'","nonce":"' + b"a" * 64
        + b'","outputs":[{"connected":true,"height_px":1080,"output_id":"HDMI-A-1",'
        b'"width_px":1920}],"purpose":"photo-wall-enroll-v2"}')


def test_the_register_body_a_player_posts_is_frozen():
    body = load_identity().enrollment(NONCE, OUTPUTS, device_id=DEVICE,
                                      boot_id=BOOT).model_dump(mode="json")
    assert set(body) == {"public_key", "nonce", "signature", "outputs", "device_id", "boot_id"}
    assert body["outputs"] == [{"output_id": "HDMI-A-1", "width_px": 1920, "height_px": 1080,
                                "connected": True}]
    assert Enrollment.model_validate(body).model_dump(mode="json") == body


def test_a_pre_pr45_body_with_ticket_id_is_refused():
    body = load_identity().enrollment(NONCE, OUTPUTS, device_id=DEVICE,
                                      boot_id=BOOT).model_dump(mode="json")
    with pytest.raises(ValidationError, match="ticket_id"):
        Enrollment.model_validate({**body, "ticket_id": None})
