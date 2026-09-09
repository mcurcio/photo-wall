"""Source-neutral registration protocol shared by central and the Player-only build."""

import json
from typing import Annotated

from pydantic import Field, model_validator

from contracts.models import Identifier, Model

BootTicketId = Annotated[str, Field(pattern=r"^[a-f0-9]{48}$")]


class OutputReport(Model):
    output_id: Identifier
    width_px: int = Field(ge=0, le=16384)
    height_px: int = Field(ge=0, le=16384)
    connected: bool = True


class Enrollment(Model):
    public_key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    nonce: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    signature: Annotated[str, Field(min_length=88, max_length=88)]
    outputs: tuple[OutputReport, ...] = Field(default=(), max_length=2)
    device_id: Identifier
    boot_id: Identifier
    ticket_id: BootTicketId

    @model_validator(mode="after")
    def unique_outputs(self):
        if len({o.output_id for o in self.outputs}) != len(self.outputs):
            raise ValueError("duplicate output")
        return self


def enrollment_message(nonce: str, outputs: tuple[OutputReport, ...], device_id: str,
                       boot_id: str, ticket_id: str) -> bytes:
    return json.dumps({
        "purpose": "photo-wall-enroll-v2",
        "nonce": nonce,
        "outputs": [o.model_dump() for o in outputs],
        "device_id": device_id,
        "boot_id": boot_id,
        "ticket_id": ticket_id,
    }, sort_keys=True, separators=(",", ":")).encode()
