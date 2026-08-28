"""Plugin lookup.

Same shape as the redaction registry ([ADR 0026](../../../../docs/adr/0026-pluggable-detection.md)),
for the same reasons and resolved the same way in
[ADR 0032](../../../../docs/adr/0032-provider-plugins.md): the plugins we
maintain are in-tree, so they are reviewed and tested with the gateway, and a
deployment can add a counterparty of its own without forking:

```toml
[project.entry-points."llmp.providers"]
my-router = "my_package:MyRouterPlugin"
```

An unknown name is an error naming what *is* available, never a quiet
substitution. Falling back to the generic plugin would be worse than refusing:
the generic one reports no cost and no serving endpoint, so a router configured
by name and silently downgraded would look like a working deployment that had
stopped recording where its money went.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points

from gateway.plugins.anthropic import AnthropicPlugin
from gateway.plugins.base import ProviderPlugin
from gateway.plugins.cortecs import CortecsRouterPlugin
from gateway.plugins.generic import GenericOpenAIPlugin

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "llmp.providers"

#: What a provider row gets when it names no plugin. The behaviour the gateway
#: had before plugins existed.
DEFAULT_PLUGIN = "generic"

#: Every plugin is constructed with no arguments. It was briefly otherwise — the
#: generic one took the provider row's declared cost unit — and that parameter
#: went with the column it came from: a plugin that knows its counterparty knows
#: the unit already, and one that does not should not be guessing.
PluginFactory = Callable[[], ProviderPlugin]

_BUILTIN: dict[str, PluginFactory] = {
    "generic": GenericOpenAIPlugin,
    "anthropic": AnthropicPlugin,
    "cortecs": CortecsRouterPlugin,
}


class UnknownPluginError(ValueError):
    """The configured plugin name resolves to nothing."""


def _discovered() -> dict[str, PluginFactory]:
    """Factories advertised by installed packages.

    One that fails to import is logged and skipped rather than taking the gateway
    down — but only when it is *not* the plugin being asked for. ``resolve``
    raises in that case, because serving a counterparty with the wrong plugin
    means recording its usage wrongly.
    """
    found: dict[str, PluginFactory] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            found[entry.name] = entry.load()
        except Exception:
            logger.warning("provider plugin %r failed to load; ignoring", entry.name, exc_info=True)
    return found


def available() -> list[str]:
    return sorted({*_BUILTIN, *_discovered()})


def describe() -> list[dict[str, object]]:
    """Every installed plugin, for the console's provider-type selector.

    Built from the registry rather than written out in the frontend, so
    installing a plugin makes it selectable without a console release — which is
    the point of the entry point existing at all.

    ``billing_modes`` is the list a deployment may actually choose for this
    plugin: pass-through only appears where the plugin asserts its reported
    figure is the counterparty's real charge, so the UI cannot offer a
    configuration the API would then refuse.
    """
    described: list[dict[str, object]] = []
    for name in available():
        try:
            plugin = resolve(name)
        except UnknownPluginError:  # pragma: no cover - available() just listed it
            continue
        modes = ["own_prices"]
        if getattr(plugin, "reports_authoritative_cost", False):
            modes.append("provider_reported")
        described.append(
            {
                "name": plugin.name,
                "label": getattr(plugin, "label", plugin.name),
                "description": getattr(plugin, "description", ""),
                "kind": plugin.kind.value,
                "billing_modes": modes,
                # The counterparty's public endpoint, where one exists, so the
                # console pre-fills it and creating a Cortecs provider is typing
                # a name and a key. Null for a type whose endpoints vary.
                "default_base_url": getattr(plugin, "default_base_url", None),
                "is_default": plugin.name == DEFAULT_PLUGIN,
            }
        )
    return described


def resolve(name: str | None) -> ProviderPlugin:
    """The plugin for *name*, or the generic one when a row names none."""
    wanted = name or DEFAULT_PLUGIN

    # Built-ins win over plugins of the same name, so an installed package cannot
    # silently replace `generic` with something that behaves differently.
    if wanted in _BUILTIN:
        return _BUILTIN[wanted]()

    for entry in entry_points(group=ENTRY_POINT_GROUP):
        if entry.name != wanted:
            continue
        try:
            factory: PluginFactory = entry.load()
        except Exception as exc:
            raise UnknownPluginError(
                f"the provider plugin {wanted!r} is registered by an installed package "
                f"but failed to load: {exc}"
            ) from exc
        return factory()

    # Third-party plugins register under the ENTRY_POINT_GROUP entry point; see
    # docs/adr/0032-provider-plugins.md. That is how a name reaches the available
    # list, and is not an action for whoever typed the wrong one.
    raise UnknownPluginError(
        f"unknown provider plugin {wanted!r}. Available: {', '.join(available())}."
    )
