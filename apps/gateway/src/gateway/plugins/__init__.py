"""Counterparty plugins: providers, routers and search backends.

See ADR 0032. The rule worth
repeating at the door: **a plugin returns facts and never computes money.**
"""

from __future__ import annotations

from gateway.plugins.anthropic import AnthropicPlugin
from gateway.plugins.base import (
    CataloguePrice,
    ProviderKind,
    ProviderPlugin,
    ReportedCost,
    ServedBy,
    bearer_headers,
)
from gateway.plugins.cortecs import CortecsRouterPlugin
from gateway.plugins.exa import ExaSearchPlugin
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.plugins.linkup import LinkupSearchPlugin
from gateway.plugins.registry import (
    DEFAULT_PLUGIN,
    ENTRY_POINT_GROUP,
    UnknownPluginError,
    available,
    resolve,
)
from gateway.plugins.search import (
    SearchPlugin,
)

__all__ = [
    "DEFAULT_PLUGIN",
    "ENTRY_POINT_GROUP",
    "AnthropicPlugin",
    "CataloguePrice",
    "CortecsRouterPlugin",
    "ExaSearchPlugin",
    "GenericOpenAIPlugin",
    "LinkupSearchPlugin",
    "ProviderKind",
    "ProviderPlugin",
    "ReportedCost",
    "SearchConfigurationError",
    "SearchOutcome",
    "SearchPlugin",
    "SearchQuery",
    "SearchResult",
    "SearchUnreadableError",
    "ServedBy",
    "UnknownPluginError",
    "available",
    "bearer_headers",
    "normalise_tier",
    "resolve",
]
