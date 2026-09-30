"""Ephemeral Player session identity.

Equipment identity and Frame bindings are central concerns. A Player process proves
possession of a fresh key only for its current enrollment session and never reads or
writes that key to local storage.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from contracts.app_process_proof import (
    AppProofChallenge,
    AppProofChallengeV2,
    AppProofResponse,
    app_proof_message,
    app_proof_message_v2,
)
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
        ticket_id: BootTicketId | None,
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

    def sign_app_proof(self, challenge: AppProofChallenge) -> AppProofResponse:
        """Prove this process's enrollment key to the local base OS verifier."""
        return AppProofResponse(
            nonce=challenge.nonce,
            public_key=self.public_key,
            signature=base64.b64encode(self._key.sign(app_proof_message(challenge))).decode(),
        )

    def sign_applied_control_proof(self, challenge: AppProofChallengeV2) -> AppProofResponse:
        """Sign the separate post-ACK domain with this enrollment's key."""
        return AppProofResponse(
            nonce=challenge.nonce,
            public_key=self.public_key,
            signature=base64.b64encode(self._key.sign(app_proof_message_v2(challenge))).decode(),
        )


def load_identity() -> Identity:
    """Create one process-local enrollment key."""
    return Identity(Ed25519PrivateKey.generate())


__all__ = ["Identity", "load_identity"]
