"""OpenAI-shaped error responses.

Clients written against the OpenAI SDK parse ``{"error": {...}}`` and will
misreport anything else as an unknown transport failure, so every error the
``/v1`` surface returns uses that envelope — including the ones that are ours
rather than the model provider's, such as quota refusals.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    """Base class for errors that map onto a specific HTTP status."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type = "gateway_error"
    code: str | None = None

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def to_payload(self) -> dict[str, Any]:
        return error_payload(self.message, type_=self.error_type, code=self.code)


class AuthenticationError(GatewayError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "invalid_request_error"
    code = "invalid_api_key"


class PermissionError_(GatewayError):
    status_code = status.HTTP_403_FORBIDDEN
    error_type = "invalid_request_error"
    code = "permission_denied"


class ModelNotFoundError(GatewayError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "invalid_request_error"
    code = "model_not_found"


class BadRequestError(GatewayError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_request_error"
    code = "bad_request"


class UpstreamUnavailableError(GatewayError):
    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "api_error"
    code = "upstream_unavailable"


class ServiceUnavailableError(GatewayError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "api_error"
    code = "service_unavailable"


def error_payload(
    message: str,
    *,
    type_: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": type_,
            "param": param,
            "code": code,
        }
    }


def error_response(
    message: str,
    *,
    status_code: int,
    type_: str = "invalid_request_error",
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_payload(message, type_=type_, code=code),
        headers=headers,
    )


async def gateway_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, GatewayError)
    headers = {}
    if isinstance(exc, AuthenticationError):
        headers["www-authenticate"] = "Bearer"
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_payload(),
        headers=headers or None,
    )
