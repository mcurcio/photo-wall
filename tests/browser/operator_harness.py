"""Shared browser-test harness: run the production app on a loopback listener.

Relocated here at the Bead 17 cutover from the retired `test_operator_browser.py`
(the legacy flat-page test). These helpers are transport-level only — spin up the
real FastAPI app over real HTTP, and read the public Installation contract — so
they outlive the flat page and are imported by the `/console` browser tests.
"""

import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from test_registry import ADMIN

from central.app import create_app
from central.installation_models import InstallationInventory


@contextmanager
def operator_server(db, clock, *, media_root=None, media_queue=None):
    """Run the production app on an ephemeral loopback listener with real lifespan."""
    app = create_app(db, clock, ADMIN, run_scheduler=False,
                     media_root=media_root, media_queue=media_queue)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started, "operator server did not start"
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "operator server did not stop"


def inventory(page, origin, token=ADMIN):
    """Read-only proof through the complete public Installation contract."""
    response = page.request.get(origin + "/v1/operator/inventory", headers={
        "Authorization": "Bearer " + token,
    })
    assert response.status == 200
    return InstallationInventory.model_validate_json(response.body())
