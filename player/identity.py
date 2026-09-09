"""Ephemeral Player session identity.

Equipment identity and Frame bindings are central concerns. A Player process proves
possession of a fresh key only for its current enrollment session and never reads or
writes that key to local storage.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from contracts.enrollment import BootTicketId, Enrollment, OutputReport, enrollment_message
from contracts.models import Identifier


@dataclass(frozen=True)
class Identity:
    _key: Ed25519PrivateKey = field(repr=False)
    @property
    def public_key(self) -> str:
        return self._key.public_key().public_bytes_raw().hex()

    def enrollment(
        self,
        nonce: str,
        outputs: tuple[OutputReport, ...],
        *,
        device_id: Identifier,
        boot_id: Identifier,
        ticket_id: BootTicketId,
    ) -> Enrollment:
        signature = self._key.sign(
            enrollment_message(nonce, outputs, device_id, boot_id, ticket_id)
        )
        return Enrollment(
            public_key=self.public_key,
            nonce=nonce,
            signature=base64.b64encode(signature).decode(),
            outputs=outputs,
            device_id=device_id,
            boot_id=boot_id,
            ticket_id=ticket_id,
        )


def load_identity() -> Identity:
    """Create one process-local enrollment key."""
    return Identity(Ed25519PrivateKey.generate())


__all__ = ["Identity", "load_identity"]
