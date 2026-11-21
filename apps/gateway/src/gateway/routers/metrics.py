"""``GET /metrics``: Prometheus text format, behind a bearer token (docs/metrics.md).

Registered only when ``GATEWAY_METRICS__ENABLED`` is on, so a deployment that
never asked for it answers 404 here like any unknown path. Kept out of the
OpenAPI schema: it is an operator's endpoint, not part of the console's API.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Request, Response

from gateway import metrics
from gateway.errors import AuthenticationError

router = APIRouter(include_in_schema=False)


def _same(presented: str, expected: str) -> bool:
    # Digests first, so the comparison is constant-time whatever the lengths.
    return hmac.compare_digest(
        hashlib.sha256(presented.encode()).digest(), hashlib.sha256(expected.encode()).digest()
    )


@router.get("/metrics")
async def scrape(request: Request) -> Response:
    expected = request.app.state.settings.metrics.token
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    # `and` after the compare, never before it: the compare always runs.
    if not (_same(presented.strip(), expected) and scheme.lower() == "bearer"):
        raise AuthenticationError("A bearer token is required for /metrics.")
    return Response(await metrics.render(request.app), media_type=metrics.CONTENT_TYPE)
