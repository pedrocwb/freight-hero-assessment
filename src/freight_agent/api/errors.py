"""API errors: every failure the client can act on is `{"detail": "..."}` with a 4xx.

The service raises these instead of `HTTPException`, so it stays usable without FastAPI
(a CLI, a test) and the status codes are decided in one place.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from freight_agent.adapters.sop.files import SopFormatError
from freight_agent.domain.errors import DomainError

logger = logging.getLogger(__name__)


class ApiError(Exception):
    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class BadRequest(ApiError):
    status_code = 400


class NotFound(ApiError):
    status_code = 404


class Conflict(ApiError):
    status_code = 409


class Unprocessable(ApiError):
    """The request is fine but the content it points at is not (a broken SOP file)."""

    status_code = 422


def _detail(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return _detail(exc.status_code, exc.detail)

    @app.exception_handler(SopFormatError)
    async def _sop_error(request: Request, exc: SopFormatError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return _detail(422, str(exc))

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return _detail(400, str(exc))
