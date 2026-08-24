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

from gateway.plugins.base import ProviderPlugin
from gateway.plugins.cortecs import CortecsRouterPlugin
from gateway.plugins.generic import GenericOpenAIPlugin

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "llmp.providers"

#: What a provider row gets when it names no plugin. The behaviour the gateway
#: had before plugins existed.
DEFAULT_PLUGIN = "generic"

PluginFactory = Callable[[], ProviderPlugin]

_BUILTIN: dict[str, PluginFactory] = {
    "generic": GenericOpenAIPlugin,
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

    raise UnknownPluginError(
        f"unknown provider plugin {wanted!r}. Available: {', '.join(available())}. "
        f"Third-party plugins register under the {ENTRY_POINT_GROUP!r} entry-point group; "
        "see docs/adr/0032-provider-plugins.md."
    )
