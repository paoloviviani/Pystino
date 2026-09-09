"""One upstream client per provider.

A request has to reach the endpoint that serves the model it names, with that
endpoint's credentials. This holds the clients that do it
(ADR 0027).

Per provider rather than one shared client, because an HTTP connection pool is a
pool *to a host*: pooling across providers would mean nothing is actually
pooled. Transport tuning — timeouts, pool sizes — stays global, since that is a
property of this gateway rather than of any provider.

Clients are built on first use and discarded when the provider row changes. The
cache key is the provider's ``updated_at``, so an edited base URL or a rotated
key takes effect on the next request without a restart and without a
subscription mechanism to get wrong.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from gateway.config import UpstreamSettings
from gateway.models import Provider
from gateway.plugins import registry
from gateway.plugins.base import ProviderPlugin
from gateway.secrets import SecretBox, SecretDecryptionError
from gateway.upstream import OpenAICompatibleUpstream, build_http_client

logger = logging.getLogger(__name__)


class ProviderConfigurationError(Exception):
    """A provider row cannot be turned into a usable client."""


@dataclass(slots=True)
class _Entry:
    upstream: OpenAICompatibleUpstream
    client: httpx.AsyncClient
    # What the provider looked like when this client was built. Any change means
    # the client is stale.
    fingerprint: tuple[str, str, datetime]


ClientFactory = Callable[[UpstreamSettings], httpx.AsyncClient]


class ProviderRegistry:
    """Builds and caches an upstream client per provider.

    ``client_factory`` is the test seam, mirroring the one ``init_app_state``
    already offers: injecting it lets a test drive every provider through a fake
    transport. When it is injected the registry does **not** close the clients it
    hands out — whoever supplied the factory owns their lifetime, and closing a
    shared injected client on one provider's behalf would break the others.
    """

    def __init__(
        self,
        settings: UpstreamSettings,
        secrets: SecretBox,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._build_client = client_factory or build_http_client
        self._owns_clients = client_factory is None
        self._entries: dict[uuid.UUID, _Entry] = {}
        # Two concurrent first requests for the same provider would otherwise
        # each build a client and one would be dropped on the floor, leaking its
        # connection pool.
        self._lock = asyncio.Lock()

    @staticmethod
    def _fingerprint(provider: Provider) -> tuple[str, str, datetime]:
        # `updated_at` covers every other editable field, including the plugin —
        # and therefore how the credential is presented: an edit bumps it, which
        # retires the cached client.
        return (provider.base_url, provider.api_key_encrypted, provider.updated_at)

    @staticmethod
    def _plugin_for(provider: Provider) -> ProviderPlugin:
        """The plugin for *provider*, degrading to generic if it is not installed.

        Refusing here would take a provider offline for a configuration mistake
        that the admin API already rejects on save; the case this covers is a
        plugin that was installed when the row was written and is not now. It
        degrades and says so loudly, matching how the recorder handles the same
        situation — but note the two consequences differ: the recorder loses a
        reported cost, this one may present the credential the wrong way and get
        a 401 that reads like an outage. Hence the log line naming the plugin.
        """
        try:
            return registry.resolve(provider.plugin)
        except registry.UnknownPluginError:
            logger.error(
                "provider %s names the plugin %r, which is not installed; "
                "presenting its credential as a bearer token, which may be refused",
                provider.name,
                provider.plugin,
            )
            return registry.resolve(None)

    def _settings_for(self, provider: Provider) -> UpstreamSettings:
        api_key = ""
        if provider.api_key_encrypted:
            try:
                api_key = self._secrets.decrypt(provider.api_key_encrypted)
            except SecretDecryptionError as exc:
                # Refused rather than sent unauthenticated. An unauthenticated
                # request arrives as a 401 from the provider and reads like an
                # outage; this says what actually happened.
                raise ProviderConfigurationError(f"provider {provider.name!r}: {exc}") from exc

        return self._settings.model_copy(
            update={
                "base_url": provider.base_url.rstrip("/"),
                "api_key": _as_secret(api_key),
                "extra_headers": dict(provider.extra_headers or {}),
            }
        )

    async def upstream_for(self, provider: Provider) -> OpenAICompatibleUpstream:
        """The client for *provider*, built if needed."""
        if not provider.is_active:
            raise ProviderConfigurationError(
                f"provider {provider.name!r} is deactivated, so the models it serves "
                "cannot be reached. Reactivate it or point the model elsewhere."
            )

        fingerprint = self._fingerprint(provider)
        entry = self._entries.get(provider.id)
        if entry is not None and entry.fingerprint == fingerprint:
            return entry.upstream

        async with self._lock:
            # Re-checked under the lock: another task may have built it while
            # this one waited.
            entry = self._entries.get(provider.id)
            if entry is not None and entry.fingerprint == fingerprint:
                return entry.upstream

            settings = self._settings_for(provider)
            client = self._build_client(settings)
            upstream = OpenAICompatibleUpstream(settings, client, plugin=self._plugin_for(provider))

            stale = self._entries.get(provider.id)
            self._entries[provider.id] = _Entry(upstream, client, fingerprint)

        if stale is not None and self._owns_clients:
            # Closed outside the lock, and after the replacement is installed, so
            # a slow teardown never blocks the request that triggered it.
            await _close_quietly(stale.client, provider.name)
            logger.info("provider %s changed; rebuilt its upstream client", provider.name)

        return upstream

    async def forget(self, provider_id: uuid.UUID) -> None:
        """Drop a provider's client, for a deletion or deactivation."""
        async with self._lock:
            entry = self._entries.pop(provider_id, None)
        if entry is not None and self._owns_clients:
            await _close_quietly(entry.client, str(provider_id))

    async def aclose(self) -> None:
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        if not self._owns_clients:
            return
        for entry in entries:
            await _close_quietly(entry.client, "shutdown")

    def build_probe(self, provider: Provider) -> tuple[OpenAICompatibleUpstream, httpx.AsyncClient]:
        """A throwaway client, for testing a provider's configuration.

        Deliberately not cached: a connection test runs against values an
        operator is still editing, and caching a client built from a half-typed
        base URL would then serve real traffic.
        """
        settings = self._settings_for(provider)
        client = self._build_client(settings)
        return OpenAICompatibleUpstream(settings, client, plugin=self._plugin_for(provider)), client


async def _close_quietly(client: httpx.AsyncClient, label: str) -> None:
    try:
        await client.aclose()
    except Exception:
        # A pool that will not close is not worth failing a request over; it is
        # worth a line in the log.
        logger.warning("could not close the HTTP client for %s", label, exc_info=True)


def _as_secret(value: str) -> object:
    """Wrap a plain string as the SecretStr the settings model expects."""
    from pydantic import SecretStr

    return SecretStr(value)
