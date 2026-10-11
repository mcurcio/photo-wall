"""Container entry point for the synthetic Immich setup and the fixture's health endpoint.

The adapter checks live in the named tests, which reach the published fixture through
tests/support/immich_library.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from scripts.harness_failure import FailureEnvelope
from scripts.immich_actions import ActionError, require, run_action


def serve() -> None:
    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/healthz":
                self.send_error(404)
                return
            data = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    HTTPServer(("0.0.0.0", 8000), Health).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("setup", "serve"))
    parser.add_argument("action", nargs="?")
    args = parser.parse_args()
    if args.role == "serve":
        serve()
        return
    require(args.action is not None, "fixture_action_required")
    print(json.dumps(run_action(args.action), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = error.code.value if isinstance(error, ActionError) else "unexpected_harness_failure"
        role = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("setup", "serve") \
            else "unknown"
        candidate = sys.argv[2] if len(sys.argv) > 2 else ""
        action = candidate if (0 < len(candidate) <= 64
                               and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                                       for character in candidate)) else "unknown"
        failure = FailureEnvelope.create("role_action", role, action, code)
        print(json.dumps(failure.public_payload()), file=sys.stderr)
        raise SystemExit(1) from None
