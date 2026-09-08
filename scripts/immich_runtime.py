"""Container entry point for synthetic Immich actions and adapter verification."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from scripts.harness_failure import FailureEnvelope
from scripts.immich_actions import (
    UPSTREAM,
    ActionError,
    read_json,
    require,
    run_action,
    write_json,
)


async def verify_adapter(action: str, *, page_size: int = 3) -> dict:
    import httpx

    from media.immich import ImmichClient
    from media.models import ConnectionConfig, MediaError, MediaLimits, OriginalAsset, SourceSpec

    runtime = Path("/runtime")
    config = ConnectionConfig.model_validate(read_json(runtime / "connection.json"))
    fixtures = read_json(runtime / "fixtures.json")
    limits = MediaLimits(page_size=page_size, max_search_requests=40, max_candidates=30)
    source = SourceSpec(source_ref="fixture-favorites:1", connection_ref=config.connection_id,
                        favorites=True, media_types=("image",))
    assertions = {}
    async with ImmichClient(config, limits=limits) as adapter:
        if action in ("deny", "outage"):
            result = await adapter.refresh(source)
            expected = "permission" if action == "deny" else "unavailable"
            require(result.snapshot.status == expected, "fault_status_mismatch")
            require(not result.assets, "fault_returned_partial_membership")
            return {"status": result.snapshot.status,
                    "diagnostics": [item.code for item in result.diagnostics]}
        expected = {label: item for label, item in fixtures.items()
                    if item["favorite"] and not item["deleted"]}
        deadline = time.monotonic() + 120
        while True:
            result = await adapter.refresh(source)
            actual = {item.upstream_id: item for item in result.assets}
            if result.snapshot.status == "ok" and set(actual) == {
                    item["upstream_id"] for item in expected.values()}:
                break
            if time.monotonic() >= deadline:
                write_json(runtime / "refresh-failure.json", {
                    "status": result.snapshot.status,
                    "counts": result.counts.model_dump(mode="json"),
                    "diagnostics": [item.code for item in result.diagnostics],
                    "missing_labels": [label for label, item in expected.items()
                                       if item["upstream_id"] not in actual],
                })
                raise ActionError("metadata_convergence_timeout")
            await asyncio.sleep(1)
        if len(expected) > page_size:
            require(result.counts.search_requests > 2, "pagination_not_exercised")
        checked = []
        for label, item in expected.items():
            asset = actual[item["upstream_id"]]
            require((asset.raw_width, asset.raw_height, asset.orientation) ==
                    (item["raw_width"], item["raw_height"], item["orientation"]),
                    "original_geometry_mismatch")
            expected_capture = datetime.fromisoformat(item["captured"].replace("Z", "+00:00"))
            require(asset.captured_at == expected_capture.timestamp(), "capture_time_mismatch")
            destination = runtime / (action + "-" + label + ".jpg")
            started = time.perf_counter()
            downloaded = await adapter.download_original(asset, destination)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            require(downloaded.sha1 == item["sha1"] and downloaded.sha256 == item["sha256"]
                    and downloaded.size == item["size"], "original_integrity_mismatch")
            checked.append({"label": label, "sha256": downloaded.sha256,
                            "size": downloaded.size, "width": asset.original_width,
                            "height": asset.original_height, "orientation": asset.orientation,
                            "acquisition_ms": elapsed_ms})
        if action == "initial":
            write_json(runtime / "selected.json", actual[fixtures["portrait"]["upstream_id"]]
                       .model_dump(mode="json"))
            empty = await adapter.refresh(source.model_copy(update={"captured_from":
                                          datetime(2040, 1, 1, tzinfo=timezone.utc).timestamp()}))
            require(empty.snapshot.status == "ok" and not empty.assets, "empty_query_failed")
            async with ImmichClient(config, limits=MediaLimits(
                    page_size=3, max_candidates=1)) as small:
                overflow = await small.refresh(source)
                require("source_limit" in [item.code for item in overflow.diagnostics]
                        and not overflow.assets, "source_limit_not_enforced")
            async with httpx.AsyncClient(
                    base_url=UPSTREAM, trust_env=False, follow_redirects=False,
                    headers={"x-api-key": config.api_key.get_secret_value()}, timeout=15) as client:
                response = await client.put("/assets/" + fixtures["square"]["upstream_id"],
                                            json={"isFavorite": True})
                require(response.status_code == 403, "runtime_mutation_not_denied")
                data = (runtime / "initial-portrait.jpg").read_bytes()
                response = await client.post("/assets", data={
                    "deviceAssetId": "denied", "deviceId": "denied",
                    "fileCreatedAt": "2024-12-11T12:00:00Z",
                    "fileModifiedAt": "2024-12-11T12:00:00Z",
                }, files={"assetData": ("denied.jpg", data, "image/jpeg")})
                require(response.status_code == 403, "runtime_upload_not_denied")
            assertions.update(empty_query="ok", source_limit="source_limit",
                              runtime_update_status=403, runtime_upload_status=403)
        if action == "deleted":
            old = OriginalAsset.model_validate(read_json(runtime / "selected.json"))
            try:
                await adapter.download_original(old, runtime / "deleted-selected.jpg")
            except MediaError as error:
                require(error.code in ("asset_missing", "asset_unavailable"),
                        "deleted_asset_wrong_failure")
                deleted_code = error.code
            else:
                raise ActionError("deleted_asset_download_succeeded")
            retained = (runtime / "initial-portrait.jpg").read_bytes()
            require(hashlib.sha256(retained).hexdigest() == fixtures["portrait"]["sha256"],
                    "local_download_changed_after_upstream_deletion")
            assertions.update(deleted_original=deleted_code, retained_local_bytes="exact")
        return {"status": result.snapshot.status, "checked_originals": checked,
                "search_requests": result.counts.search_requests, "page_size": page_size,
                "diagnostics": [item.code for item in result.diagnostics],
                "assertions": assertions}


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
    parser.add_argument("role", choices=("setup", "verify", "serve"))
    parser.add_argument("action", nargs="?")
    parser.add_argument("--page-size", type=int, default=3)
    args = parser.parse_args()
    if args.role == "serve":
        serve()
        return
    require(args.action is not None, "fixture_action_required")
    result = (run_action(args.action) if args.role == "setup"
              else asyncio.run(verify_adapter(args.action, page_size=args.page_size)))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = error.code.value if isinstance(error, ActionError) else "unexpected_harness_failure"
        role = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("setup", "verify", "serve") \
            else "unknown"
        candidate = sys.argv[2] if len(sys.argv) > 2 else ""
        action = candidate if (0 < len(candidate) <= 64
                               and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                                       for character in candidate)) else "unknown"
        failure = FailureEnvelope.create("role_action", role, action, code)
        print(json.dumps(failure.public_payload()), file=sys.stderr)
        raise SystemExit(1) from None
