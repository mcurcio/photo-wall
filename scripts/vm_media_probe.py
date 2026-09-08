"""Operator actions and read-only evidence inside the isolated boot central.

The admin credential stays in the central environment. Only public identifiers,
the synthetic query interval, and bounded presentation evidence cross the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import time
from contextlib import contextmanager
from typing import Annotated, Literal, Self

import httpx
from pydantic import ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, model_validator

from central.db import Database
from central.installation_models import InstallationInventory
from central.registry import FrameCreate
from contracts.enrollment import OutputReport
from contracts.models import Digest, FrameProfile, Identifier, Model
from media.models import SourceSpec
from scripts.vm_media_evidence import read_grants, read_presentations, stale_session

SOURCE = "vm-photo:1"
FRAME = "vm-photo-frame"
SCENE = "vm-photo"
# This is the fixture's authored presentation target, independent of connector
# mode observations or GTK's actual framebuffer allocation. Unknown stays 0x0.
VM_PHOTO_FRAME = FrameCreate(id=FRAME, width_mm=500, height_mm=281.25,
    profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24, video=False))
MAX_RESPONSE = 1024 * 1024
ProbeCode = Literal[
    "fixture_capture_interval", "media_player_authority", "native_output_missing",
    "native_output_mode", "operator_response_invalid", "grant_history_bound",
    "grant_history_invalid", "prior_epoch_invalid", "probe_contract_invalid",
    "probe_transport", "probe_internal",
]
ProbePhase = Literal[
    "arguments", "operator_connect", "capture_interval", "inventory", "session", "output",
    "source", "frame", "binding", "calibration", "scene", "program",
    "evidence_arguments", "evidence_read", "stale_arguments", "stale_read", "result",
]


class ProbeEnvelope(Model):
    @model_validator(mode="before")
    @classmethod
    def strict_boolean(cls, value):
        if isinstance(value, dict) and "ok" in value and type(value["ok"]) is not bool:
            raise ValueError("probe status must be boolean")
        return value


class ProbeSuccess(ProbeEnvelope):
    ok: Literal[True] = True
    value: dict[str, JsonValue]


class ProbeFailure(ProbeEnvelope):
    ok: Literal[False] = False
    error: ProbeCode
    phase: ProbePhase


class MediaConfigurationReceipt(Model):
    """The authored fixture and unmodified equipment observation have distinct roles."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Annotated[int, Field(ge=1, le=1)]
    kind: Literal["vm-photo-configured"]
    frame_id: Literal[FRAME]
    output_id: Identifier
    source_ref: Literal[SOURCE]
    player_id: Identifier
    authority_epoch: Annotated[int, Field(ge=1)]
    starts_at: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    authored_frame: FrameCreate
    observed_output: OutputReport

    @model_validator(mode="before")
    @classmethod
    def complete_nested_facts(cls, value):
        if isinstance(value, dict):
            for name, fields in (("authored_frame", FrameCreate.model_fields),
                                 ("observed_output", OutputReport.model_fields)):
                item = value.get(name)
                if isinstance(item, dict) and set(item) != set(fields):
                    raise ValueError("configuration_fact_fields")
            authored = value.get("authored_frame")
            if isinstance(authored, dict):
                profile = authored.get("profile")
                if isinstance(profile, dict) and set(profile) != set(FrameProfile.model_fields):
                    raise ValueError("configuration_profile_fields")
        return value

    @model_validator(mode="after")
    def matching_configuration(self) -> Self:
        if self.authored_frame != VM_PHOTO_FRAME or self.authored_frame.id != self.frame_id:
            raise ValueError("configuration_authored_frame")
        if self.observed_output.output_id != self.output_id or not self.observed_output.connected:
            raise ValueError("configuration_output_identity")
        return self


def decode_configuration(value: dict) -> MediaConfigurationReceipt:
    """Require the exact configure receipt before the host consumes or journals it."""
    return MediaConfigurationReceipt.model_validate(value, strict=True)


PROBE_RESULT = TypeAdapter(Annotated[ProbeSuccess | ProbeFailure, Field(discriminator="ok")])


def decode_result(payload: bytes) -> ProbeSuccess | ProbeFailure:
    if len(payload) > MAX_RESPONSE:
        raise ValueError("probe response bound")
    return PROBE_RESULT.validate_json(payload)


class ProbeError(ValueError):
    def __init__(self, code: ProbeCode, *, phase: ProbePhase = "arguments"):
        self.failure = ProbeFailure(error=code, phase=phase)
        super().__init__(code)


@contextmanager
def probe_phase(phase: ProbePhase):
    """Keep only allowlisted failure metadata at each operation boundary."""
    try:
        yield
    except ProbeError as error:
        raise ProbeError(error.failure.error, phase=phase) from None
    except ValidationError:
        raise ProbeError("probe_contract_invalid", phase=phase) from None
    except httpx.HTTPError:
        raise ProbeError("probe_transport", phase=phase) from None
    except Exception:
        raise ProbeError("probe_internal", phase=phase) from None


class Operator:
    def __init__(self):
        context = ssl.create_default_context(cafile="/public/ca.pem")
        self.client = httpx.Client(base_url="https://photo-wall.test", verify=context,
            trust_env=False, follow_redirects=False, timeout=10,
            headers={"Authorization": "Bearer " + os.environ["PHOTO_WALL_ADMIN_TOKEN"]})

    def close(self):
        self.client.close()

    def request(self, method, path, body=None):
        with self.client.stream(method, path, json=body) as response:
            expected = 201 if method == "POST" and path == "/v1/operator/frames" else 200
            if response.status_code != expected:
                raise ProbeError("operator_response_invalid")
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    raise ProbeError("operator_response_invalid")
            return json.loads(data)


def configure(operator, *, player_id: str, epoch: int, captured_from: float,
              captured_until: float, now=time.time) -> MediaConfigurationReceipt:
    with probe_phase("capture_interval"):
        source = SourceSpec(source_ref=SOURCE, connection_ref="fixture-library", favorites=True,
                            captured_from=captured_from, captured_until=captured_until, media_types=("image",))
        if (source.captured_from is None or source.captured_until is None
                or source.captured_until - source.captured_from > 1):
            raise ProbeError("fixture_capture_interval")
    with probe_phase("inventory"):
        inventory = InstallationInventory.model_validate(operator.request("GET", "/v1/operator/inventory"))
    with probe_phase("session"):
        owners = [p for p in inventory.players if p.id == player_id
                  and p.authority_epoch == epoch and p.retired_at is None]
        # Installation owns session authority. Player-local health and release
        # acceptance are separate observations, already checked by ApplianceE2E.
        if len(owners) != 1:
            raise ProbeError("media_player_authority")
    outputs = sorted((o for o in inventory.outputs if o.player_id == player_id
                      and o.observation.connected), key=lambda o: o.output_id)
    if not outputs:
        raise ProbeError("native_output_missing", phase="output")
    output = outputs[0]
    if output.output_id != output.observation.output_id:
        raise ProbeError("probe_contract_invalid", phase="output")
    with probe_phase("source"):
        operator.request("PUT", "/v1/operator/sources/" + SOURCE, source.model_dump(mode="json", by_alias=True))
    with probe_phase("frame"):
        operator.request("POST", "/v1/operator/frames",
                         VM_PHOTO_FRAME.model_dump(mode="json"))
    with probe_phase("binding"):
        operator.request("PUT", "/v1/operator/frames/" + FRAME + "/binding",
            dict(player_id=player_id, output_id=output.output_id, expected_generation=0))
    with probe_phase("calibration"):
        operator.request("POST", "/v1/operator/frames/" + FRAME + "/calibration",
            dict(operation="commit", expected_revision=1, expected_generation=1, calibration={}))
    with probe_phase("scene"):
        operator.request("PUT", "/v1/operator/scenes/" + SCENE,
            dict(scene_id=SCENE, loop=True, cycle_seconds=120,
                 contributions=[dict(target="frame:"+FRAME, kind="media", source_refs=[SOURCE])]))
    with probe_phase("program"):
        started = now()
        operator.request("PUT", "/v1/operator/programs/" + SCENE,
            dict(program_id=SCENE, scene_id=SCENE, starts_at=started+90, ends_at=started+7200))
    with probe_phase("result"):
        return MediaConfigurationReceipt(schema_version=1, kind="vm-photo-configured",
            frame_id=FRAME, output_id=output.output_id, source_ref=SOURCE,
            player_id=player_id, authority_epoch=epoch, starts_at=started+90,
            authored_frame=VM_PHOTO_FRAME, observed_output=output.observation)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "evidence", "stale"))
    parser.add_argument("--player-id", required=True)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--output-id")
    parser.add_argument("--captured-from", type=float)
    parser.add_argument("--captured-until", type=float)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--original-sha256")
    parser.add_argument("--prior-grants", default="[]")
    parser.add_argument("--prior-epoch", type=int)
    args = parser.parse_args(argv)
    try:
        with probe_phase("arguments"):
            TypeAdapter(Identifier).validate_python(args.player_id)
            if args.epoch < 1:
                raise ProbeError("media_player_authority")
        if args.action == "configure":
            with probe_phase("operator_connect"):
                operator = Operator()
            try:
                result = configure(operator, player_id=args.player_id, epoch=args.epoch,
                    captured_from=args.captured_from, captured_until=args.captured_until).model_dump(mode="json")
            finally:
                operator.close()
        elif args.action == "evidence":
            with probe_phase("evidence_arguments"):
                TypeAdapter(Identifier).validate_python(args.output_id)
                TypeAdapter(Digest).validate_python(args.original_sha256)
                if len(args.prior_grants) > 65536:
                    raise ProbeError("grant_history_bound")
                prior = json.loads(args.prior_grants)
                if not isinstance(prior, list) or len(prior) > 64 or any(not isinstance(g, dict) for g in prior):
                    raise ProbeError("grant_history_invalid")
            with probe_phase("evidence_read"):
                db = Database(os.environ["PHOTO_WALL_DATABASE_URL"])
                try:
                    with db.transaction() as connection:
                        result = dict(grants=read_grants(connection, player_id=args.player_id, authority_epoch=args.epoch),
                            presentations=read_presentations(connection, player_id=args.player_id,
                            authority_epoch=args.epoch, frame_id=FRAME, output_id=args.output_id,
                            source_ref=SOURCE, expected_sha256=args.expected_sha256,
                            expected_original_sha256=args.original_sha256, prior_grants=tuple(prior)))
                finally:
                    db.close()
        else:
            with probe_phase("stale_arguments"):
                if args.prior_epoch is None or not 0 < args.prior_epoch < args.epoch:
                    raise ProbeError("prior_epoch_invalid")
            with probe_phase("stale_read"):
                db = Database(os.environ["PHOTO_WALL_DATABASE_URL"])
                try:
                    with db.transaction() as connection:
                        result = stale_session(connection, player_id=args.player_id,
                                               current_epoch=args.epoch, prior_epoch=args.prior_epoch)
                finally:
                    db.close()
        with probe_phase("result"):
            encoded = ProbeSuccess(value=result).model_dump_json()
            if len(encoded.encode()) > MAX_RESPONSE:
                raise ProbeError("operator_response_invalid")
    except ProbeError as error:
        encoded = error.failure.model_dump_json()
    except Exception:
        # Neither HTTP error bodies nor DB/credential values are public evidence.
        encoded = ProbeFailure(error="probe_internal", phase="arguments").model_dump_json()
    # Handled domain results use successful process transport. A nonzero exit
    # remains reserved for a process failure that never produced this contract.
    print(encoded)


if __name__ == "__main__":
    main()
