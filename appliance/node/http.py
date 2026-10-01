"""Bounded direct node HTTP transport; never follows credential redirects."""
from __future__ import annotations

import http.client
from urllib.parse import urlsplit

from contracts.node_commands import NodeSessionClaim


class NodeHTTP:
    def __init__(self, central: str):
        self.url = urlsplit(central)
        if self.url.scheme not in ("http", "https") or not self.url.hostname or self.url.username or self.url.password or self.url.query or self.url.fragment or self.url.path not in ("", "/"):
            raise ValueError("node_central_url_invalid")

    def request(self, method: str, path: str, body: bytes | None = None,
                claim: NodeSessionClaim | None = None) -> tuple[int, bytes]:
        connection_type = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(self.url.hostname, self.url.port, timeout=5)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if claim is not None:
            headers.update({"Authorization": "Bearer " + claim.credential,
                            "X-Node-Session": str(claim.session_id)})
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("node_response_bound")
            return response.status, raw
        finally:
            connection.close()

