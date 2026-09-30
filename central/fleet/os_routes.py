"""Separate, data-only loader-OS HTTP seam for exact app attempts.

Mounting requires a request authenticator that has already established a T1/T2
attachment or device identity. This module neither derives authority from T0
serial observations nor mounts itself in Central's production composition root.
The per-operation stores recheck the current session under database locks.
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
from central.fleet.attempt_bytes import AttemptByteAccess
from central.fleet.attempt_reports import AttemptReportStore
from central.fleet.bytes import OfferByteReader
from central.fleet.models import FleetError
from central.fleet.os_route_support import (
    OsRequestVerifier,
    authenticate_os_request,
    bounded_os_body,
    install_os_no_store,
    os_error,
)
from central.fleet.os_route_support import OsVerifierForbidden as OsVerifierForbidden
from central.fleet.os_route_support import OsVerifierUnauthorized as OsVerifierUnauthorized
from central.fleet.principal import PrincipalError
from contracts.os_attempt_report import MAX_ATTEMPT_REPORT_BYTES, parse_os_attempt_report
from contracts.time import Clock

# Keep imports for existing independent route clients; the shared support
# module owns verification and body policy for all loader-OS adapters.
_error = os_error


def mount_os_fleet_data_routes(
    app: FastAPI, *, verifier: OsRequestVerifier, db: Database,
    clock: Clock, content: ContentServices,
) -> None:
    """Bind authenticated loader-OS *data* routes, without command effects.

    There is deliberately no default verifier and no production call site.
    D14 must supply a protected T1/T2 implementation before this can be mounted.
    """
    if not callable(verifier):
        raise ValueError("os_request_verifier_required")
    access = AttemptByteAccess(db, clock, OfferByteReader(content.reader))
    reports = AttemptReportStore(db, clock)

    install_os_no_store(app)

    @app.get("/v1/os/attempts/{attempt_id}/artifacts/{role}")
    async def attempt_artifact(request: Request, attempt_id: UUID, role: str) -> Response:
        principal = await authenticate_os_request(request, verifier)
        if isinstance(principal, JSONResponse):
            return principal
        if role not in ("target", "fallback"):
            return _error("attempt_artifact_request_invalid", 422)
        try:
            opened = await until_disconnect(
                request, access.open_role(principal, attempt_id, role))
        except ClientDisconnected:
            return Response(status_code=499)
        except PrincipalError as exc:
            return _error(str(exc), 403)
        except FleetError as exc:
            return _error(exc.code, exc.status)
        try:
            response = _stream(opened, "application/gzip")
            # Unlike serial-only boot assets, these bytes require a fresh
            # authenticated session and must never be served from a shared cache.
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except BaseException:
            os.close(opened.fd)
            raise

    @app.post("/v1/os/attempts/{attempt_id}/reports")
    async def attempt_report(request: Request, attempt_id: UUID) -> Response:
        principal = await authenticate_os_request(request, verifier)
        if isinstance(principal, JSONResponse):
            return principal
        try:
            raw = await bounded_os_body(
                request, limit=MAX_ATTEMPT_REPORT_BYTES,
                error_code="attempt_report_too_large",
            )
            try:
                report = parse_os_attempt_report(raw)
            except ValueError:
                raise FleetError("attempt_report_invalid", 422) from None
            if report.attempt_id != attempt_id:
                raise FleetError("attempt_report_path_mismatch", 409)
            disposition = await asyncio.to_thread(reports.record, principal, report)
        except ClientDisconnect:
            return Response(status_code=499)
        except PrincipalError as exc:
            return _error(str(exc), 403)
        except FleetError as exc:
            return _error(exc.code, exc.status)
        return JSONResponse({"disposition": disposition},
                            status_code=201 if disposition == "stored" else 200,
                            headers={"Cache-Control": "no-store"})
