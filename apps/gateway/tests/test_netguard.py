"""The network guard for private (BYOK) providers.

A private provider's base URL is typed by a member, so the gateway must never be
steerable at an internal service through it. These tests pin each refused range,
the by-name refusals, the connect-time pinning that makes DNS rebinding
impossible, and — against a real local server, not a mock — that the guarded
client refuses loopback and does not follow a redirect.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpcore
import httpx
import pytest
from gateway.config import UpstreamSettings
from gateway.netguard import (
    GuardedBackend,
    NetworkGuardError,
    address_problem,
    build_guarded_client,
    check_url,
    hostname_problem,
)

PUBLIC = "93.184.216.34"


async def public_dns(host: str, port: int) -> list[str]:
    return [PUBLIC]


@pytest.mark.parametrize(
    ("address", "fragment"),
    [
        ("127.0.0.1", "loopback"),
        ("127.255.255.254", "loopback"),
        ("::1", "loopback"),
        ("10.0.0.5", "private"),
        ("172.16.0.1", "private"),
        ("172.31.255.255", "private"),
        ("192.168.1.1", "private"),
        ("fd00::1", "private"),
        ("fc00::5", "private"),
        ("169.254.169.254", "link-local"),
        ("169.254.0.1", "link-local"),
        ("fe80::1", "link-local"),
        ("fd00:ec2::254", "private"),
        ("100.64.0.1", "carrier-grade"),
        ("100.127.255.255", "carrier-grade"),
        ("224.0.0.1", "multicast"),
        ("ff02::1", "multicast"),
        ("0.0.0.0", "unspecified"),  # noqa: S104 - a refused address, not a bind
        ("::", "unspecified"),
        ("240.0.0.1", "address"),
        ("192.0.0.1", "address"),
        ("198.18.0.1", "address"),
        # The IPv4 address embedded in an IPv6 form is what is judged.
        ("::ffff:10.0.0.1", "private"),
        ("::ffff:127.0.0.1", "loopback"),
        ("::ffff:169.254.169.254", "link-local"),
        ("64:ff9b::a00:1", "private"),
        ("2002:7f00:1::1", "loopback"),
    ],
)
def test_every_non_public_range_is_refused(address: str, fragment: str) -> None:
    reason = address_problem(address)
    assert reason is not None, address
    assert fragment in reason


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_a_public_address_is_allowed(address: str) -> None:
    assert address_problem(address) is None


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST.",
        "gateway",
        "postgres",
        "valkey",
        "redaction",
        "authelia",
        "caddy",
        "some-compose-service",
        "db.internal",
        "printer.local",
        "foo.lan",
        "metadata.google.internal",
        "host.docker.internal",
        "x.localhost",
    ],
)
def test_internal_names_are_refused_before_any_lookup(host: str) -> None:
    assert hostname_problem(host) is not None


@pytest.mark.parametrize("host", ["api.openai.com", "inference.example.org", "a.b.c.d.example.com"])
def test_a_public_name_is_allowed(host: str) -> None:
    assert hostname_problem(host) is None


class TestCheckUrl:
    async def test_a_public_https_url_passes(self) -> None:
        await check_url("https://api.example.com/v1", resolver=public_dns)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/v1",
            "http://localhost/v1",
            "http://[::1]/v1",
            "http://169.254.169.254/latest/meta-data",
            "http://10.1.2.3/v1",
            "http://192.168.0.10:11434/v1",
            "http://100.64.0.9/v1",
            "http://gateway:8000/v1",
            "http://postgres:5432",
            "http://0.0.0.0/",
            # Numeric spellings of loopback: the resolver turns them into it.
            "http://2130706433/",
            "http://0x7f000001/",
            "http://127.1/",
        ],
    )
    async def test_internal_targets_are_refused(self, url: str) -> None:
        with pytest.raises(NetworkGuardError):
            await check_url(url)

    async def test_a_name_that_resolves_to_a_private_address_is_refused(self) -> None:
        async def rebinder(host: str, port: int) -> list[str]:
            return ["10.0.0.7"]

        with pytest.raises(NetworkGuardError) as caught:
            await check_url("https://sneaky.example.com/v1", resolver=rebinder)
        assert "10.0.0.7" in str(caught.value)
        assert "private" in str(caught.value)

    async def test_one_bad_address_among_good_ones_refuses_the_name(self) -> None:
        async def mixed(host: str, port: int) -> list[str]:
            return [PUBLIC, "127.0.0.1"]

        with pytest.raises(NetworkGuardError):
            await check_url("https://mixed.example.com/v1", resolver=mixed)

    @pytest.mark.parametrize(
        "url", ["ftp://api.example.com/", "file:///etc/passwd", "api.example.com/v1", "https://"]
    )
    async def test_only_http_and_https_with_a_host(self, url: str) -> None:
        with pytest.raises(NetworkGuardError):
            await check_url(url, resolver=public_dns)

    async def test_credentials_in_the_url_are_refused(self) -> None:
        with pytest.raises(NetworkGuardError):
            await check_url("https://user:pw@api.example.com/v1", resolver=public_dns)

    async def test_the_admin_switch_lifts_the_guard(self) -> None:
        await check_url("http://10.0.0.5:8000/v1", allow_private=True)
        await check_url("http://gateway:8000/v1", resolver=public_dns, allow_private=True)


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """Records what the guard asks the real backend to connect to."""

    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host: str, port: int, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        self.connected.append((host, port))
        raise httpcore.ConnectError("recorded, not connected")

    async def sleep(self, seconds: float) -> None:
        return None


class TestRebinding:
    async def test_the_socket_goes_to_the_address_that_was_validated(self) -> None:
        """The connect receives an IP literal, never the name, so nothing is
        resolved a second time and there is no window to rebind in."""
        inner = _RecordingBackend()
        lookups: list[str] = []

        async def resolver(host: str, port: int) -> list[str]:
            lookups.append(host)
            # First answer public; a later lookup (which must never happen)
            # would be the attacker's loopback.
            return [PUBLIC] if len(lookups) == 1 else ["127.0.0.1"]

        backend = GuardedBackend(resolver=resolver, inner=inner)
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("rebind.example.com", 443)
        assert inner.connected == [(PUBLIC, 443)]
        assert lookups == ["rebind.example.com"]

    async def test_every_new_connection_is_validated_again(self) -> None:
        inner = _RecordingBackend()
        answers = iter([[PUBLIC], ["127.0.0.1"]])

        async def resolver(host: str, port: int) -> list[str]:
            return next(answers)

        backend = GuardedBackend(resolver=resolver, inner=inner)
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("flip.example.com", 443)
        with pytest.raises(httpcore.ConnectError) as blocked:
            await backend.connect_tcp("flip.example.com", 443)
        assert "loopback" in str(blocked.value)
        assert inner.connected == [(PUBLIC, 443)]

    async def test_a_blocked_address_never_reaches_the_real_backend(self) -> None:
        inner = _RecordingBackend()
        backend = GuardedBackend(inner=inner)
        for host in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "gateway", "localhost"):
            with pytest.raises(httpcore.ConnectError):
                await backend.connect_tcp(host, 80)
        assert inner.connected == []

    async def test_the_switch_is_read_at_connect_time(self) -> None:
        inner = _RecordingBackend()
        allowed = {"on": False}
        backend = GuardedBackend(allow_private=lambda: allowed["on"], inner=inner)
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("10.0.0.5", 80)
        assert inner.connected == []
        allowed["on"] = True
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("10.0.0.5", 80)
        assert inner.connected == [("10.0.0.5", 80)]


@pytest.fixture
async def local_server() -> AsyncIterator[tuple[int, list[bytes]]]:
    """A real HTTP server on loopback: 302 to the metadata address on /redirect,
    200 otherwise. Records every request it receives."""
    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.readuntil(b"\r\n\r\n")
        received.append(data)
        if data.startswith(b"GET /redirect"):
            body = (
                b"HTTP/1.1 302 Found\r\nlocation: http://169.254.169.254/latest/meta-data\r\n"
                b"content-length: 0\r\nconnection: close\r\n\r\n"
            )
        else:
            body = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok"
        writer.write(body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, received
    finally:
        server.close()
        await server.wait_closed()


class TestTheRealClient:
    """Through ``build_guarded_client`` and a real socket: if httpx ever moves
    the transport's pool, this fails rather than the guard silently going."""

    async def test_loopback_is_refused_and_nothing_is_sent(
        self, local_server: tuple[int, list[bytes]]
    ) -> None:
        port, received = local_server
        client = build_guarded_client(UpstreamSettings(base_url="http://x/v1"))
        try:
            with pytest.raises(httpx.ConnectError) as caught:
                await client.get(f"http://127.0.0.1:{port}/models")
        finally:
            await client.aclose()
        assert "loopback" in str(caught.value)
        assert received == []

    async def test_a_name_resolving_to_loopback_is_refused_at_connect(
        self, local_server: tuple[int, list[bytes]]
    ) -> None:
        port, received = local_server

        async def resolver(host: str, port_: int) -> list[str]:
            return ["127.0.0.1"]

        client = build_guarded_client(UpstreamSettings(base_url="http://x/v1"), resolver=resolver)
        try:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"http://rebound.example.com:{port}/models")
        finally:
            await client.aclose()
        assert received == []

    async def test_the_admin_switch_lets_a_private_provider_through(
        self, local_server: tuple[int, list[bytes]]
    ) -> None:
        port, received = local_server
        client = build_guarded_client(
            UpstreamSettings(base_url="http://x/v1"), allow_private=lambda: True
        )
        try:
            response = await client.get(f"http://127.0.0.1:{port}/models")
        finally:
            await client.aclose()
        assert response.status_code == 200
        assert len(received) == 1

    async def test_a_redirect_is_not_followed(self, local_server: tuple[int, list[bytes]]) -> None:
        """Even with the guard lifted, the 302 to the metadata address is
        returned as a response: no second request is made anywhere."""
        port, received = local_server
        client = build_guarded_client(
            UpstreamSettings(base_url="http://x/v1"), allow_private=lambda: True
        )
        try:
            response = await client.get(f"http://127.0.0.1:{port}/redirect")
        finally:
            await client.aclose()
        assert response.status_code == 302
        assert len(received) == 1

    async def test_a_proxy_in_the_environment_cannot_route_around_the_guard(
        self, local_server: tuple[int, list[bytes]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``HTTP_PROXY`` set, a trusting client would send the request to
        the proxy and never resolve the name itself. The guarded client does
        not trust the environment: it connects to the validated address."""
        port, received = local_server
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")
        inner = _RecordingBackend()
        client = build_guarded_client(
            UpstreamSettings(base_url="http://x/v1"), resolver=public_dns, inner=inner
        )
        try:
            with pytest.raises(httpx.ConnectError):
                await client.get("http://public.example.com/models")
        finally:
            await client.aclose()
        assert received == []
        assert inner.connected == [(PUBLIC, 80)]
