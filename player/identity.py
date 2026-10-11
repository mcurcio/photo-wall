"""Ephemeral Player session identity.

Equipment identity and Frame bindings are central concerns. A Player process proves
possession of a fresh key only for its current enrollment session and never reads or
writes that key to local storage.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from contracts.enrollment import Enrollment, OutputReport, enrollment_message
from contracts.models import Identifier
from contracts.node_app_link import NodeAppLinkChallengeV2, NodeAppLinkV2, node_app_link_message


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
    ) -> Enrollment:
        signature = self._key.sign(enrollment_message(nonce, outputs, device_id, boot_id))
        return Enrollment(
            public_key=self.public_key,
            nonce=nonce,
            signature=base64.b64encode(signature).decode(),
            outputs=outputs,
            device_id=device_id,
            boot_id=boot_id,
        )

    def sign_node_app_link(self, challenge: NodeAppLinkChallengeV2) -> NodeAppLinkV2:
        """Sign LAN node linkage in its own domain."""
        return NodeAppLinkV2(challenge, self.public_key,
                             self._key.sign(node_app_link_message(challenge)).hex())


def load_identity() -> Identity:
    """Create one process-local enrollment key."""
    return Identity(Ed25519PrivateKey.generate())


__all__ = ["Identity", "load_identity"]
