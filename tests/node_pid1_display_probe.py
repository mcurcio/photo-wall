"""What the Pi's Display Host has handed each Output to, as root reads it on the Pi: one `outputs`
packet on the controller's root-only ingress (appliance/display_host/runner.py `Controller.receive`,
`answer_ingress`), answered with every Output's state and the handoff's two inputs (the broker's
run and each Output's binding, appliance/display_host/handoff.py `Handoffs.inputs`).

The real-PID1 scenarios (tests/test_node_pid1.py) prove the Pi's handoff with it: in that fixture
the Player has no GPU (no /dev/dri/renderD128, and headless Weston's pixman renderer offers no
zwp_linux_dmabuf_v1), so its renderer is never made and it never presents; the Display Host's offer
to the broker's current run for the bound Frame is what the fixture can show, and Weston's scene
graph never holds a Player view there. Rendering on a GPU is the render bench's (docs/validation.md).
tests/node/display/test_display_handoff.py runs `DISPLAY_OUTPUTS` against a real ingress.
"""
from __future__ import annotations

# Run as root with the ingress socket's path as argv[1]: prints the controller's answer.
DISPLAY_OUTPUTS = """import socket,sys
s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);s.settimeout(5)
s.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
s.connect(sys.argv[1]);s.send(b'{"op":"outputs"}')
print(s.recv(1<<20).decode())"""


def held(answer: dict, output_id: str) -> dict | None:
    """The surface `output_id` is held for (its admitted surface, else its candidate: offered and
    awaiting the Player's presentation), or None. AssertionError when the answer was refused or
    names no such Output."""
    assert answer.get("accepted") is True, answer
    [state] = [each for each in answer["outputs"] if each["key"]["output_id"] == output_id] or [None]
    assert state is not None, ("Display knows no " + output_id, answer)
    return state["admitted"] if state["admitted"] is not None else state["candidate"]


def handed_to(answer: dict, output_id: str, *, pid: int, invocation_id: str, frame_id: str) -> bool:
    """Whether Display holds `output_id` for the run `pid`/`invocation_id` (hex with or without
    dashes) and the Frame `frame_id`, with the broker's run told being that same run."""
    surface = held(answer, output_id)
    run = answer["handoff"]["run"]
    same = invocation_id.replace("-", "")
    return (surface is not None and run is not None
            and surface["process"]["pid"] == pid == run["pid"]
            and str(surface["process"]["invocation_id"]).replace("-", "") == same
            and run["invocation_id"].replace("-", "") == same
            and surface["frame_id"] == frame_id)


__all__ = ["DISPLAY_OUTPUTS", "handed_to", "held"]
