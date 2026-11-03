"""The network guard for private (BYOK) providers.

A deployment provider's base URL is typed by an administrator, who could just
as well edit the compose file. A **private** provider's base URL is typed by any
member holding the capability, and the gateway then makes requests to it with a
credential and a body the member chose. Unguarded, that is a request-forgery
service inside the deployment's network: ``http://169.254.169.254/`` (cloud
metadata), ``http://postgres:5432``, ``http://gateway:8000/api/admin`` or a
laptop on the office LAN would all be reachable by anyone allowed to add a
provider, and the response (a model list, an error body) comes back to them.

So for a private provider the gateway refuses to talk to anything but a public
address:

* **what is refused:** loopback, private (RFC 1918 and fc00::/7), link-local
  (including the 169.254.169.254 metadata address and fe80::/10), carrier-grade
  NAT (100.64.0.0/10), multicast, unspecified, reserved and every other
  not-globally-routable range (``ipaddress``' ``is_global``), IPv4-mapped IPv6
  and the NAT64 / 6to4 forms judged by the IPv4 address they embed; and by
  *name*, the compose service names this deployment is made of (``gateway``,
  ``postgres``, ``valkey``, ``redaction``, ``authelia``, ``caddy``,
  ``extractor``, ``cerea``, ...), which a Docker network resolves to a private
  address anyway. Any single-label host is refused for the same reason, as are
  ``localhost`` and the ``.local``, ``.internal``, ``.lan``, ``.home.arpa`` and
  ``.localhost`` suffixes.
* **when:** at create and update (:func:`check_url`, a clear 400), before every
  request (the registry's preflight, so the caller gets a message naming the
  reason rather than a connection error), and **at connect time**, which is the
  one that cannot be raced.

**DNS rebinding.** Validating a URL and then handing the *name* to the HTTP
client resolves it twice: an attacker who controls the zone answers a public
address the first time and ``127.0.0.1`` the second (TTL 0). The choice here is
to pin rather than re-validate: :class:`GuardedBackend` is an httpcore network
backend, so the resolution that matters happens **inside the connect**. It
resolves the host itself, requires *every* address it got to be allowed (a
mixed answer is refused outright, not filtered, because filtering would let the
attacker choose which half we use), and then opens the socket to the validated
IP literal. The name is never resolved a second time, so there is no window to
rebind in. TLS is untouched by this: httpcore still performs the handshake with
the original hostname for SNI and certificate verification, only the TCP
connect goes to the pinned address. A kept-alive connection is simply a
connection that was validated when it was made; a changed DNS answer only
matters to the next new connection, which is validated again.

**Redirects** are never followed by any provider client in this gateway
(``follow_redirects=False``, ``build_http_client``), so a provider cannot answer
301 to ``http://169.254.169.254/`` and be followed there; the 3xx is returned to
the caller as the upstream error it is. The guard does not rely on that: were a
client ever configured to follow, each hop opens its connection through the same
backend and is judged like the first.

The admin switch ``byok_private_networks`` lifts all of this for private
providers (a lab whose members run vLLM on the LAN). Deployment providers never
pass through here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from gateway.config import UpstreamSettings

#: What the compose files of this deployment and of Cerea's kit name their
#: services. A single-label host is refused anyway; the list is for the message
#: and for a name that happens to carry a dot in someone's network.
INTERNAL_SERVICE_NAMES = frozenset(
    {
        "gateway",
        "pystino",
        "postgres",
        "valkey",
        "redis",
        "redaction",
        "authelia",
        "caddy",
        "proxy",
        "extractor",
        "cerea",
        "chat",
        "mongo",
        "mongodb",
        "migrate",
        "bootstrap",
        "localhost",
        "host.docker.internal",
        "metadata.google.internal",
    }
)

_INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".svc",
    ".intranet",
)

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")

#: Resolves a host to the addresses a connection would use. A seam: tests supply
#: one, production uses the event loop's ``getaddrinfo``.
Resolver = Callable[[str, int], Awaitable[list[str]]]


class NetworkGuardError(Exception):
    """A private provider's address is one the guard does not allow."""


async def system_resolver(host: str, port: int) -> list[str]:
    """Every address the system resolver gives ``host`` (IP literals pass through)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkGuardError(
            f"The host {host!r} does not resolve: {exc.strerror or exc}"
        ) from exc
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    if not seen:
        raise NetworkGuardError(f"The host {host!r} does not resolve.")
    return seen


def address_problem(address: str) -> str | None:
    """Why ``address`` may not be connected to, or ``None`` if it is public."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return f"{address!r} is not an IP address"
    if isinstance(ip, ipaddress.IPv6Address):
        # `::ffff:10.0.0.1` is 10.0.0.1 as far as the kernel is concerned.
        if ip.ipv4_mapped is not None:
            return address_problem(str(ip.ipv4_mapped))
        if ip in _NAT64:
            return address_problem(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        if ip in _SIX_TO_FOUR:
            embedded = ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
            return address_problem(str(embedded))
    if ip.is_unspecified:
        return "an unspecified address"
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address (this includes the cloud metadata address)"
    if ip.is_multicast:
        return "a multicast address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return "a carrier-grade NAT address"
    if ip.is_private:
        return "a private-network address"
    if ip.is_reserved:
        return "a reserved address"
    if not ip.is_global:
        return "an address that is not publicly routable"
    return None


def hostname_problem(host: str) -> str | None:
    """Why a *name* is refused regardless of what it resolves to."""
    name = host.rstrip(".").lower()
    if not name:
        return "the URL has no host"
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        pass
    else:
        return None  # a literal is judged by `address_problem`
    if name in INTERNAL_SERVICE_NAMES:
        return f"{name!r} is an internal service name"
    if "." not in name:
        return f"{name!r} is a bare host name, which only resolves inside a private network"
    if name.endswith(_INTERNAL_SUFFIXES):
        return f"{name!r} is an internal host name"
    return None


async def check_host(
    host: str, port: int, *, resolver: Resolver | None = None, allow_private: bool = False
) -> list[str]:
    """The validated addresses of ``host``, or :class:`NetworkGuardError`.

    Every resolved address must be allowed. With ``allow_private`` the check is
    skipped and the addresses are returned as resolved, so the caller that
    pins still pins.
    """
    resolve = resolver or system_resolver
    literal = host.strip("[]")
    if not allow_private and (reason := hostname_problem(host)):
        raise NetworkGuardError(
            f"This provider's host is not allowed: {reason}. A private provider "
            "must be a public endpoint (an administrator can allow private "
            "networks for private providers)."
        )
    addresses = [literal] if _is_ip(literal) else await resolve(host.rstrip("."), port)
    if not allow_private:
        for address in addresses:
            if reason := address_problem(address):
                raise NetworkGuardError(
                    f"This provider's host {host!r} resolves to {address}, {reason}. A private "
                    "provider must be a public endpoint (an administrator can allow private "
                    "networks for private providers)."
                )
    return addresses


async def check_url(
    url: str, *, resolver: Resolver | None = None, allow_private: bool = False
) -> None:
    """Refuse a base URL the guard does not allow (create, update, preflight)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise NetworkGuardError("The base URL must start with http:// or https://.")
    if parts.username is not None or parts.password is not None:
        raise NetworkGuardError(
            "The base URL must not carry credentials; put the key in the API key field."
        )
    host = parts.hostname
    if not host:
        raise NetworkGuardError("The base URL has no host.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise NetworkGuardError("The base URL has an invalid port.") from exc
    if allow_private:
        # Nothing to judge, so nothing to resolve: a name that does not resolve
        # fails at connect time like any other provider's. (The connect-time
        # backend still resolves and pins.)
        return
    await check_host(host, port, resolver=resolver, allow_private=False)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """An httpcore network backend that connects only to validated addresses.

    Resolution, validation and the connect are one step: the socket is opened
    to the IP that was validated, never to the name, which is what makes
    rebinding impossible (see the module docstring).
    """

    def __init__(
        self,
        *,
        allow_private: Callable[[], bool] = lambda: False,
        resolver: Resolver | None = None,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        from httpcore import AnyIOBackend

        self._allow_private = allow_private
        self._resolver = resolver
        self._inner = inner or AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's signature
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await check_host(
                host, port, resolver=self._resolver, allow_private=self._allow_private()
            )
        except NetworkGuardError as exc:
            # httpcore's own connect failure, so httpx maps it to ConnectError
            # and the request path treats it as "provider unreachable".
            raise httpcore.ConnectError(str(exc)) from exc
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                last = exc
        assert last is not None
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's signature
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed for private providers")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def build_guarded_client(
    settings: UpstreamSettings,
    *,
    allow_private: Callable[[], bool] = lambda: False,
    resolver: Resolver | None = None,
    inner: httpcore.AsyncNetworkBackend | None = None,
    read_timeout: float | None = None,
) -> httpx.AsyncClient:
    """The HTTP client for a private provider's traffic.

    Same timeouts as ``build_http_client`` (no read timeout, for streams) with
    the guarded backend underneath, no redirects, and ``trust_env=False`` so an
    ``HTTP_PROXY`` in the environment cannot route the connection around the
    guard (a proxy would resolve the name itself).

    ``transport._pool`` is an httpx private attribute: httpx offers no public
    way to give its transport a network backend, and ``test_netguard.py``
    connects through this client for real, so an httpx upgrade that moves it
    fails a test rather than silently dropping the guard.
    """
    timeout = httpx.Timeout(
        connect=settings.connect_timeout_seconds,
        read=read_timeout,
        write=settings.write_timeout_seconds,
        pool=settings.pool_timeout_seconds,
    )
    limits = httpx.Limits(
        max_connections=settings.max_connections,
        max_keepalive_connections=settings.max_keepalive_connections,
    )
    transport = httpx.AsyncHTTPTransport(limits=limits, trust_env=False)
    guarded = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=False),
        max_connections=limits.max_connections,
        max_keepalive_connections=limits.max_keepalive_connections,
        keepalive_expiry=limits.keepalive_expiry,
        http1=True,
        http2=False,
        network_backend=GuardedBackend(allow_private=allow_private, resolver=resolver, inner=inner),
    )
    transport._pool = guarded
    return httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
    )
