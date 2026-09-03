"""Nebius AI Studio."""

from __future__ import annotations

from gateway.plugins.generic import GenericOpenAIPlugin


class NebiusPlugin(GenericOpenAIPlugin):
    """Nebius AI Studio: an OpenAI-compatible endpoint.

    Its cache-write token spellings — ``cache_write_tokens``,
    ``cache_creation_tokens`` — already reach the gateway when Nebius serves
    behind a router such as Cortecs, and ``accounting/cost.py`` reads both. A
    direct connection reads them through the same shared readers, which is the
    whole reason the readers live there rather than in a plugin.
    """

    name = "nebius"
    label = "Nebius AI Studio"
    description = (
        "Nebius AI Studio, OpenAI-compatible. Billed from the configured prices."
    )
    default_base_url: str | None = "https://api.studio.nebius.com/v1"
