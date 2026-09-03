"""Tensorix."""

from __future__ import annotations

from gateway.plugins.generic import GenericOpenAIPlugin


class TensorixPlugin(GenericOpenAIPlugin):
    """Tensorix, treated as an OpenAI-compatible endpoint.

    Registered with **no default base URL**, deliberately: this plugin's only
    claim is the wire shape, and an address the gateway cannot verify is not a
    fact a plugin should assert. The provider row's Base URL field is where the
    endpoint goes, exactly as it does for the generic type.

    If Tensorix grows quirks of its own — an odd auth header, a reported cost
    in an unusual unit — they belong in *this* file, which is the point of it
    existing rather than everyone choosing "generic".
    """

    name = "tensorix"
    label = "Tensorix"
    description = (
        "Tensorix, OpenAI-compatible. Set the endpoint on the provider row. "
        "Billed from the configured prices."
    )
    default_base_url: str | None = None
