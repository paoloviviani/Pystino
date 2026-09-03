"""The first-party OpenAI API."""

from __future__ import annotations

from gateway.plugins.generic import GenericOpenAIPlugin


class OpenAIPlugin(GenericOpenAIPlugin):
    """OpenAI's own API.

    The whole difference from the generic plugin is the endpoint to pre-fill:
    the wire shape, the bearer auth, and the ask-for-usage-on-streams behaviour
    are the ones every other OpenAI-compatible endpoint copied in the first
    place. It reports tokens, never a charge, so billing is from the configured
    prices — the same rule the generic plugin states.
    """

    name = "openai"
    label = "OpenAI (direct)"
    description = (
        "OpenAI's own API. Billed from the configured prices — the API reports "
        "tokens, not a charge."
    )
    default_base_url: str | None = "https://api.openai.com/v1"
