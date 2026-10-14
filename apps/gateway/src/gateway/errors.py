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

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        #: Extra top-level fields for the response body, beside the OpenAI
        #: `error` envelope. Used by the code_agents gate so a refused /v1/me
        #: still tells the client its capabilities (ADR 0096).
        self.extra_payload = extra_payload

    def to_payload(self) -> dict[str, Any]:
        payload = error_payload(self.message, type_=self.error_type, code=self.code)
        if self.extra_payload:
            payload.update(self.extra_payload)
        return payload


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


class NotFoundError(GatewayError):
    """A management-surface object that does not exist — from this caller's
    point of view. Deliberately not :class:`ModelNotFoundError`, whose error
    code is part of the OpenAI wire contract for `/v1/models`."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "invalid_request_error"
    code = "not_found"


class BadRequestError(GatewayError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_request_error"
    code = "bad_request"


class OutsideAccessGroupsError(GatewayError):
    """The join gate refused an authenticated `/v1` caller (ADR 0095).

    403 rather than 401: the caller *is* authenticated — the credential
    verified — and what is refused is access under the group policy, which
    regaining the group restores. A 401 would tell Cerea's machine
    revalidation the token died and make it revoke the device permanently,
    forcing a re-enrol for a loss that was meant to be temporary. The
    message names no group: a prober learns nothing to ask for.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "invalid_request_error"
    code = "outside_access_groups"


class AgentsNotAllowedError(GatewayError):
    """A machine-client token from a person without the code_agents
    capability (ADR 0096).

    403 rather than 401 for the same reason as ``outside_access_groups``:
    the token is valid, the loss is one a group flag restores, and a 401
    would make Cerea's machine revalidation revoke the device permanently.
    On ``/v1/me`` the body still carries ``capabilities`` so the client can
    tell why.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "invalid_request_error"
    code = "agents_not_allowed"


class ApiKeysNotAllowedError(GatewayError):
    """An API key its owner may not hold, or may not mint (ADR 0096).

    Refused, never deleted: a key whose owner loses the api_keys capability
    stops working until a group flag restores it, and comes back the moment
    it does. The mint refusal and the use refusal are the same code so a
    client (the install script included) needs one string to recognise.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "invalid_request_error"
    code = "api_keys_not_allowed"


class ContentBlockedError(GatewayError):
    """A redaction policy refused the request before any provider saw it.

    403 rather than 400: the request is well-formed and the caller is
    authenticated; what is refused is the *content*, under a policy an
    administrator set, and no amount of re-sending the same prompt will change
    that (ADR 0038).

    The message names the entity type and the scope whose rule decided, and
    **never quotes the matched text**. Echoing a blocked credential into an
    error body — which is logged, and which the caller may paste into a ticket —
    would leak exactly what the block exists to contain.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "invalid_request_error"
    code = "content_blocked"


class UpstreamUnavailableError(GatewayError):
    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "api_error"
    code = "upstream_unavailable"


class ServiceUnavailableError(GatewayError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "api_error"
    code = "service_unavailable"


class TooManyRequestsError(GatewayError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "rate_limit_error"
    code = "rate_limited"


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
