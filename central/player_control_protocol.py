"""One pure Player state projection shared by REST and WebSocket transports."""

import hashlib
import json

from contracts.player_control import ControlSelection


def project_state(delivery: dict, selection: ControlSelection) -> dict:
    """The state a negotiated session reads; the Identify cue only where it was selected."""
    cue = delivery["identify_output"] if "identify_output" in selection.capabilities else None
    return {
        "configuration": delivery["configuration"].model_dump(mode="json"),
        "plan": delivery["plan"].model_dump(mode="json") if delivery["plan"] else None,
        "commits": [item.model_dump(mode="json") for item in delivery["commits"]],
        "revocations": [item.model_dump(mode="json") for item in delivery["revocations"]],
        "identify_output": cue.model_dump(mode="json") if cue else None,
    }


def state_digest(state: dict) -> str:
    """Bind an acknowledgment to semantic state, excluding the cue's live countdown.

    `remaining_seconds` is only a display deadline hint. It changes on every
    WebSocket poll while the Identify request itself is unchanged; including it
    would replace the pending challenge faster than a slower Player can apply it.
    The request ID, Output and authority epoch remain part of the digest.
    """
    semantic = dict(state)
    semantic.pop("identify_expires_at", None)
    semantic.pop("delivery_id", None)
    semantic.pop("delivery_sequence", None)
    cue = semantic.get("identify_output")
    if cue is not None:
        semantic["identify_output"] = {key: value for key, value in cue.items()
                                       if key != "remaining_seconds"}
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()
