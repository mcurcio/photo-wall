"""Shared HTTP boundary for separately mounted loader-OS data adapters."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from central.fleet.models import FleetError
from central.fleet.principal import PrincipalError, VerifiedOsPrincipal

OsRequestVerifier = Callable[
    [Request], VerifiedOsPrincipal | Awaitable[VerifiedOsPrincipal]
]


class OsVerifierUnauthorized(Exception):
    """The request has no acceptable loader-OS carrier credential."""


class OsVerifierForbidden(Exception):
    """The carrier is authenticated but cannot act as this loader OS."""


def os_error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status,
                        headers={"Cache-Control": "private, no-store"})


def install_os_no_store(app: FastAPI) -> None:
    """Mark every OS response, including framework errors, private and uncached."""
    if getattr(app.state, "photo_wall_os_no_store_installed", False):
        return
    app.state.photo_wall_os_no_store_installed = True

    @app.middleware("http")
    async def os_no_store(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v1/os/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response


async def authenticate_os_request(
    request: Request, verifier: OsRequestVerifier,
) -> VerifiedOsPrincipal | JSONResponse:
    """Accept only a separately verified T1/T2 carrier, never a T0 claim."""
    try:
        result = verifier(request)
        principal = await result if inspect.isawaitable(result) else result
    except OsVerifierUnauthorized:
        return os_error("os_authentication_required", 401)
    except OsVerifierForbidden:
        return os_error("os_verifier_denied", 403)
    except PrincipalError as exc:
        return os_error(str(exc), 403)
    if type(principal) is not VerifiedOsPrincipal:
        return os_error("os_verifier_denied", 403)
    return principal


async def bounded_os_body(request: Request, *, limit: int, error_code: str) -> bytes:
    """Bound streamed content even when Content-Length is absent or dishonest."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise FleetError(error_code, 413)
        except ValueError:
            pass
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise FleetError(error_code, 413)
        data.extend(chunk)
    return bytes(data)
