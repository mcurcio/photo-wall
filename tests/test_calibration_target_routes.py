"""The paper target's operator route (central/calibration_target_routes.py): GET
/v1/operator/calibration-target/paper.pdf?paper=letter|a4|4x6. On a bare FastAPI app with the mount
and a stub admin dependency (no database); the real app's guard is tests/test_operator_scope.py's.
The route answers the committed sheet, which tests/test_calibration_boards.py holds to a fresh draw.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from central.calibration_target_routes import PAPER_PDF_PATH, mount_calibration_target_routes
from central.displays.calibration_paper import PDF_MEDIA_TYPE, sheet_path
from central.operator_auth import OPERATOR_PREFIX
from contracts.calibration_target import PaperSize

URL = OPERATOR_PREFIX + PAPER_PDF_PATH
AUTH = {"Authorization": "Bearer operator"}


def _admin(request: Request) -> None:
    if request.headers.get("authorization") != AUTH["Authorization"]:
        raise HTTPException(401)


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    mount_calibration_target_routes(app, admin=_admin)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("paper", list(PaperSize))
def test_the_route_answers_the_pdf_as_an_attachment_never_cached(client, paper):
    answer = client.get(URL, params={"paper": paper.value}, headers=AUTH)
    assert answer.status_code == 200
    assert answer.headers["content-type"] == PDF_MEDIA_TYPE
    assert answer.headers["content-disposition"] == (
        f"attachment; filename=photo-wall-calibration-target-{paper.value}.pdf")
    assert answer.headers["cache-control"] == "private, no-store"
    assert answer.content == sheet_path(paper).read_bytes()
    assert answer.content.startswith(b"%PDF-")


@pytest.mark.parametrize("params", [{}, {"paper": "legal"}, {"paper": "LETTER"}, {"paper": ""}])
def test_a_paper_that_is_not_a_sheet_is_refused(client, params):
    answer = client.get(URL, params=params, headers=AUTH)
    assert (answer.status_code, answer.json()) == (422, {"error": "bad_paper"})


def test_the_route_takes_the_admin_dependency(client):
    assert client.get(URL, params={"paper": "letter"}).status_code == 401
