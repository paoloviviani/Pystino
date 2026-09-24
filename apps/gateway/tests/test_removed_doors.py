"""The house IdP and the local-password door are gone (ADR 0088, decision D3).

Removed, not merely off: the routes do not exist, and a deployment still
asking for either gets a startup error naming what to do instead (ADR 0065's
rule — a setting that would silently do nothing is worse than a refusal).
"""

from __future__ import annotations

import httpx
import pytest
from gateway.config import IdPSettings, LocalAuthSettings
from pydantic import ValidationError


def test_asking_for_the_local_door_refuses_to_start() -> None:
    with pytest.raises(ValidationError, match="pystino admin grant"):
        LocalAuthSettings(enabled=True)
    # The mail settings that used to live beside it still configure SMTP.
    assert LocalAuthSettings().password_reset.smtp_port == 587


def test_asking_for_the_house_idp_refuses_to_start() -> None:
    with pytest.raises(ValidationError, match="bundled Authelia"):
        IdPSettings(enabled=True)


async def test_the_routes_are_gone(client: httpx.AsyncClient) -> None:
    methods = (await client.get("/auth/methods")).json()
    assert methods["local"] is False
    for method, path in (
        ("POST", "/auth/login"),
        ("POST", "/auth/password-reset"),
        ("POST", "/auth/token"),
        ("GET", "/.well-known/openid-configuration"),
        ("GET", "/oauth/authorize"),
    ):
        response = await client.request(method, path, json={})
        assert response.status_code in (404, 405), (method, path, response.status_code)
