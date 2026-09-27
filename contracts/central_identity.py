"""Central's identity at GET /v1/locate. Devices parse, never byte-compare: unknown keys are
ignored and a higher `api` is accepted. Central keeps every v1 device route while it answers
api >= 1; a change api-1 devices cannot follow is a new route family beside v1.

The identity is a misconfiguration check, not authentication: anyone can serve this body."""

import json
from dataclasses import dataclass
from typing import Final

from contracts.strict_json import loads_object

LOCATE_PATH: Final = "/v1/locate"
SERVICE: Final = "photo-wall-central"
API: Final = 1
MAX_IDENTITY_BYTES: Final = 256


@dataclass(frozen=True, slots=True)
class CentralIdentity:
    api: int


def identity_body() -> bytes:
    """What Central serves: b'{"service":"photo-wall-central","api":1}'."""
    return json.dumps({"service": SERVICE, "api": API}, separators=(",", ":")).encode()


def parse_identity(body: bytes) -> CentralIdentity | None:
    """An object within MAX_IDENTITY_BYTES; `service` exactly SERVICE; `api` an int (not a
    bool) >= 1."""
    document = loads_object(body, max_bytes=MAX_IDENTITY_BYTES)
    if document is None or document.get("service") != SERVICE:
        return None
    api = document.get("api")
    if type(api) is not int or api < 1:
        return None
    return CentralIdentity(api)
