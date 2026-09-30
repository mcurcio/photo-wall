"""Unmounted, authenticated loader-OS data routes for post-stop repair.

The route installer requires an injected T1/T2 verifier. A lease id is only a
CAS marker; each call rechecks the verified current carrier and durable drain.
No production composition root mounts this adapter while D14/D17 are open.
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from central.content_routes import ClientDisconnected, _stream, until_disconnect
from central.content_wiring import ContentServices
from central.db import Database
from central.fleet.bytes import OfferByteReader
from central.fleet.models import FleetError
from central.fleet.os_route_support import (
    OsRequestVerifier,
    authenticate_os_request,
    bounded_os_body,
    install_os_no_store,
    os_error,
)
from central.fleet.principal import PrincipalError
from central.fleet.recovery import RecoveryService
from contracts.os_recovery_report import (
    MAX_RECOVERY_CLAIM_BYTES,
    MAX_RECOVERY_REPORT_BYTES,
    parse_os_recovery_report,
    parse_recovery_lease_claim,
)
from contracts.time import Clock


def mount_os_recovery_data_routes(
    app: FastAPI, *, verifier: OsRequestVerifier, db: Database,
    clock: Clock, content: ContentServices,
) -> None:
    """Stage recovery data operations; never mount without a trusted verifier."""
    if not callable(verifier):
        raise ValueError("os_request_verifier_required")
    service = RecoveryService(db, clock, OfferByteReader(content.reader))

    install_os_no_store(app)

    @app.post("/v1/os/attempts/{attempt_id}/recovery-leases")
    async def claim(request: Request, attempt_id: UUID) -> Response:
        principal = await authenticate_os_request(request, verifier)
        if isinstance(principal, JSONResponse):
            return principal
        try:
            raw = await bounded_os_body(
                request, limit=MAX_RECOVERY_CLAIM_BYTES,
                error_code="recovery_body_too_large",
            )
            try:
                body = parse_recovery_lease_claim(raw)
            except ValueError:
                raise FleetError("recovery_lease_request_invalid", 422) from None
            lease = await asyncio.to_thread(
                service.claim, principal, attempt_id, lease_id=body.lease_id,
                expected_lease_id=body.expected_lease_id,
            )
        except ClientDisconnect:
            return Response(status_code=499)
        except PrincipalError as exc:
            return os_error(str(exc), 403)
        except FleetError as exc:
            return os_error(exc.code, exc.status)
        return JSONResponse({
            "lease_id": str(lease.lease_id), "attempt_id": str(lease.attempt_id),
            "lease_sequence": lease.lease_sequence, "expires_at": lease.expires_at,
            "target_sha256": lease.attempt.target_sha256,
            "fallback_sha256": lease.attempt.fallback_sha256,
        }, headers={"Cache-Control": "private, no-store"})

    @app.get("/v1/os/attempts/{attempt_id}/recovery-leases/{lease_id}/artifacts/{role}")
    async def artifact(request: Request, attempt_id: UUID, lease_id: UUID,
                       role: str) -> Response:
        principal = await authenticate_os_request(request, verifier)
        if isinstance(principal, JSONResponse):
            return principal
        if role not in ("target", "fallback"):
            return os_error("recovery_artifact_role_invalid", 422)
        try:
            opened = await until_disconnect(
                request, service.open_role(principal, attempt_id, lease_id, role))
        except ClientDisconnected:
            return Response(status_code=499)
        except PrincipalError as exc:
            return os_error(str(exc), 403)
        except FleetError as exc:
            return os_error(exc.code, exc.status)
        try:
            response = _stream(opened, "application/gzip")
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except BaseException:
            os.close(opened.fd)
            raise

    @app.post("/v1/os/attempts/{attempt_id}/recovery-leases/{lease_id}/reports")
    async def report(request: Request, attempt_id: UUID, lease_id: UUID) -> Response:
        principal = await authenticate_os_request(request, verifier)
        if isinstance(principal, JSONResponse):
            return principal
        try:
            raw = await bounded_os_body(
                request, limit=MAX_RECOVERY_REPORT_BYTES,
                error_code="recovery_body_too_large",
            )
            try:
                value = parse_os_recovery_report(raw)
            except ValueError:
                raise FleetError("recovery_report_invalid", 422) from None
            if value.attempt_id != attempt_id or value.lease_id != lease_id:
                raise FleetError("recovery_report_path_mismatch")
            disposition = await asyncio.to_thread(service.record, principal, value)
        except ClientDisconnect:
            return Response(status_code=499)
        except PrincipalError as exc:
            return os_error(str(exc), 403)
        except FleetError as exc:
            return os_error(exc.code, exc.status)
        return JSONResponse({"disposition": disposition},
                            status_code=201 if disposition == "stored" else 200,
                            headers={"Cache-Control": "private, no-store"})
