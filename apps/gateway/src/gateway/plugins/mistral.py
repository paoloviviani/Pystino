"""The first-party Mistral API."""

from __future__ import annotations

from gateway.plugins.generic import GenericOpenAIPlugin


class MistralPlugin(GenericOpenAIPlugin):
    """Mistral's own API: an OpenAI-compatible endpoint with its own host.

    Bearer auth, standard usage reporting, no vendor quirks worth a plugin
    beyond the address — which is exactly why it subclasses the generic plugin
    instead of copying it.
    """

    name = "mistral"
    label = "Mistral (direct)"
    description = "Mistral's own API, OpenAI-compatible. Billed from the configured prices."
    default_base_url: str | None = "https://api.mistral.ai/v1"
