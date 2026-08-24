"""Counterparty plugins: providers and routers.

See [ADR 0032](../../../../docs/adr/0032-provider-plugins.md). The rule worth
repeating at the door: **a plugin returns facts and never computes money.**
"""

from __future__ import annotations

from gateway.plugins.base import (
    CataloguePrice,
    ProviderKind,
    ProviderPlugin,
    ReportedCost,
    ServedBy,
)
from gateway.plugins.cortecs import CortecsRouterPlugin
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.plugins.registry import (
    DEFAULT_PLUGIN,
    ENTRY_POINT_GROUP,
    UnknownPluginError,
    available,
    resolve,
)

__all__ = [
    "DEFAULT_PLUGIN",
    "ENTRY_POINT_GROUP",
    "CataloguePrice",
    "CortecsRouterPlugin",
    "GenericOpenAIPlugin",
    "ProviderKind",
    "ProviderPlugin",
    "ReportedCost",
    "ServedBy",
    "UnknownPluginError",
    "available",
    "resolve",
]
