"""``GET /opencode/install.sh``: what the gateway serves, and what it refuses to splice in."""

from __future__ import annotations

import subprocess

import httpx
import pytest
from gateway.routers.client_scripts import _ORIGIN_LINE


async def test_served_script_carries_the_requests_origin(client: httpx.AsyncClient) -> None:
    response = await client.get("/opencode/install.sh")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-shellscript")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "served_origin='http://gateway'" in response.text
    assert response.text.startswith("#!/usr/bin/env bash")
    # Valid shell, and the same file otherwise.
    assert subprocess.run(["bash", "-n"], input=response.text, text=True).returncode == 0


async def test_it_needs_no_credential_and_is_not_in_the_api_schema(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/opencode/install.sh")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert "/opencode/install.sh" not in schema["paths"]


@pytest.mark.parametrize("host", ["gateway:8443", "llm.example.org", "[::1]:8000", "10.0.0.5"])
async def test_ordinary_hosts_are_spliced_in(client: httpx.AsyncClient, host: str) -> None:
    response = await client.get("/opencode/install.sh", headers={"host": host})
    assert f"served_origin='http://{host}'" in response.text


@pytest.mark.parametrize(
    "host",
    [
        "x'; rm -rf ~; echo '",
        "evil.test'$(id)'",
        "a b",
        "host/with/path",
        "user@host",
        "$(id)",
        "host:notaport",
        "ho`id`st",
        "",
    ],
)
async def test_a_hostile_host_header_is_never_interpolated(
    client: httpx.AsyncClient, host: str
) -> None:
    response = await client.get("/opencode/install.sh", headers={"host": host})
    assert response.status_code == 200
    assert _ORIGIN_LINE in response.text  # left empty: the script then asks
    assert "rm -rf" not in response.text
    assert "$(id)" not in response.text
